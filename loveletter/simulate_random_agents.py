from __future__ import annotations

import argparse
import json
import random
import traceback
from collections import Counter, deque
from dataclasses import dataclass
from typing import Iterable

from .game_logic import (
    BARON,
    CHANCELLOR,
    COUNTESS,
    GUARD,
    HANDMAID,
    KING,
    PRIEST,
    PRINCE,
    PRINCESS,
    SPY,
    TOKENS_TO_WIN,
    GameEngine,
    Player,
    Room,
)

TOTAL_CARDS = len(GameEngine.build_deck())
GUARD_GUESS_CHOICES = [v for v in range(10) if v != GUARD]


class SimulationFailure(RuntimeError):
    def __init__(self, message: str, history: list[str]):
        super().__init__(message)
        self.history = history


@dataclass
class GameResult:
    winner_ids: list[str]
    rounds: int
    turns: int


def _fail(message: str, history: deque[str]) -> None:
    raise SimulationFailure(message, list(history))


def _assert_invariants(
    room: Room,
    history: deque[str],
    stage: str,
    *,
    allow_eliminated_current: bool = False,
) -> None:
    total = (
        len(room.deck) + len(room.face_up_cards) + (1 if room.aside_card != -1 else 0)
    )
    alive = 0
    for player in room.players:
        total += len(player.hand) + len(player.discard_pile)
        if len(player.hand) > 2:
            _fail(
                f"[{stage}] {player.player_id} has too many cards in hand: {len(player.hand)}",
                history,
            )
        if player.eliminated and player.hand:
            _fail(
                f"[{stage}] eliminated player {player.player_id} still has a hand",
                history,
            )
        if not player.eliminated:
            alive += 1

    if total != TOTAL_CARDS:
        _fail(
            f"[{stage}] card conservation broken: expected {TOTAL_CARDS}, got {total}",
            history,
        )

    if room.phase == "playing":
        if not (0 <= room.current_player_idx < len(room.players)):
            _fail(
                f"[{stage}] invalid current_player_idx: {room.current_player_idx}",
                history,
            )
        current = room.players[room.current_player_idx]
        if current.eliminated and not allow_eliminated_current:
            _fail(
                f"[{stage}] current player {current.player_id} is eliminated", history
            )
        if alive <= 0:
            _fail(f"[{stage}] no alive players while phase=playing", history)


def _create_room(num_players: int, first_player_idx: int) -> Room:
    players = [Player(player_id=f"P{i}", name=f"Player{i}") for i in range(num_players)]
    room = Room(
        room_id="SIM",
        host_id=players[0].player_id,
        players=players,
        current_player_idx=first_player_idx,
    )
    room.tokens_to_win = TOKENS_TO_WIN.get(num_players, 3)
    GameEngine.setup_round(room)
    return room


def _choose_playable_card_index(
    room: Room, current: Player, history: deque[str]
) -> int:
    candidates = []
    for idx, card in enumerate(current.hand):
        if GameEngine.validate_play(room, current, card) is None:
            candidates.append(idx)
    if not candidates:
        _fail(
            f"{current.player_id} has no playable cards: hand={current.hand}", history
        )
    return random.choice(candidates)


def _resolve_chancellor(room: Room, current: Player, history: deque[str]) -> None:
    chancellor_hand = list(current.hand)
    for _ in range(2):
        if room.deck:
            chancellor_hand.append(room.deck.pop())

    if len(chancellor_hand) <= 1:
        current.hand = chancellor_hand[:1]
        for card in chancellor_hand[1:]:
            room.deck.insert(0, card)
        history.append(f"{current.player_id} chancellor resolved with <=1 option")
        return

    keep_index = random.randrange(len(chancellor_hand))
    kept_card = chancellor_hand.pop(keep_index)
    random.shuffle(chancellor_hand)
    for card in reversed(chancellor_hand):
        room.deck.insert(0, card)

    current.hand = [kept_card]
    history.append(
        f"{current.player_id} chancellor kept={kept_card} returned={len(chancellor_hand)} cards"
    )


