from __future__ import annotations

import asyncio
import logging
import random
import string
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import ai_agent
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Connection Manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    def __init__(self):
        """ルーム状態・接続情報・ルームロックを保持する管理オブジェクトを初期化する。"""
        self.rooms: dict[str, Room] = {}
        self.connections: dict[str, dict[str, WebSocket]] = {}
        self.room_locks: dict[str, asyncio.Lock] = {}

    def _gen_room_id(self) -> str:
        """既存ルームと重複しない4文字のルームIDを生成する。"""
        while True:
            rid = "".join(random.choices(string.ascii_uppercase, k=4))
            if rid not in self.rooms:
                return rid

    async def connect(self, ws: WebSocket, room_id: str, player_id: str) -> None:
        """指定ルームのプレイヤーにWebSocket接続を紐づける。"""
        if room_id not in self.connections:
            self.connections[room_id] = {}
        self.connections[room_id][player_id] = ws

    def disconnect(self, room_id: str, player_id: str) -> None:
        """接続情報を破棄し、プレイヤーの接続状態を切断に更新する。"""
        if room_id in self.connections:
            self.connections[room_id].pop(player_id, None)
        room = self.rooms.get(room_id)
        if room:
            p = GameEngine._get_player(room, player_id)
            if p:
                p.is_connected = False

    async def send_to(self, room_id: str, player_id: str, msg: dict) -> None:
        """特定プレイヤーへメッセージを送信する。"""
        ws = self.connections.get(room_id, {}).get(player_id)
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    async def broadcast(self, room_id: str, msg: dict) -> None:
        """ルーム内の接続中プレイヤー全員へメッセージを送信する。"""
        for pid, ws in list(self.connections.get(room_id, {}).items()):
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    async def broadcast_state(self, room_id: str) -> None:
        """ルーム内の各プレイヤー視点で状態を組み立てて一斉送信する。"""
        room = self.rooms.get(room_id)
        if not room:
            return
        for pid, ws in list(self.connections.get(room_id, {}).items()):
            try:
                await ws.send_json(self._build_state(room, pid))
            except Exception:
                pass

    def _build_state(self, room: Room, viewer_id: str) -> dict:
        """閲覧者ごとの秘匿情報を反映した状態ペイロードを生成する。"""
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
                "is_ai": p.is_ai,
                "ai_provider": p.ai_provider,
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
        """ルーム単位の排他制御用ロックを取得または生成する。"""
        if room_id not in self.room_locks:
            self.room_locks[room_id] = asyncio.Lock()
        return self.room_locks[room_id]


manager = ConnectionManager()
ACTION_LOG_LIMIT = 40
MAX_AI_STEPS_PER_TICK = 80


class _NoopWebSocket:
    """AI内部実行用: エラー受信だけ行うダミーWebSocket。"""

    def __init__(self) -> None:
        self.last_error: Optional[dict] = None

    async def send_json(self, msg: dict) -> None:
        if msg.get("type") == "error":
            self.last_error = msg


def _append_action_log(room: Room, message: str) -> None:
    text = message.strip()
    if not text:
        return
    room.action_log.append(text)
    if len(room.action_log) > ACTION_LOG_LIMIT:
        del room.action_log[: len(room.action_log) - ACTION_LOG_LIMIT]


async def _broadcast_game_event(room_id: str, event: dict) -> None:
    room = manager.rooms.get(room_id)
    if room:
        description = event.get("data", {}).get("description")
        if isinstance(description, str):
            _append_action_log(room, description)
    await manager.broadcast(room_id, {"type": "game_event", **event})


def _legal_card_indices(room: Room, player: Player) -> list[int]:
    legal: list[int] = []
    for idx, card_value in enumerate(player.hand):
        if GameEngine.validate_play(room, player, card_value) is None:
            legal.append(idx)
    return legal


