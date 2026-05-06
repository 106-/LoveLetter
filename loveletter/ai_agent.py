from __future__ import annotations

import json
import logging
import os
import random
import re
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from .game_logic import CARD_NAMES, GUARD, Player, Room

try:
    import litellm as _litellm

    _litellm.suppress_debug_info = True
    from litellm import completion
except Exception:  # pragma: no cover - handled by fallback paths.
    completion = None

load_dotenv()

SUPPORTED_AI_PROVIDERS = ("openai", "anthropic", "gemini", "xai")

KEY_ENV_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY",),
}

MODEL_ENV_BY_PROVIDER: dict[str, str] = {
    "openai": "LITELLM_MODEL_OPENAI",
    "anthropic": "LITELLM_MODEL_ANTHROPIC",
    "gemini": "LITELLM_MODEL_GEMINI",
    "xai": "LITELLM_MODEL_XAI",
}

SYSTEM_PROMPT = """\
You are an AI player in the card game Love Letter.

## Goal
Be the last player standing, or hold the highest card when the deck runs out.
Collect affection tokens to win the game (tokens needed: 2p=6, 3p=5, 4p=4, 5-6p=3).

## Cards (value: name — effect)
0 Spy     — No effect when played. If you are the only Spy played this round and survive, gain +1 token.
1 Guard   — Guess another player's card (not Guard). If correct, they are eliminated.
2 Priest  — Look at another player's hand privately.
3 Baron   — Compare hands with another player; lower hand is eliminated (tie = both safe).
4 Handmaid — You are protected from all card effects until your next turn.
5 Prince  — Choose a player (including yourself) to discard their hand and draw a new card. If the deck is empty, they draw the set-aside card.
6 Chancellor — Draw 2 cards, keep 1, return the rest to the bottom of the deck in any order.
7 King    — Swap hands with another player.
8 Countess — Must be played if you also hold King or Prince in hand.
9 Princess — If you play or discard this card for any reason, you are immediately eliminated.

## Key strategies
- Guard: Target players whose card you can deduce from action logs or discard piles. Never guess Guard (1).
- Baron: Play when your hand value is likely higher than the target's. Avoid if your card is low.
- Prince: Use on a player you suspect holds a high card, or on yourself to escape a bad hand.
- King: Swap when you hold a low card and suspect the target holds a higher one.
- Countess: You MUST play it when holding King or Prince — playing those cards instead is illegal.
- Princess: Never play this card; holding it is fine but playing/discarding it eliminates you.
- Handmaid: Use it when you expect to be targeted, or to stall when no better play exists.
- Spy: Playing it early lets you keep it in discard; survive to end of round for the token bonus.

## Decision tips
- Use `discard_pile` and `recent_action_log` to narrow down what opponents hold.
- A protected (Handmaid) player cannot be targeted — pick another candidate.
- When the deck is nearly empty, preserve high-value cards and eliminate threats quickly.
- If only one legal target exists, you must choose that player.

Return JSON only, no markdown, no explanations.\
"""


def normalize_provider(provider: str | None) -> str | None:
    if not provider:
        return None
    key = provider.strip().lower()
    if key in SUPPORTED_AI_PROVIDERS:
        return key
    return None


def resolve_model(provider: str) -> str | None:
    """LITELLM_MODEL_* が設定されていればその値を、未設定なら None を返す。"""
    return os.getenv(MODEL_ENV_BY_PROVIDER[provider]) or None


def _provider_has_key(provider: str) -> bool:
    return any(os.getenv(env_name) for env_name in KEY_ENV_BY_PROVIDER[provider])


def _provider_is_available(provider: str) -> bool:
    """APIキーとモデル名の両方が設定されている場合のみ True。"""
    return _provider_has_key(provider) and resolve_model(provider) is not None


def _resolve_api_key(provider: str) -> str | None:
    for env_name in KEY_ENV_BY_PROVIDER[provider]:
        val = os.getenv(env_name)
        if val:
            return val
    return None


def _player_view(room: Room, acting: Player) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    for p in room.players:
        players.append(
            {
                "player_id": p.player_id,
                "name": p.name,
                "is_ai": p.is_ai,
                "ai_provider": p.ai_provider,
                "eliminated": p.eliminated,
                "protected": p.protected,
                "tokens": p.tokens,
                "discard_pile": p.discard_pile[:],
                "hand_count": len(p.hand),
                "my_hand": p.hand[:] if p.player_id == acting.player_id else None,
            }
        )
    return players


def _build_base_payload(
    room: Room, acting: Player, recent_logs: list[str]
) -> dict[str, Any]:
    return {
        "my_player_id": acting.player_id,
        "my_name": acting.name,
        "my_hand": acting.hand[:],
        "current_player_id": room.players[room.current_player_idx].player_id
        if room.players
        else None,
        "deck_count": len(room.deck),
        "players": _player_view(room, acting),
        "recent_action_log": recent_logs[-12:],
    }