def _resolve_turn(room: Room, history: deque[str]) -> None:
    current = room.players[room.current_player_idx]
    if not current.hand:
        _fail(f"{current.player_id} has no cards at turn start", history)

    card_index = _choose_playable_card_index(room, current, history)
    card_value = current.hand[card_index]
    history.append(
        f"{current.player_id} hand={current.hand} plays card={card_value} idx={card_index}"
    )

    current.hand.pop(card_index)
    current.discard_pile.append(card_value)

    if card_value in (SPY, HANDMAID, COUNTESS, PRINCESS):
        GameEngine.resolve_card(room, current, card_value)
        return

    if card_value == CHANCELLOR:
        _resolve_chancellor(room, current, history)
        return

    target_ids = GameEngine.compute_valid_targets(room, current, card_value)
    if not target_ids:
        history.append(f"{current.player_id} {card_value} fizzled (no valid target)")
        return

    target_id = random.choice(target_ids)
    target = GameEngine._get_player(room, target_id)
    if target is None:
        _fail(f"target not found: {target_id}", history)

    if card_value == GUARD:
        guessed = random.choice(GUARD_GUESS_CHOICES)
        history.append(
            f"{current.player_id} GUARD target={target.player_id} guess={guessed}"
        )
        GameEngine.resolve_guard(room, current, target, guessed)
        return

    if card_value in (PRIEST, BARON, PRINCE, KING):
        history.append(
            f"{current.player_id} card={card_value} target={target.player_id}"
        )
        GameEngine.resolve_card(room, current, card_value, target)
        return

    _fail(f"unsupported playable card encountered: {card_value}", history)


def _finish_round(room: Room, history: deque[str]) -> None:
    room.phase = "round_end"
    winner_ids = GameEngine.evaluate_round_winner(room)
    room.round_winner_ids = winner_ids
    award = GameEngine.award_tokens(room, winner_ids)
    history.append(
        f"round_end winners={winner_ids} spy_bonus={award.get('spy_bonus_id')}"
    )

    game_winners = GameEngine.check_game_winner(room)
    if game_winners:
        room.winner_ids = game_winners
        room.phase = "game_over"
        history.append(f"game_over winners={game_winners}")
        return

    if len(winner_ids) == 1:
        next_id = winner_ids[0]
    elif winner_ids:
        next_id = random.choice(winner_ids)
    else:
        next_id = room.players[room.current_player_idx].player_id
    next_player = GameEngine._get_player(room, next_id)
    if next_player is None:
        _fail(f"next round starter not found: {next_id}", history)
    room.current_player_idx = room.players.index(next_player)
    GameEngine.setup_round(room)


def run_single_game(
    *,
    num_players: int,
    seed: int,
    max_rounds: int,
    max_turns_per_round: int,
    randomize_first_player: bool,
) -> GameResult:
    random.seed(seed)
    first_player_idx = random.randrange(num_players) if randomize_first_player else 0
    room = _create_room(num_players, first_player_idx)
    history: deque[str] = deque(maxlen=120)
    history.append(f"start seed={seed} players={num_players} first={first_player_idx}")
    rounds = 0
    turns = 0

    _assert_invariants(room, history, "setup_round")

    try:
        while room.phase != "game_over":
            turns_in_round = 0
            while room.phase == "playing":
                turns += 1
                turns_in_round += 1
                if turns_in_round > max_turns_per_round:
                    _fail(
                        f"turn limit exceeded: {max_turns_per_round} turns in one round",
                        history,
                    )

                _assert_invariants(room, history, f"before_turn_{turns}")
                _resolve_turn(room, history)
                _assert_invariants(
                    room,
                    history,
                    f"after_turn_{turns}",
                    allow_eliminated_current=True,
                )

                if GameEngine.check_round_end(room):
                    rounds += 1
                    if rounds > max_rounds:
                        _fail(
                            f"round limit exceeded: {max_rounds} (possible infinite game)",
                            history,
                        )
                    _finish_round(room, history)
                    _assert_invariants(room, history, f"after_round_{rounds}")
                    break

                GameEngine.advance_turn(room)
                _assert_invariants(room, history, f"after_advance_{turns}")

        if not room.winner_ids:
            _fail("game finished without winners", history)

        return GameResult(winner_ids=room.winner_ids[:], rounds=rounds, turns=turns)
    except SimulationFailure:
        raise
    except Exception as exc:
        raise SimulationFailure(
            f"unexpected exception: {exc.__class__.__name__}: {exc}",
            list(history),
        ) from exc