def _next_ai_name(room: Room, provider: str) -> str:
    existing = {
        p.name.strip().lower()
        for p in room.players
        if p.is_ai and p.ai_provider == provider
    }
    base = f"{provider.capitalize()} AI"
    if base.lower() not in existing:
        return base
    i = 2
    while True:
        candidate = f"{base} {i}"
        if candidate.lower() not in existing:
            return candidate
        i += 1


async def _run_ai_play_card(
    room_id: str, room: Room, acting: Player, sink: _NoopWebSocket
) -> None:
    legal_indices = _legal_card_indices(room, acting)
    if not legal_indices:
        _append_action_log(room, f"{acting.name} は合法手がなく行動できませんでした。")
        return

    chosen_index, fallback = await asyncio.to_thread(
        ai_agent.choose_card_index,
        room,
        acting,
        legal_indices,
        room.action_log[:],
    )
    if chosen_index not in legal_indices:
        chosen_index = random.choice(legal_indices)
        fallback = True
    if fallback:
        _append_action_log(
            room, f"{acting.name} のAI判断に失敗したためランダムに行動しました。"
        )

    await handle_play_card(
        sink,
        {
            "room_id": room_id,
            "player_id": acting.player_id,
            "card_index": chosen_index,
        },
    )


async def _run_ai_pending_action(
    room_id: str, room: Room, acting: Player, sink: _NoopWebSocket
) -> None:
    pa = room.pending_action
    if not pa:
        return

    if pa.phase == "await_target":
        candidates = pa.candidate_target_ids[:]
        if not candidates:
            return
        target_id, fallback = await asyncio.to_thread(
            ai_agent.choose_target_id,
            room,
            acting,
            pa.card_played,
            candidates,
            room.action_log[:],
        )
        if target_id not in candidates:
            target_id = random.choice(candidates)
            fallback = True
        if fallback:
            _append_action_log(
                room, f"{acting.name} のAI判断に失敗したためランダムに行動しました。"
            )
        await handle_select_target(
            sink,
            {
                "room_id": room_id,
                "player_id": acting.player_id,
                "target_id": target_id,
            },
        )
        return

    if pa.phase == "await_guard_guess":
        guessed_value, fallback = await asyncio.to_thread(
            ai_agent.choose_guard_guess, room, acting, room.action_log[:]
        )
        if guessed_value == GUARD:
            guessed_value = random.choice([v for v in range(10) if v != GUARD])
            fallback = True
        if fallback:
            _append_action_log(
                room, f"{acting.name} のAI判断に失敗したためランダムに行動しました。"
            )
        await handle_guard_guess(
            sink,
            {
                "room_id": room_id,
                "player_id": acting.player_id,
                "guessed_value": guessed_value,
            },
        )
        return

    if pa.phase == "await_chancellor_return":
        chancellor_hand = pa.chancellor_hand[:]
        if not chancellor_hand:
            return
        kept_index, bottom_order_indices, fallback = await asyncio.to_thread(
            ai_agent.choose_chancellor_return,
            room,
            acting,
            chancellor_hand,
            room.action_log[:],
        )
        if not (0 <= kept_index < len(chancellor_hand)):
            kept_index = random.randrange(len(chancellor_hand))
            fallback = True
        expected = [i for i in range(len(chancellor_hand)) if i != kept_index]
        if sorted(bottom_order_indices) != sorted(expected):
            bottom_order_indices = expected
            fallback = True
        if fallback:
            _append_action_log(
                room, f"{acting.name} のAI判断に失敗したためランダムに行動しました。"
            )
        bottom_order = [chancellor_hand[idx] for idx in bottom_order_indices]
        await handle_chancellor_return(
            sink,
            {
                "room_id": room_id,
                "player_id": acting.player_id,
                "kept_card": chancellor_hand[kept_index],
                "bottom_order": bottom_order,
            },
        )
        return


