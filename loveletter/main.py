from __future__ import annotations

import asyncio
import random
import string
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .game_logic import (
    CARD_NAMES,
    CHANCELLOR,
    COUNTESS,
    GUARD,
    HANDMAID,
    PRINCESS,
    SPY,
    TOKENS_TO_WIN,
    GameEngine,
    PendingAction,
    Player,
    Room,
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.connections: dict[str, dict[str, WebSocket]] = {}
        self.room_locks: dict[str, asyncio.Lock] = {}

    def _gen_room_id(self) -> str:
        while True:
            rid = "".join(random.choices(string.ascii_uppercase, k=4))
            if rid not in self.rooms:
                return rid

    async def connect(self, ws: WebSocket, room_id: str, player_id: str) -> None:
        if room_id not in self.connections:
            self.connections[room_id] = {}
        self.connections[room_id][player_id] = ws

    def disconnect(self, room_id: str, player_id: str) -> None:
        if room_id in self.connections:
            self.connections[room_id].pop(player_id, None)
        room = self.rooms.get(room_id)
        if room:
            p = GameEngine._get_player(room, player_id)
            if p:
                p.is_connected = False

    async def send_to(self, room_id: str, player_id: str, msg: dict) -> None:
        ws = self.connections.get(room_id, {}).get(player_id)
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    async def broadcast(self, room_id: str, msg: dict) -> None:
        for pid, ws in list(self.connections.get(room_id, {}).items()):
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    async def broadcast_state(self, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if not room:
            return
        for pid, ws in list(self.connections.get(room_id, {}).items()):
            try:
                await ws.send_json(self._build_state(room, pid))
            except Exception:
                pass

    def _build_state(self, room: Room, viewer_id: str) -> dict:
        current_player = room.players[room.current_player_idx] if room.players else None
        players_data = []
        for p in room.players:
            pd: dict = {
                "player_id": p.player_id,
                "name": p.name,
                "tokens": p.tokens,
                "discard_pile": p.discard_pile,
                "eliminated": p.eliminated,
                "protected": p.protected,
                "played_spy": p.played_spy,
                "is_connected": p.is_connected,
            }
            if p.player_id == viewer_id:
                pd["hand"] = p.hand
                pd["hand_count"] = len(p.hand)
            else:
                pd["hand"] = []
                pd["hand_count"] = len(p.hand)
            players_data.append(pd)

        pending = None
        if room.pending_action:
            pa = room.pending_action
            pending = {
                "card_played": pa.card_played,
                "phase": pa.phase,
                "acting_player_id": pa.acting_player_id,
                "candidate_target_ids": pa.candidate_target_ids,
            }
            if (
                pa.phase == "await_chancellor_return"
                and pa.acting_player_id == viewer_id
            ):
                pending["chancellor_hand"] = pa.chancellor_hand

        return {
            "type": "state_update",
            "phase": room.phase,
            "current_player_id": current_player.player_id if current_player else None,
            "deck_count": len(room.deck),
            "face_up_cards": room.face_up_cards,
            "players": players_data,
            "your_player_id": viewer_id,
            "pending_action": pending,
            "winner_ids": room.winner_ids,
            "round_winner_ids": room.round_winner_ids,
        }

    def get_lock(self, room_id: str) -> asyncio.Lock:
        if room_id not in self.room_locks:
            self.room_locks[room_id] = asyncio.Lock()
        return self.room_locks[room_id]


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Message Handlers
# ---------------------------------------------------------------------------


async def handle_create_room(ws: WebSocket, data: dict) -> str:
    player_id = data["player_id"]
    name = data.get("name", "Player")
    room_id = manager._gen_room_id()
    room = Room(room_id=room_id, host_id=player_id)
    player = Player(player_id=player_id, name=name)
    room.players.append(player)
    manager.rooms[room_id] = room
    await manager.connect(ws, room_id, player_id)
    await ws.send_json(
        {"type": "room_created", "room_id": room_id, "player_id": player_id}
    )
    await manager.broadcast_state(room_id)
    return room_id


async def handle_join(ws: WebSocket, data: dict) -> None:
    player_id = data["player_id"]
    name = data.get("name", "Player")
    room_id = data["room_id"].upper()
    room = manager.rooms.get(room_id)

    if not room:
        await ws.send_json(
            {
                "type": "error",
                "code": "ROOM_NOT_FOUND",
                "message": "ルームが見つかりません",
            }
        )
        return

    existing = GameEngine._get_player(room, player_id)
    if existing:
        existing.is_connected = True
        await manager.connect(ws, room_id, player_id)
        await ws.send_json(
            {"type": "room_created", "room_id": room_id, "player_id": player_id}
        )
        await manager.broadcast_state(room_id)
        return

    if room.phase != "lobby":
        await ws.send_json(
            {
                "type": "error",
                "code": "GAME_IN_PROGRESS",
                "message": "ゲームは既に開始されています",
            }
        )
        return

    if len(room.players) >= 6:
        await ws.send_json(
            {"type": "error", "code": "ROOM_FULL", "message": "ルームが満員です"}
        )
        return

    player = Player(player_id=player_id, name=name)
    room.players.append(player)
    await manager.connect(ws, room_id, player_id)
    await manager.broadcast(
        room_id, {"type": "player_joined", "player_id": player_id, "name": name}
    )
    await manager.broadcast_state(room_id)


async def handle_start_game(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    room = manager.rooms.get(room_id)

    if not room:
        await ws.send_json(
            {
                "type": "error",
                "code": "ROOM_NOT_FOUND",
                "message": "ルームが見つかりません",
            }
        )
        return
    if player_id != room.host_id:
        await ws.send_json(
            {"type": "error", "code": "NOT_HOST", "message": "ホストのみ開始できます"}
        )
        return
    if len(room.players) < 2:
        await ws.send_json(
            {
                "type": "error",
                "code": "NOT_ENOUGH_PLAYERS",
                "message": "2人以上必要です",
            }
        )
        return

    n = len(room.players)
    room.tokens_to_win = TOKENS_TO_WIN.get(n, 3)
    GameEngine.setup_round(room)
    await manager.broadcast_state(room_id)


async def handle_play_card(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    card_index = data["card_index"]
    room = manager.rooms.get(room_id)

    if not room or room.phase != "playing":
        await ws.send_json(
            {"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"}
        )
        return

    current = room.players[room.current_player_idx]
    if current.player_id != player_id:
        await ws.send_json(
            {
                "type": "error",
                "code": "NOT_YOUR_TURN",
                "message": "あなたのターンではありません",
            }
        )
        return
    if room.pending_action:
        await ws.send_json(
            {
                "type": "error",
                "code": "PENDING_ACTION",
                "message": "アクションが保留中です",
            }
        )
        return

    if card_index < 0 or card_index >= len(current.hand):
        await ws.send_json(
            {
                "type": "error",
                "code": "INVALID_CARD",
                "message": "無効なカードインデックスです",
            }
        )
        return

    card_value = current.hand[card_index]
    err = GameEngine.validate_play(room, current, card_value)
    if err:
        await ws.send_json(
            {"type": "error", "code": err, "message": f"プレイ不可: {err}"}
        )
        return

    # Remove from hand, add to discard
    current.hand.pop(card_index)
    current.discard_pile.append(card_value)

    # No-target cards
    if card_value in (SPY, HANDMAID, COUNTESS, PRINCESS):
        events = GameEngine.resolve_card(room, current, card_value)
        for ev in events:
            await manager.broadcast(room_id, {"type": "game_event", **ev})
        if GameEngine.check_round_end(room):
            await _end_round(room_id)
            return
        GameEngine.advance_turn(room)
        await manager.broadcast_state(room_id)
        return

    if card_value == CHANCELLOR:
        # Gather up to 2 cards from deck into chancellor_hand
        chancellor_hand = list(current.hand)
        for _ in range(2):
            if room.deck:
                chancellor_hand.append(room.deck.pop())

        if len(chancellor_hand) <= 1:
            # Nothing meaningful to choose; keep as-is
            current.hand = chancellor_hand[:1]
            for c in chancellor_hand[1:]:
                room.deck.insert(0, c)
            await manager.broadcast(
                room_id,
                {
                    "type": "game_event",
                    "event_type": "card_played",
                    "data": {
                        "player_id": current.player_id,
                        "player_name": current.name,
                        "card": card_value,
                        "card_name": CARD_NAMES[card_value],
                        "description": f"{current.name} が Chancellor をプレイしました（引けるカードなし）。",
                    },
                },
            )
            if GameEngine.check_round_end(room):
                await _end_round(room_id)
                return
            GameEngine.advance_turn(room)
            await manager.broadcast_state(room_id)
            return

        room.pending_action = PendingAction(
            card_played=card_value,
            phase="await_chancellor_return",
            acting_player_id=current.player_id,
            chancellor_hand=chancellor_hand,
        )
        # Clear current hand until chancellor is resolved
        current.hand = []
        await manager.broadcast_state(room_id)
        await manager.send_to(
            room_id,
            current.player_id,
            {
                "type": "await_input",
                "action_type": "chancellor_return",
                "card_played": card_value,
                "chancellor_hand": chancellor_hand,
                "extra": {},
            },
        )
        return

    # Cards needing a target
    candidates = GameEngine.compute_valid_targets(room, current, card_value)

    if not candidates:
        # Fizzle
        await manager.broadcast(
            room_id,
            {
                "type": "game_event",
                "event_type": "fizzle",
                "data": {
                    "player_id": current.player_id,
                    "player_name": current.name,
                    "card": card_value,
                    "card_name": CARD_NAMES[card_value],
                    "description": f"{current.name} の {CARD_NAMES[card_value]} は全員保護されているため不発でした。",
                },
            },
        )
        if GameEngine.check_round_end(room):
            await _end_round(room_id)
            return
        GameEngine.advance_turn(room)
        await manager.broadcast_state(room_id)
        return

    room.pending_action = PendingAction(
        card_played=card_value,
        phase="await_target",
        acting_player_id=current.player_id,
        candidate_target_ids=candidates,
    )
    await manager.broadcast_state(room_id)
    await manager.send_to(
        room_id,
        current.player_id,
        {
            "type": "await_input",
            "action_type": "select_target",
            "candidates": candidates,
            "card_played": card_value,
            "extra": {},
        },
    )


async def handle_select_target(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    target_id = data["target_id"]
    room = manager.rooms.get(room_id)

    if not room or not room.pending_action:
        await ws.send_json(
            {"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"}
        )
        return

    pa = room.pending_action
    if pa.acting_player_id != player_id or pa.phase != "await_target":
        await ws.send_json(
            {
                "type": "error",
                "code": "NOT_YOUR_ACTION",
                "message": "あなたのアクションではありません",
            }
        )
        return
    if target_id not in pa.candidate_target_ids:
        await ws.send_json(
            {
                "type": "error",
                "code": "INVALID_TARGET",
                "message": "無効なターゲットです",
            }
        )
        return

    acting = GameEngine._get_player(room, player_id)
    target = GameEngine._get_player(room, target_id)
    card_value = pa.card_played

    if card_value == GUARD:
        room.pending_action = PendingAction(
            card_played=card_value,
            phase="await_guard_guess",
            acting_player_id=player_id,
            candidate_target_ids=[target_id],
        )
        await manager.broadcast_state(room_id)
        await manager.send_to(
            room_id,
            player_id,
            {
                "type": "await_input",
                "action_type": "guard_guess",
                "card_played": card_value,
                "target_id": target_id,
                "target_name": target.name,
                "extra": {},
            },
        )
        return

    events = GameEngine.resolve_card(room, acting, card_value, target)
    room.pending_action = None
    for ev in events:
        await manager.broadcast(room_id, {"type": "game_event", **ev})

    if GameEngine.check_round_end(room):
        await _end_round(room_id)
        return
    GameEngine.advance_turn(room)
    await manager.broadcast_state(room_id)


async def handle_guard_guess(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    guessed_value = int(data["guessed_value"])
    room = manager.rooms.get(room_id)

    if not room or not room.pending_action:
        await ws.send_json(
            {"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"}
        )
        return

    pa = room.pending_action
    if pa.acting_player_id != player_id or pa.phase != "await_guard_guess":
        await ws.send_json(
            {
                "type": "error",
                "code": "NOT_YOUR_ACTION",
                "message": "あなたのアクションではありません",
            }
        )
        return
    if guessed_value == GUARD:
        await ws.send_json(
            {
                "type": "error",
                "code": "INVALID_GUESS",
                "message": "Guardは推測できません",
            }
        )
        return

    acting = GameEngine._get_player(room, player_id)
    target_id = pa.candidate_target_ids[0]
    target = GameEngine._get_player(room, target_id)
    room.pending_action = None

    events = GameEngine.resolve_guard(room, acting, target, guessed_value)
    for ev in events:
        await manager.broadcast(room_id, {"type": "game_event", **ev})

    if GameEngine.check_round_end(room):
        await _end_round(room_id)
        return
    GameEngine.advance_turn(room)
    await manager.broadcast_state(room_id)


async def handle_chancellor_return(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    kept_card = int(data["kept_card"])
    bottom_order = [int(c) for c in data["bottom_order"]]
    room = manager.rooms.get(room_id)

    if not room or not room.pending_action:
        await ws.send_json(
            {"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"}
        )
        return

    pa = room.pending_action
    if pa.acting_player_id != player_id or pa.phase != "await_chancellor_return":
        await ws.send_json(
            {
                "type": "error",
                "code": "NOT_YOUR_ACTION",
                "message": "あなたのアクションではありません",
            }
        )
        return

    chancellor_hand = pa.chancellor_hand
    if kept_card not in chancellor_hand:
        await ws.send_json(
            {"type": "error", "code": "INVALID_CARD", "message": "無効なカードです"}
        )
        return

    acting = GameEngine._get_player(room, player_id)
    acting.hand = [kept_card]
    remaining = list(chancellor_hand)
    remaining.remove(kept_card)

    if sorted(bottom_order) != sorted(remaining):
        bottom_order = remaining

    for c in reversed(bottom_order):
        room.deck.insert(0, c)

    room.pending_action = None
    await manager.broadcast(
        room_id,
        {
            "type": "game_event",
            "event_type": "chancellor_used",
            "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "description": f"{acting.name} が Chancellor をプレイ、手札を選び直しました。",
            },
        },
    )

    if GameEngine.check_round_end(room):
        await _end_round(room_id)
        return
    GameEngine.advance_turn(room)
    await manager.broadcast_state(room_id)


async def handle_next_round(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    room = manager.rooms.get(room_id)

    if not room or room.phase != "round_end":
        return
    if player_id != room.host_id:
        await ws.send_json(
            {
                "type": "error",
                "code": "NOT_HOST",
                "message": "ホストのみ次ラウンドを開始できます",
            }
        )
        return

    if len(room.round_winner_ids) == 1:
        w = GameEngine._get_player(room, room.round_winner_ids[0])
        if w:
            room.current_player_idx = room.players.index(w)
    elif room.round_winner_ids:
        chosen_id = random.choice(room.round_winner_ids)
        w = GameEngine._get_player(room, chosen_id)
        if w:
            room.current_player_idx = room.players.index(w)

    GameEngine.setup_round(room)
    await manager.broadcast_state(room_id)


async def _end_round(room_id: str) -> None:
    room = manager.rooms.get(room_id)
    if not room:
        return

    room.phase = "round_end"
    winner_ids = GameEngine.evaluate_round_winner(room)
    room.round_winner_ids = winner_ids
    award_info = GameEngine.award_tokens(room, winner_ids)

    revealed = [
        {"player_id": p.player_id, "name": p.name, "hand": p.hand} for p in room.players
    ]
    token_updates = [
        {"player_id": p.player_id, "name": p.name, "tokens": p.tokens}
        for p in room.players
    ]

    await manager.broadcast(
        room_id,
        {
            "type": "round_end",
            "round_winner_ids": winner_ids,
            "spy_bonus_id": award_info["spy_bonus_id"],
            "revealed_hands": revealed,
            "token_updates": token_updates,
        },
    )

    game_winners = GameEngine.check_game_winner(room)
    if game_winners:
        room.winner_ids = game_winners
        room.phase = "game_over"
        await manager.broadcast(
            room_id, {"type": "game_over", "winner_ids": game_winners}
        )

    await manager.broadcast_state(room_id)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    room_id: Optional[str] = None
    player_id: Optional[str] = None

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if msg_type == "create_room":
                player_id = data["player_id"]
                room_id = await handle_create_room(ws, data)
                continue

            if msg_type == "join":
                player_id = data["player_id"]
                room_id = data["room_id"].upper()
                lock = manager.get_lock(room_id)
                async with lock:
                    await handle_join(ws, data)
                continue

            # Remaining messages need room context
            rid = data.get("room_id", "")
            if rid:
                room_id = rid.upper()
            if not player_id:
                player_id = data.get("player_id")

            if not room_id:
                await ws.send_json(
                    {
                        "type": "error",
                        "code": "NO_ROOM",
                        "message": "ルームに参加していません",
                    }
                )
                continue

            lock = manager.get_lock(room_id)
            async with lock:
                if msg_type == "start_game":
                    await handle_start_game(ws, data)
                elif msg_type == "play_card":
                    await handle_play_card(ws, data)
                elif msg_type == "select_target":
                    await handle_select_target(ws, data)
                elif msg_type == "guard_guess":
                    await handle_guard_guess(ws, data)
                elif msg_type == "chancellor_return":
                    await handle_chancellor_return(ws, data)
                elif msg_type == "next_round":
                    await handle_next_round(ws, data)

    except WebSocketDisconnect:
        if room_id and player_id:
            manager.disconnect(room_id, player_id)
    except Exception as e:
        try:
            await ws.send_json(
                {"type": "error", "code": "SERVER_ERROR", "message": str(e)}
            )
        except Exception:
            pass
        if room_id and player_id:
            manager.disconnect(room_id, player_id)


if __name__ == "__main__":
    uvicorn.run("loveletter.main:app", host="0.0.0.0", port=8000, reload=True)
