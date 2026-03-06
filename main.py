from __future__ import annotations

import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Optional
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Player:
    player_id: str
    name: str
    hand: list[int] = field(default_factory=list)
    discard_pile: list[int] = field(default_factory=list)
    tokens: int = 0
    eliminated: bool = False
    protected: bool = False
    played_spy: bool = False
    is_connected: bool = True


@dataclass
class PendingAction:
    card_played: int
    phase: str  # "await_target" | "await_guard_guess" | "await_chancellor_return"
    acting_player_id: str
    candidate_target_ids: list[str] = field(default_factory=list)
    chancellor_hand: list[int] = field(default_factory=list)


@dataclass
class Room:
    room_id: str
    host_id: str
    players: list[Player] = field(default_factory=list)
    phase: str = "lobby"  # "lobby" | "playing" | "round_end" | "game_over"
    deck: list[int] = field(default_factory=list)
    aside_card: int = -1
    face_up_cards: list[int] = field(default_factory=list)
    current_player_idx: int = 0
    winner_ids: list[str] = field(default_factory=list)
    round_winner_ids: list[str] = field(default_factory=list)
    tokens_to_win: int = 6
    pending_action: Optional[PendingAction] = None


# ---------------------------------------------------------------------------
# Card constants
# ---------------------------------------------------------------------------
SPY = 0
GUARD = 1
PRIEST = 2
BARON = 3
HANDMAID = 4
PRINCE = 5
CHANCELLOR = 6
KING = 7
COUNTESS = 8
PRINCESS = 9

CARD_NAMES = {
    SPY: "Spy",
    GUARD: "Guard",
    PRIEST: "Priest",
    BARON: "Baron",
    HANDMAID: "Handmaid",
    PRINCE: "Prince",
    CHANCELLOR: "Chancellor",
    KING: "King",
    COUNTESS: "Countess",
    PRINCESS: "Princess",
}

TOKENS_TO_WIN = {2: 6, 3: 5, 4: 4, 5: 3, 6: 3}


# ---------------------------------------------------------------------------
# Game Engine
# ---------------------------------------------------------------------------