async def run_ai_until_human_turn(room_id: str) -> None:
    room = manager.rooms.get(room_id)
    if not room or room.phase != "playing":
        return

    sink = _NoopWebSocket()
    for _ in range(MAX_AI_STEPS_PER_TICK):
        room = manager.rooms.get(room_id)
        if not room or room.phase != "playing":
            return

        acting: Optional[Player] = None
        if room.pending_action:
            acting = GameEngine._get_player(room, room.pending_action.acting_player_id)
        elif room.players:
            acting = room.players[room.current_player_idx]

        if not acting or not acting.is_ai:
            return

        if room.pending_action:
            await _run_ai_pending_action(room_id, room, acting, sink)
        else:
            await _run_ai_play_card(room_id, room, acting, sink)

        if sink.last_error:
            _append_action_log(
                room,
                f"{acting.name} のAI処理でエラー: {sink.last_error.get('code', 'UNKNOWN')}",
            )
            sink.last_error = None
    _append_action_log(room, "AI処理がステップ上限に到達したため中断しました。")


# ---------------------------------------------------------------------------
# Message Handlers
# ---------------------------------------------------------------------------


async def handle_create_room(ws: WebSocket, data: dict) -> str:
    """ルーム作成要求を処理してホストを参加させ、初期状態を配信する。"""
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
    """ルーム参加または再接続要求を処理する。"""
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


async def handle_add_ai_player(ws: WebSocket, data: dict) -> None:
    """ホストがロビーにAIプレイヤーを追加する。"""
    room_id = data["room_id"].upper()
    host_id = data["player_id"]
    provider = ai_agent.normalize_provider(data.get("provider"))
    name = str(data.get("name", "")).strip()
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
    if host_id != room.host_id:
        await ws.send_json(
            {"type": "error", "code": "NOT_HOST", "message": "ホストのみ追加できます"}
        )
        return
    if room.phase != "lobby":
        await ws.send_json(
            {
                "type": "error",
                "code": "GAME_IN_PROGRESS",
                "message": "ゲーム開始後は追加できません",
            }
        )
        return
    if len(room.players) >= 6:
        await ws.send_json(
            {"type": "error", "code": "ROOM_FULL", "message": "ルームが満員です"}
        )
        return
    if not provider:
        await ws.send_json(
            {
                "type": "error",
                "code": "INVALID_PROVIDER",
                "message": "provider は openai/anthropic/gemini/xai のいずれかです",
            }
        )
        return

    ai_player_id = f"ai-{uuid.uuid4().hex[:10]}"
    if not name:
        name = _next_ai_name(room, provider)

    ai_player = Player(
        player_id=ai_player_id,
        name=name,
        is_connected=True,
        is_ai=True,
        ai_provider=provider,
    )
    room.players.append(ai_player)

    await manager.broadcast(
        room_id,
        {
            "type": "player_joined",
            "player_id": ai_player_id,
            "name": name,
            "is_ai": True,
            "ai_provider": provider,
        },
    )
    await manager.broadcast_state(room_id)


async def handle_start_game(ws: WebSocket, data: dict) -> None:
    """ゲーム開始要求を検証し、ラウンド初期化を実行する。"""
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
    starter = room.players[room.current_player_idx] if room.players else None
    if starter:
        _append_action_log(room, f"ラウンド開始: 先手は {starter.name}")
    await manager.broadcast_state(room_id)


