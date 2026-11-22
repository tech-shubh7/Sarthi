"""Pairing the bot with its owner: a one-time code shown on the computer, sent once from the user's chat.

Only the code's hash is stored, it expires after a few minutes, works once, and is cancelled after a few
wrong guesses (a stranger who finds the bot can neither guess it nor block pairing with one bad guess).
After pairing the bot answers that one chat only and ignores everyone else.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sarthi.clock import from_iso, to_iso
from sarthi.db.chat import ChatStore

OWNER_KEY = "telegram.owner_chat_id"
_CODE_HASH_KEY = "telegram.pair_hash"
_CODE_EXPIRES_KEY = "telegram.pair_expires"
_FAILURES_KEY = "telegram.pair_failures"
MAX_WRONG_GUESSES = 5
CODE_LIFETIME = timedelta(minutes=15)
_CODE_BYTES = 6


def code_hash(code: str) -> str:
    """How a code is stored (and, with the shared bot, sent to the relay)."""
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def new_code(store: ChatStore, now: datetime) -> str:
    """Create a fresh one-time pairing code (replacing any earlier one) and return it."""
    code = secrets.token_hex(_CODE_BYTES).upper()
    store.set_state(_CODE_HASH_KEY, code_hash(code))
    store.set_state(_CODE_EXPIRES_KEY, to_iso(now + CODE_LIFETIME))
    store.set_state(_FAILURES_KEY, None)
    return code


def _forget_code(store: ChatStore) -> None:
    for key in (_CODE_HASH_KEY, _CODE_EXPIRES_KEY, _FAILURES_KEY):
        store.set_state(key, None)


def redeem(store: ChatStore, code: str, chat_id: int, now: datetime) -> bool:
    """Pair ``chat_id`` if ``code`` is the current, unexpired code (which is then used up)."""
    expected, expires = store.get_state(_CODE_HASH_KEY), store.get_state(_CODE_EXPIRES_KEY)
    if not expected or not expires:
        return False
    if now > from_iso(expires):
        _forget_code(store)
        return False
    if not code or not hmac.compare_digest(expected, code_hash(code)):
        failures = int(store.get_state(_FAILURES_KEY) or 0) + 1
        if failures >= MAX_WRONG_GUESSES:
            _forget_code(store)
        else:
            store.set_state(_FAILURES_KEY, str(failures))
        return False
    _forget_code(store)
    store.set_state(OWNER_KEY, str(chat_id))
    return True


def owner_chat(store: ChatStore) -> int | None:
    """The paired chat, or None before pairing."""
    value = store.get_state(OWNER_KEY)
    return int(value) if value else None