class GameEngine:

    @staticmethod
    def build_deck() -> list[int]:
        deck = (
            [SPY] * 2
            + [GUARD] * 6
            + [PRIEST] * 2
            + [BARON] * 2
            + [HANDMAID] * 2
            + [PRINCE] * 2
            + [CHANCELLOR] * 2
            + [KING] * 1
            + [COUNTESS] * 1
            + [PRINCESS] * 1
        )
        random.shuffle(deck)
        return deck

    @staticmethod
    def setup_round(room: Room) -> None:
        for p in room.players:
            p.hand = []
            p.discard_pile = []
            p.eliminated = False
            p.protected = False
            p.played_spy = False

        deck = GameEngine.build_deck()
        room.deck = deck
        room.aside_card = deck.pop()
        room.face_up_cards = []

        if len(room.players) == 2:
            for _ in range(3):
                room.face_up_cards.append(deck.pop())

        for p in room.players:
            p.hand.append(deck.pop())

        room.phase = "playing"
        room.pending_action = None
        room.round_winner_ids = []

    @staticmethod
    def validate_play(room: Room, player: Player, card_value: int) -> Optional[str]:
        if card_value not in player.hand:
            return "CARD_NOT_IN_HAND"
        # Countess forced play
        if COUNTESS in player.hand:
            if card_value != COUNTESS and (KING in player.hand or PRINCE in player.hand):
                return "MUST_PLAY_COUNTESS"
        return None

    @staticmethod
    def compute_valid_targets(room: Room, acting: Player, card_value: int) -> list[str]:
        others = [p for p in room.players if p.player_id != acting.player_id and not p.eliminated]
        unprotected_others = [p for p in others if not p.protected]

        if card_value == PRINCE:
            # Prince can target self; if all others protected, must self-target
            if not unprotected_others:
                return [acting.player_id]
            return [p.player_id for p in unprotected_others] + [acting.player_id]

        # Guard / Priest / Baron / King: only others, unprotected
        return [p.player_id for p in unprotected_others]

    @staticmethod
    def _get_player(room: Room, player_id: str) -> Optional[Player]:
        for p in room.players:
            if p.player_id == player_id:
                return p
        return None

    @staticmethod
    def resolve_card(room: Room, acting: Player, card_value: int, target: Optional[Player] = None) -> list[dict]:
        """Resolve card effect. Returns list of game events."""
        events: list[dict] = []

        if card_value == SPY:
            acting.played_spy = True
            events.append({"event_type": "card_played", "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "card": card_value,
                "card_name": CARD_NAMES[card_value],
                "description": f"{acting.name} が Spy をプレイしました。",
            }})

        elif card_value == PRIEST:
            events.append({"event_type": "priest_reveal", "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "target_id": target.player_id,
                "target_name": target.name,
                "target_hand": target.hand[:],
                "description": f"{acting.name} が {target.name} の手札を確認しました。",
            }})

        elif card_value == BARON:
            actor_val = acting.hand[0] if acting.hand else -1
            target_val = target.hand[0] if target.hand else -1
            if actor_val > target_val:
                target.eliminated = True
                target.discard_pile.extend(target.hand)
                target.hand = []
                events.append({"event_type": "baron_compare", "data": {
                    "player_id": acting.player_id,
                    "player_name": acting.name,
                    "target_id": target.player_id,
                    "target_name": target.name,
                    "actor_val": actor_val,
                    "target_val": target_val,
                    "loser_id": target.player_id,
                    "description": f"{acting.name}({actor_val}) vs {target.name}({target_val}) → {target.name} が脱落！",
                }})
            elif actor_val < target_val:
                acting.eliminated = True
                acting.discard_pile.extend(acting.hand)
                acting.hand = []
                events.append({"event_type": "baron_compare", "data": {
                    "player_id": acting.player_id,
                    "player_name": acting.name,
                    "target_id": target.player_id,
                    "target_name": target.name,
                    "actor_val": actor_val,
                    "target_val": target_val,
                    "loser_id": acting.player_id,
                    "description": f"{acting.name}({actor_val}) vs {target.name}({target_val}) → {acting.name} が脱落！",
                }})
            else:
                events.append({"event_type": "baron_compare", "data": {
                    "player_id": acting.player_id,
                    "player_name": acting.name,
                    "target_id": target.player_id,
                    "target_name": target.name,
                    "actor_val": actor_val,
                    "target_val": target_val,
                    "loser_id": None,
                    "description": f"{acting.name}({actor_val}) vs {target.name}({target_val}) → 引き分け、脱落なし。",
                }})

        elif card_value == HANDMAID:
            acting.protected = True
            events.append({"event_type": "card_played", "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "card": card_value,
                "card_name": CARD_NAMES[card_value],
                "description": f"{acting.name} が Handmaid をプレイ。次のターン開始まで保護されます。",
            }})

        elif card_value == PRINCE:
            discarded = target.hand[0]
            target.discard_pile.append(discarded)
            target.hand = []
            if discarded == PRINCESS:
                target.eliminated = True
                events.append({"event_type": "prince_discard", "data": {
                    "player_id": acting.player_id,
                    "player_name": acting.name,
                    "target_id": target.player_id,
                    "target_name": target.name,
                    "discarded": discarded,
                    "description": f"{acting.name} が {target.name} に Prince をプレイ → Princess を捨て、{target.name} が脱落！",
                }})
            else:
                if room.deck:
                    new_card = room.deck.pop()
                elif room.aside_card != -1:
                    new_card = room.aside_card
                    room.aside_card = -1
                else:
                    new_card = None
                if new_card is not None:
                    target.hand.append(new_card)
                events.append({"event_type": "prince_discard", "data": {
                    "player_id": acting.player_id,
                    "player_name": acting.name,
                    "target_id": target.player_id,
                    "target_name": target.name,
                    "discarded": discarded,
                    "description": f"{acting.name} が {target.name} に Prince をプレイ → {CARD_NAMES[discarded]} を捨て、新しいカードを引きました。",
                }})

        elif card_value == KING:
            acting_card = acting.hand[0] if acting.hand else None
            target_card = target.hand[0] if target.hand else None
            if acting_card is not None and target_card is not None:
                acting.hand[0] = target_card
                target.hand[0] = acting_card
            events.append({"event_type": "king_swap", "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "target_id": target.player_id,
                "target_name": target.name,
                "description": f"{acting.name} と {target.name} が手札を交換しました。",
            }})

        elif card_value == COUNTESS:
            events.append({"event_type": "card_played", "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "card": card_value,
                "card_name": CARD_NAMES[card_value],
                "description": f"{acting.name} が Countess をプレイしました。",
            }})

        elif card_value == PRINCESS:
            acting.eliminated = True
            acting.discard_pile.extend(acting.hand)
            acting.hand = []
            events.append({"event_type": "princess_played", "data": {
                "player_id": acting.player_id,
                "player_name": acting.name,
                "description": f"{acting.name} が Princess をプレイ → 即脱落！",
            }})

        return events

    @staticmethod
    def resolve_guard(room: Room, acting: Player, target: Player, guessed_value: int) -> list[dict]:
        hit = bool(target.hand and target.hand[0] == guessed_value)
        if hit:
            target.eliminated = True
            target.discard_pile.extend(target.hand)
            target.hand = []
            desc = f"{acting.name} が Guard で {target.name} の手札を {CARD_NAMES[guessed_value]} と推測 → 当たり！{target.name} が脱落！"
        else:
            desc = f"{acting.name} が Guard で {target.name} の手札を {CARD_NAMES[guessed_value]} と推測 → 外れ。"
        return [{"event_type": "guard_guess", "data": {
            "player_id": acting.player_id,
            "player_name": acting.name,
            "target_id": target.player_id,
            "target_name": target.name,
            "guessed_value": guessed_value,
            "hit": hit,
            "description": desc,
        }}]

    @staticmethod
    def check_round_end(room: Room) -> bool:
        alive = [p for p in room.players if not p.eliminated]
        if len(alive) <= 1:
            return True
        if not room.deck:
            return True
        return False

    @staticmethod
    def evaluate_round_winner(room: Room) -> list[str]:
        alive = [p for p in room.players if not p.eliminated]
        if not alive:
            return []
        max_val = max(p.hand[0] if p.hand else -1 for p in alive)
        winners = [p for p in alive if (p.hand[0] if p.hand else -1) == max_val]
        return [p.player_id for p in winners]

    @staticmethod
    def award_tokens(room: Room, winner_ids: list[str]) -> dict:
        spy_bonus_id = None
        spy_survivors = [p for p in room.players if p.played_spy and not p.eliminated]
        if len(spy_survivors) == 1:
            spy_survivors[0].tokens += 1
            spy_bonus_id = spy_survivors[0].player_id

        for p in room.players:
            if p.player_id in winner_ids:
                p.tokens += 1

        return {"spy_bonus_id": spy_bonus_id}

    @staticmethod
    def check_game_winner(room: Room) -> list[str]:
        winners = [p for p in room.players if p.tokens >= room.tokens_to_win]
        return [p.player_id for p in winners]

    @staticmethod
    def advance_turn(room: Room) -> None:
        """Move to next non-eliminated player, clear their Handmaid protection, draw a card."""
        alive = [p for p in room.players if not p.eliminated]
        if not alive:
            return
        n = len(room.players)
        idx = room.current_player_idx
        for _ in range(n):
            idx = (idx + 1) % n
            if not room.players[idx].eliminated:
                break
        room.current_player_idx = idx
        room.players[idx].protected = False

        # Draw card for new current player
        if room.deck:
            room.players[idx].hand.append(room.deck.pop())
        elif room.aside_card != -1:
            room.players[idx].hand.append(room.aside_card)
            room.aside_card = -1

        room.pending_action = None


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
            if pa.phase == "await_chancellor_return" and pa.acting_player_id == viewer_id:
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
    await ws.send_json({"type": "room_created", "room_id": room_id, "player_id": player_id})
    await manager.broadcast_state(room_id)
    return room_id