async def handle_play_card(ws: WebSocket, data: dict) -> None:
    """カードプレイ要求を処理し、必要に応じて追加入力待ち状態へ遷移させる。"""
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
            await _broadcast_game_event(room_id, ev)
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
            await _broadcast_game_event(
                room_id,
                {
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
        await _broadcast_game_event(
            room_id,
            {
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
    """対象選択入力を処理し、効果解決または追加入力待ちへ進める。"""
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
        await _broadcast_game_event(room_id, ev)

    if GameEngine.check_round_end(room):
        await _end_round(room_id)
        return
    GameEngine.advance_turn(room)
    await manager.broadcast_state(room_id)


async def handle_guard_guess(ws: WebSocket, data: dict) -> None:
    """Guardの推測入力を処理して効果を解決する。"""
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
        await _broadcast_game_event(room_id, ev)

    if GameEngine.check_round_end(room):
        await _end_round(room_id)
        return
    GameEngine.advance_turn(room)
    await manager.broadcast_state(room_id)


async def handle_chancellor_return(ws: WebSocket, data: dict) -> None:
    """Chancellor後の手札選択・戻し順入力を処理する。"""
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
    await _broadcast_game_event(
        room_id,
        {
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
    """次ラウンド開始要求を処理し、先手決定後にラウンドを再初期化する。"""
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
    starter = room.players[room.current_player_idx] if room.players else None
    if starter:
        _append_action_log(room, f"次ラウンド開始: 先手は {starter.name}")
    await manager.broadcast_state(room_id)


async def _end_round(room_id: str) -> None:
    """ラウンド終了処理を実行し、必要ならゲーム終了へ遷移させる。"""
    room = manager.rooms.get(room_id)
    if not room:
        return

    room.phase = "round_end"
    winner_ids = GameEngine.evaluate_round_winner(room)
    room.round_winner_ids = winner_ids
    award_info = GameEngine.award_tokens(room, winner_ids)
    winner_names = [p.name for p in room.players if p.player_id in set(winner_ids)] or [
        "（勝者なし）"
    ]
    _append_action_log(room, f"ラウンド終了: {', '.join(winner_names)} が勝利")
    if award_info["spy_bonus_id"]:
        spy_player = GameEngine._get_player(room, award_info["spy_bonus_id"])
        spy_name = spy_player.name if spy_player else award_info["spy_bonus_id"]
        _append_action_log(room, f"Spyボーナス: {spy_name} +1トークン")

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
        game_winner_names = [
            p.name for p in room.players if p.player_id in set(game_winners)
        ] or ["（勝者なし）"]
        _append_action_log(room, f"ゲーム終了: {', '.join(game_winner_names)} が優勝")
        await manager.broadcast(
            room_id, {"type": "game_over", "winner_ids": game_winners}
        )

    await manager.broadcast_state(room_id)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.get("/api/ai-providers")
async def list_ai_providers():
    """APIキーが設定済みのAIプロバイダ一覧を返す。"""
    available = [
        p for p in ai_agent.SUPPORTED_AI_PROVIDERS if ai_agent._provider_has_key(p)
    ]
    return {"providers": available}


@app.get("/api/rooms")
async def list_rooms():
    """入室可能なルーム（ロビー状態かつ満員でない）一覧を返す。"""
    rooms = []
    for room in manager.rooms.values():
        if room.phase != "lobby":
            continue
        if len(room.players) >= 6:
            continue

        host = GameEngine._get_player(room, room.host_id)
        rooms.append(
            {
                "room_id": room.room_id,
                "host_name": host.name
                if host
                else room.players[0].name
                if room.players
                else "",
                "player_count": len(room.players),
            }
        )

    rooms.sort(key=lambda r: r["room_id"])
    return {"rooms": rooms}


@app.get("/")
async def index():
    """フロントエンドのエントリHTMLを返す。"""
    return FileResponse("static/index.html")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocketメッセージを受信して各種ハンドラへ振り分ける。"""
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
                    await run_ai_until_human_turn(room_id)
                elif msg_type == "add_ai_player":
                    await handle_add_ai_player(ws, data)
                elif msg_type == "play_card":
                    await handle_play_card(ws, data)
                    await run_ai_until_human_turn(room_id)
                elif msg_type == "select_target":
                    await handle_select_target(ws, data)
                    await run_ai_until_human_turn(room_id)
                elif msg_type == "guard_guess":
                    await handle_guard_guess(ws, data)
                    await run_ai_until_human_turn(room_id)
                elif msg_type == "chancellor_return":
                    await handle_chancellor_return(ws, data)
                    await run_ai_until_human_turn(room_id)
                elif msg_type == "next_round":
                    await handle_next_round(ws, data)
                    await run_ai_until_human_turn(room_id)

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