def _parse_player_counts(raw: Iterable[int]) -> list[int]:
    values = sorted(set(raw))
    for n in values:
        if n < 2 or n > 6:
            raise ValueError(f"player count must be in [2, 6], got {n}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stress-test Love Letter logic by simulating many random-agent games."
    )
    parser.add_argument(
        "--games-per-config",
        type=int,
        default=2000,
        help="Number of simulated games per player-count configuration.",
    )
    parser.add_argument(
        "--player-counts",
        type=int,
        nargs="+",
        default=[2, 3, 4, 5, 6],
        help="Player counts to test (2..6).",
    )
    parser.add_argument(
        "--base-seed",
        type=int,
        default=20260308,
        help="Base seed. Each trial derives its own seed from this.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=300,
        help="Safety cap for rounds in a single game.",
    )
    parser.add_argument(
        "--max-turns-per-round",
        type=int,
        default=200,
        help="Safety cap for turns in a single round.",
    )
    parser.add_argument(
        "--randomize-first-player",
        action="store_true",
        help="Start each game with a random first player instead of player 0.",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop immediately when the first failure is found.",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default="",
        help="Optional path to write a JSON summary report.",
    )
    args = parser.parse_args()

    try:
        player_counts = _parse_player_counts(args.player_counts)
    except ValueError as exc:
        print(f"[invalid-args] {exc}")
        return 2

    if args.games_per_config <= 0:
        print("[invalid-args] --games-per-config must be >= 1")
        return 2

    summary: dict[str, object] = {
        "base_seed": args.base_seed,
        "games_per_config": args.games_per_config,
        "player_counts": player_counts,
        "max_rounds": args.max_rounds,
        "max_turns_per_round": args.max_turns_per_round,
        "randomize_first_player": args.randomize_first_player,
        "configs": [],
        "failures": [],
    }

    total_games = 0
    total_rounds = 0
    total_turns = 0
    total_failures = 0

    for players in player_counts:
        wins = Counter()
        config_games = 0
        config_rounds = 0
        config_turns = 0
        config_failures = 0

        for trial in range(args.games_per_config):
            seed = args.base_seed + players * 1_000_000 + trial
            try:
                result = run_single_game(
                    num_players=players,
                    seed=seed,
                    max_rounds=args.max_rounds,
                    max_turns_per_round=args.max_turns_per_round,
                    randomize_first_player=args.randomize_first_player,
                )
                config_games += 1
                config_rounds += result.rounds
                config_turns += result.turns
                for winner_id in result.winner_ids:
                    wins[winner_id] += 1
            except SimulationFailure as exc:
                config_failures += 1
                failure_info = {
                    "players": players,
                    "trial": trial,
                    "seed": seed,
                    "error": str(exc),
                    "history": exc.history,
                    "traceback": traceback.format_exc(),
                }
                cast_failures = summary["failures"]
                if isinstance(cast_failures, list):
                    cast_failures.append(failure_info)
                print(
                    f"[FAIL] players={players} trial={trial} seed={seed} "
                    f"error={str(exc).splitlines()[0]}"
                )
                if args.stop_on_failure:
                    break

        total_games += config_games
        total_rounds += config_rounds
        total_turns += config_turns
        total_failures += config_failures

        avg_rounds = (config_rounds / config_games) if config_games else 0.0
        avg_turns = (config_turns / config_games) if config_games else 0.0
        print(
            f"[CONFIG] players={players} games={config_games} "
            f"failures={config_failures} avg_rounds={avg_rounds:.2f} avg_turns={avg_turns:.2f}"
        )

        config_summary = {
            "players": players,
            "games_completed": config_games,
            "games_failed": config_failures,
            "avg_rounds": avg_rounds,
            "avg_turns": avg_turns,
            "wins": dict(wins),
        }
        cast_configs = summary["configs"]
        if isinstance(cast_configs, list):
            cast_configs.append(config_summary)

        if args.stop_on_failure and config_failures > 0:
            break

    overall_avg_rounds = (total_rounds / total_games) if total_games else 0.0
    overall_avg_turns = (total_turns / total_games) if total_games else 0.0
    print(
        f"[TOTAL] games={total_games} failures={total_failures} "
        f"avg_rounds={overall_avg_rounds:.2f} avg_turns={overall_avg_turns:.2f}"
    )

    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[REPORT] wrote {args.report_json}")

    return 1 if total_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