async def handle_join(ws: WebSocket, data: dict) -> None:
    player_id = data["player_id"]
    name = data.get("name", "Player")
    room_id = data["room_id"].upper()
    room = manager.rooms.get(room_id)

    if not room:
        await ws.send_json({"type": "error", "code": "ROOM_NOT_FOUND", "message": "ルームが見つかりません"})
        return

    existing = GameEngine._get_player(room, player_id)
    if existing:
        existing.is_connected = True
        await manager.connect(ws, room_id, player_id)
        await ws.send_json({"type": "room_created", "room_id": room_id, "player_id": player_id})
        await manager.broadcast_state(room_id)
        return

    if room.phase != "lobby":
        await ws.send_json({"type": "error", "code": "GAME_IN_PROGRESS", "message": "ゲームは既に開始されています"})
        return

    if len(room.players) >= 6:
        await ws.send_json({"type": "error", "code": "ROOM_FULL", "message": "ルームが満員です"})
        return

    player = Player(player_id=player_id, name=name)
    room.players.append(player)
    await manager.connect(ws, room_id, player_id)
    await manager.broadcast(room_id, {"type": "player_joined", "player_id": player_id, "name": name})
    await manager.broadcast_state(room_id)


async def handle_start_game(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    room = manager.rooms.get(room_id)

    if not room:
        await ws.send_json({"type": "error", "code": "ROOM_NOT_FOUND", "message": "ルームが見つかりません"})
        return
    if player_id != room.host_id:
        await ws.send_json({"type": "error", "code": "NOT_HOST", "message": "ホストのみ開始できます"})
        return
    if len(room.players) < 2:
        await ws.send_json({"type": "error", "code": "NOT_ENOUGH_PLAYERS", "message": "2人以上必要です"})
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
        await ws.send_json({"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"})
        return

    current = room.players[room.current_player_idx]
    if current.player_id != player_id:
        await ws.send_json({"type": "error", "code": "NOT_YOUR_TURN", "message": "あなたのターンではありません"})
        return
    if room.pending_action:
        await ws.send_json({"type": "error", "code": "PENDING_ACTION", "message": "アクションが保留中です"})
        return

    if card_index < 0 or card_index >= len(current.hand):
        await ws.send_json({"type": "error", "code": "INVALID_CARD", "message": "無効なカードインデックスです"})
        return

    card_value = current.hand[card_index]
    err = GameEngine.validate_play(room, current, card_value)
    if err:
        await ws.send_json({"type": "error", "code": err, "message": f"プレイ不可: {err}"})
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
            await manager.broadcast(room_id, {"type": "game_event", "event_type": "card_played", "data": {
                "player_id": current.player_id,
                "player_name": current.name,
                "card": card_value,
                "card_name": CARD_NAMES[card_value],
                "description": f"{current.name} が Chancellor をプレイしました（引けるカードなし）。",
            }})
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
        await manager.send_to(room_id, current.player_id, {
            "type": "await_input",
            "action_type": "chancellor_return",
            "card_played": card_value,
            "chancellor_hand": chancellor_hand,
            "extra": {},
        })
        return

    # Cards needing a target
    candidates = GameEngine.compute_valid_targets(room, current, card_value)

    if not candidates:
        # Fizzle
        await manager.broadcast(room_id, {"type": "game_event", "event_type": "fizzle", "data": {
            "player_id": current.player_id,
            "player_name": current.name,
            "card": card_value,
            "card_name": CARD_NAMES[card_value],
            "description": f"{current.name} の {CARD_NAMES[card_value]} は全員保護されているため不発でした。",
        }})
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
    await manager.send_to(room_id, current.player_id, {
        "type": "await_input",
        "action_type": "select_target",
        "candidates": candidates,
        "card_played": card_value,
        "extra": {},
    })


async def handle_select_target(ws: WebSocket, data: dict) -> None:
    room_id = data["room_id"]
    player_id = data["player_id"]
    target_id = data["target_id"]
    room = manager.rooms.get(room_id)

    if not room or not room.pending_action:
        await ws.send_json({"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"})
        return

    pa = room.pending_action
    if pa.acting_player_id != player_id or pa.phase != "await_target":
        await ws.send_json({"type": "error", "code": "NOT_YOUR_ACTION", "message": "あなたのアクションではありません"})
        return
    if target_id not in pa.candidate_target_ids:
        await ws.send_json({"type": "error", "code": "INVALID_TARGET", "message": "無効なターゲットです"})
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
        await manager.send_to(room_id, player_id, {
            "type": "await_input",
            "action_type": "guard_guess",
            "card_played": card_value,
            "target_id": target_id,
            "target_name": target.name,
            "extra": {},
        })
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
        await ws.send_json({"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"})
        return

    pa = room.pending_action
    if pa.acting_player_id != player_id or pa.phase != "await_guard_guess":
        await ws.send_json({"type": "error", "code": "NOT_YOUR_ACTION", "message": "あなたのアクションではありません"})
        return
    if guessed_value == GUARD:
        await ws.send_json({"type": "error", "code": "INVALID_GUESS", "message": "Guardは推測できません"})
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
        await ws.send_json({"type": "error", "code": "INVALID_STATE", "message": "無効な状態です"})
        return

    pa = room.pending_action
    if pa.acting_player_id != player_id or pa.phase != "await_chancellor_return":
        await ws.send_json({"type": "error", "code": "NOT_YOUR_ACTION", "message": "あなたのアクションではありません"})
        return

    chancellor_hand = pa.chancellor_hand
    if kept_card not in chancellor_hand:
        await ws.send_json({"type": "error", "code": "INVALID_CARD", "message": "無効なカードです"})
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
    await manager.broadcast(room_id, {"type": "game_event", "event_type": "chancellor_used", "data": {
        "player_id": acting.player_id,
        "player_name": acting.name,
        "description": f"{acting.name} が Chancellor をプレイ、手札を選び直しました。",
    }})

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
        await ws.send_json({"type": "error", "code": "NOT_HOST", "message": "ホストのみ次ラウンドを開始できます"})
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
        {"player_id": p.player_id, "name": p.name, "hand": p.hand}
        for p in room.players
    ]
    token_updates = [
        {"player_id": p.player_id, "name": p.name, "tokens": p.tokens}
        for p in room.players
    ]

    await manager.broadcast(room_id, {
        "type": "round_end",
        "round_winner_ids": winner_ids,
        "spy_bonus_id": award_info["spy_bonus_id"],
        "revealed_hands": revealed,
        "token_updates": token_updates,
    })

    game_winners = GameEngine.check_game_winner(room)
    if game_winners:
        room.winner_ids = game_winners
        room.phase = "game_over"
        await manager.broadcast(room_id, {"type": "game_over", "winner_ids": game_winners})

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
                await ws.send_json({"type": "error", "code": "NO_ROOM", "message": "ルームに参加していません"})
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
            await ws.send_json({"type": "error", "code": "SERVER_ERROR", "message": str(e)})
        except Exception:
            pass
        if room_id and player_id:
            manager.disconnect(room_id, player_id)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