def _extract_text_content(raw_message: Any) -> str:
    if isinstance(raw_message, str):
        return raw_message
    if isinstance(raw_message, list):
        parts: list[str] = []
        for block in raw_message:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _parse_json_text(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _query_model(
    *,
    provider: str,
    model: str | None,
    payload: dict[str, Any],
    schema_hint: dict[str, Any],
) -> dict[str, Any] | None:
    if completion is None:
        return None
    if not model:
        return None
    if not _provider_has_key(provider):
        return None

    api_key = _resolve_api_key(provider)
    task = payload.get("task", "unknown")
    logger.info("AI query: model=%s task=%s", model, task)

    user_payload = {"request": payload, "expected_json_schema_hint": schema_hint}
    try:
        response = completion(
            model=model,
            api_key=api_key,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            temperature=0.2,
            max_tokens=220,
        )
    except Exception:
        logger.exception("AI query failed: model=%s task=%s", model, task)
        return None

    content = None
    try:
        content = response.choices[0].message.content
    except Exception:
        try:
            content = response["choices"][0]["message"]["content"]
        except Exception:
            content = None

    raw_text = _extract_text_content(content)
    logger.info("AI response: model=%s task=%s raw=%s", model, task, raw_text)
    result = _parse_json_text(raw_text)
    if result is None:
        logger.warning("AI response parse failed: model=%s task=%s", model, task)
    return result


def choose_card_index(
    room: Room, acting: Player, legal_indices: list[int], recent_logs: list[str]
) -> tuple[int, bool]:
    provider = normalize_provider(acting.ai_provider)
    if not provider or not _provider_is_available(provider):
        return random.choice(legal_indices), False
    model = resolve_model(provider)

    payload = {
        **_build_base_payload(room, acting, recent_logs),
        "task": "choose_card_to_play",
        "legal_card_indices": legal_indices,
        "hand_options": [
            {"index": i, "card": c, "card_name": CARD_NAMES.get(c, str(c))}
            for i, c in enumerate(acting.hand)
        ],
    }
    result = _query_model(
        provider=provider,
        model=model,
        payload=payload,
        schema_hint={"card_index": "int"},
    )
    if result and isinstance(result.get("card_index"), int):
        card_index = int(result["card_index"])
        if card_index in legal_indices:
            return card_index, False
    return random.choice(legal_indices), True


def choose_target_id(
    room: Room,
    acting: Player,
    card_played: int,
    candidate_target_ids: list[str],
    recent_logs: list[str],
) -> tuple[str, bool]:
    provider = normalize_provider(acting.ai_provider)
    if not provider or not _provider_is_available(provider):
        return random.choice(candidate_target_ids), False
    model = resolve_model(provider)

    payload = {
        **_build_base_payload(room, acting, recent_logs),
        "task": "choose_target_player",
        "card_played": card_played,
        "card_played_name": CARD_NAMES.get(card_played, str(card_played)),
        "candidate_target_ids": candidate_target_ids,
    }
    result = _query_model(
        provider=provider,
        model=model,
        payload=payload,
        schema_hint={"target_id": "string"},
    )
    target_id = result.get("target_id") if result else None
    if isinstance(target_id, str) and target_id in candidate_target_ids:
        return target_id, False
    return random.choice(candidate_target_ids), True


def choose_guard_guess(
    room: Room, acting: Player, recent_logs: list[str]
) -> tuple[int, bool]:
    legal_guesses = [v for v in range(10) if v != GUARD]
    provider = normalize_provider(acting.ai_provider)
    if not provider or not _provider_is_available(provider):
        return random.choice(legal_guesses), False
    model = resolve_model(provider)

    payload = {
        **_build_base_payload(room, acting, recent_logs),
        "task": "choose_guard_guess",
        "legal_guesses": legal_guesses,
        "legal_guess_names": {v: CARD_NAMES[v] for v in legal_guesses},
    }
    result = _query_model(
        provider=provider,
        model=model,
        payload=payload,
        schema_hint={"guessed_value": "int"},
    )
    guessed = result.get("guessed_value") if result else None
    if isinstance(guessed, int) and guessed in legal_guesses:
        return guessed, False
    return random.choice(legal_guesses), True


def choose_chancellor_return(
    room: Room,
    acting: Player,
    chancellor_hand: list[int],
    recent_logs: list[str],
) -> tuple[int, list[int], bool]:
    provider = normalize_provider(acting.ai_provider)
    if not provider or not _provider_is_available(provider):
        kept_index = random.randrange(len(chancellor_hand))
        remaining = [i for i in range(len(chancellor_hand)) if i != kept_index]
        random.shuffle(remaining)
        return kept_index, remaining, False
    model = resolve_model(provider)

    payload = {
        **_build_base_payload(room, acting, recent_logs),
        "task": "resolve_chancellor",
        "chancellor_hand": chancellor_hand,
        "chancellor_options": [
            {"index": i, "card": c, "card_name": CARD_NAMES.get(c, str(c))}
            for i, c in enumerate(chancellor_hand)
        ],
    }
    result = _query_model(
        provider=provider,
        model=model,
        payload=payload,
        schema_hint={"kept_index": "int", "bottom_order_indices": "int[]"},
    )

    if result and isinstance(result.get("kept_index"), int):
        kept_index = int(result["kept_index"])
        if 0 <= kept_index < len(chancellor_hand):
            expected = [i for i in range(len(chancellor_hand)) if i != kept_index]
            order = result.get("bottom_order_indices")
            if isinstance(order, list) and all(isinstance(v, int) for v in order):
                if sorted(order) == sorted(expected):
                    return kept_index, [int(v) for v in order], False

            randomized = expected[:]
            random.shuffle(randomized)
            return kept_index, randomized, False

    kept_index = random.randrange(len(chancellor_hand))
    remaining = [i for i in range(len(chancellor_hand)) if i != kept_index]
    random.shuffle(remaining)
    return kept_index, remaining, True
