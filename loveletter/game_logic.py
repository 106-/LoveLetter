from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

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
        """ゲームで使用するカード山札を構築してシャッフルして返す。"""
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
        """ラウンド開始状態を初期化し、配札と先手の初回ドローを行う。"""
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

        # The starting player must begin their turn with 2 cards.
        if room.players:
            idx = room.current_player_idx % len(room.players)
            if room.deck:
                room.players[idx].hand.append(room.deck.pop())
            elif room.aside_card != -1:
                room.players[idx].hand.append(room.aside_card)
                room.aside_card = -1

        room.phase = "playing"
        room.pending_action = None
        room.round_winner_ids = []

    @staticmethod
    def validate_play(room: Room, player: Player, card_value: int) -> Optional[str]:
        """指定カードがプレイ可能かを検証し、不可の場合はエラーコードを返す。"""
        if card_value not in player.hand:
            return "CARD_NOT_IN_HAND"
        # Countess forced play
        if COUNTESS in player.hand:
            if card_value != COUNTESS and (
                KING in player.hand or PRINCE in player.hand
            ):
                return "MUST_PLAY_COUNTESS"
        return None

    @staticmethod
    def compute_valid_targets(room: Room, acting: Player, card_value: int) -> list[str]:
        """カード効果に応じた有効な対象プレイヤーID一覧を返す。"""
        others = [
            p
            for p in room.players
            if p.player_id != acting.player_id and not p.eliminated
        ]
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
        """ルーム内からプレイヤーIDに一致するプレイヤーを取得する。"""
        for p in room.players:
            if p.player_id == player_id:
                return p
        return None

    @staticmethod
    def resolve_card(
        room: Room, acting: Player, card_value: int, target: Optional[Player] = None
    ) -> list[dict]:
        """Guard以外のカード効果を解決し、発生したゲームイベント一覧を返す。"""
        events: list[dict] = []

        if card_value == SPY:
            acting.played_spy = True
            events.append(
                {
                    "event_type": "card_played",
                    "data": {
                        "player_id": acting.player_id,
                        "player_name": acting.name,
                        "card": card_value,
                        "card_name": CARD_NAMES[card_value],
                        "description": f"{acting.name} が Spy をプレイしました。",
                    },
                }
            )

        elif card_value == PRIEST:
            events.append(
                {
                    "event_type": "priest_reveal",
                    "data": {
                        "player_id": acting.player_id,
                        "player_name": acting.name,
                        "target_id": target.player_id,
                        "target_name": target.name,
                        "target_hand": target.hand[:],
                        "description": f"{acting.name} が {target.name} の手札を確認しました。",
                    },
                }
            )

        elif card_value == BARON:
            actor_val = acting.hand[0] if acting.hand else -1
            target_val = target.hand[0] if target.hand else -1
            if actor_val > target_val:
                target.eliminated = True
                target.discard_pile.extend(target.hand)
                target.hand = []
                events.append(
                    {
                        "event_type": "baron_compare",
                        "data": {
                            "player_id": acting.player_id,
                            "player_name": acting.name,
                            "target_id": target.player_id,
                            "target_name": target.name,
                            "actor_val": actor_val,
                            "target_val": target_val,
                            "loser_id": target.player_id,
                            "description": f"{acting.name}({actor_val}) vs {target.name}({target_val}) → {target.name} が脱落！",
                        },
                    }
                )
            elif actor_val < target_val:
                acting.eliminated = True
                acting.discard_pile.extend(acting.hand)
                acting.hand = []
                events.append(
                    {
                        "event_type": "baron_compare",
                        "data": {
                            "player_id": acting.player_id,
                            "player_name": acting.name,
                            "target_id": target.player_id,
                            "target_name": target.name,
                            "actor_val": actor_val,
                            "target_val": target_val,
                            "loser_id": acting.player_id,
                            "description": f"{acting.name}({actor_val}) vs {target.name}({target_val}) → {acting.name} が脱落！",
                        },
                    }
                )
            else:
                events.append(
                    {
                        "event_type": "baron_compare",
                        "data": {
                            "player_id": acting.player_id,
                            "player_name": acting.name,
                            "target_id": target.player_id,
                            "target_name": target.name,
                            "actor_val": actor_val,
                            "target_val": target_val,
                            "loser_id": None,
                            "description": f"{acting.name}({actor_val}) vs {target.name}({target_val}) → 引き分け、脱落なし。",
                        },
                    }
                )

        elif card_value == HANDMAID:
            acting.protected = True
            events.append(
                {
                    "event_type": "card_played",
                    "data": {
                        "player_id": acting.player_id,
                        "player_name": acting.name,
                        "card": card_value,
                        "card_name": CARD_NAMES[card_value],
                        "description": f"{acting.name} が Handmaid をプレイ。次のターン開始まで保護されます。",
                    },
                }
            )

        elif card_value == PRINCE:
            discarded = target.hand[0]
            target.discard_pile.append(discarded)
            target.hand = []
            if discarded == PRINCESS:
                target.eliminated = True
                events.append(
                    {
                        "event_type": "prince_discard",
                        "data": {
                            "player_id": acting.player_id,
                            "player_name": acting.name,
                            "target_id": target.player_id,
                            "target_name": target.name,
                            "discarded": discarded,
                            "description": f"{acting.name} が {target.name} に Prince をプレイ → Princess を捨て、{target.name} が脱落！",
                        },
                    }
                )
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
                events.append(
                    {
                        "event_type": "prince_discard",
                        "data": {
                            "player_id": acting.player_id,
                            "player_name": acting.name,
                            "target_id": target.player_id,
                            "target_name": target.name,
                            "discarded": discarded,
                            "description": f"{acting.name} が {target.name} に Prince をプレイ → {CARD_NAMES[discarded]} を捨て、新しいカードを引きました。",
                        },
                    }
                )

        elif card_value == KING:
            acting_card = acting.hand[0] if acting.hand else None
            target_card = target.hand[0] if target.hand else None
            if acting_card is not None and target_card is not None:
                acting.hand[0] = target_card
                target.hand[0] = acting_card
            events.append(
                {
                    "event_type": "king_swap",
                    "data": {
                        "player_id": acting.player_id,
                        "player_name": acting.name,
                        "target_id": target.player_id,
                        "target_name": target.name,
                        "description": f"{acting.name} と {target.name} が手札を交換しました。",
                    },
                }
            )

        elif card_value == COUNTESS:
            events.append(
                {
                    "event_type": "card_played",
                    "data": {
                        "player_id": acting.player_id,
                        "player_name": acting.name,
                        "card": card_value,
                        "card_name": CARD_NAMES[card_value],
                        "description": f"{acting.name} が Countess をプレイしました。",
                    },
                }
            )

        elif card_value == PRINCESS:
            acting.eliminated = True
            acting.discard_pile.extend(acting.hand)
            acting.hand = []
            events.append(
                {
                    "event_type": "princess_played",
                    "data": {
                        "player_id": acting.player_id,
                        "player_name": acting.name,
                        "description": f"{acting.name} が Princess をプレイ → 即脱落！",
                    },
                }
            )

        return events

    @staticmethod
    def resolve_guard(
        room: Room, acting: Player, target: Player, guessed_value: int
    ) -> list[dict]:
        """Guardの推測処理を解決し、結果イベントを返す。"""
        hit = bool(target.hand and target.hand[0] == guessed_value)
        if hit:
            target.eliminated = True
            target.discard_pile.extend(target.hand)
            target.hand = []
            desc = f"{acting.name} が Guard で {target.name} の手札を {CARD_NAMES[guessed_value]} と推測 → 当たり！{target.name} が脱落！"
        else:
            desc = f"{acting.name} が Guard で {target.name} の手札を {CARD_NAMES[guessed_value]} と推測 → 外れ。"
        return [
            {
                "event_type": "guard_guess",
                "data": {
                    "player_id": acting.player_id,
                    "player_name": acting.name,
                    "target_id": target.player_id,
                    "target_name": target.name,
                    "guessed_value": guessed_value,
                    "hit": hit,
                    "description": desc,
                },
            }
        ]

    @staticmethod
    def check_round_end(room: Room) -> bool:
        """ラウンド終了条件（生存者1人以下または山札枯渇）を判定する。"""
        alive = [p for p in room.players if not p.eliminated]
        if len(alive) <= 1:
            return True
        if not room.deck:
            return True
        return False

    @staticmethod
    def evaluate_round_winner(room: Room) -> list[str]:
        """ラウンド終了時点の勝者プレイヤーID一覧を算出する。"""
        alive = [p for p in room.players if not p.eliminated]
        if not alive:
            return []
        max_val = max(p.hand[0] if p.hand else -1 for p in alive)
        winners = [p for p in alive if (p.hand[0] if p.hand else -1) == max_val]
        return [p.player_id for p in winners]

    @staticmethod
    def award_tokens(room: Room, winner_ids: list[str]) -> dict:
        """勝者とSpyボーナスを反映してトークンを付与し、付与情報を返す。"""
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
        """ゲーム勝利条件を満たしたプレイヤーID一覧を返す。"""
        winners = [p for p in room.players if p.tokens >= room.tokens_to_win]
        return [p.player_id for p in winners]

    @staticmethod
    def advance_turn(room: Room) -> None:
        """次の生存プレイヤーへ手番を進め、保護解除とドロー処理を行う。"""
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
