"""ULID generation — a real, spec-correct ULID, not a look-alike.

A ULID is 128 bits: a 48-bit big-endian millisecond timestamp followed by 80
bits of randomness, rendered as 26 Crockford Base32 characters. The encoding is
lexicographically sortable (ids created later sort after earlier ones) and the
timestamp is recoverable from the first 10 characters.

We implement the spec ourselves rather than take a dependency: no ULID library
clears the project's Golden-rule bar (>5k stars), and the spec is small and
well-defined. We follow the **monotonic** factory: ids minted within the same
millisecond increment the random component so they keep sorting in creation
order. See https://github.com/ulid/spec.

Do not confuse this with the test harness's ``SeededRandom.sortable_id``, which
is deliberately *not* a ULID (no real timestamp, no Crockford alphabet).
"""

from __future__ import annotations

import os
import time
from threading import Lock

# Crockford's Base32: the digits/letters excluding I, L, O, U (to avoid
# ambiguity). Index = 5-bit value.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {ch: i for i, ch in enumerate(_CROCKFORD)}

_TIME_LEN = 10  # chars encoding the 48-bit timestamp
_RAND_LEN = 16  # chars encoding the 80-bit randomness
ULID_LEN = _TIME_LEN + _RAND_LEN  # 26

_TIMESTAMP_MAX = (1 << 48) - 1
_RANDOM_MAX = (1 << 80) - 1

_lock = Lock()
_last_ms = -1
_last_random = 0


def _encode(value: int, length: int) -> str:
    """Render ``value`` as ``length`` Crockford Base32 characters, big-endian."""
    out = bytearray(length)
    for i in range(length - 1, -1, -1):
        out[i] = ord(_CROCKFORD[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a fresh, monotonic 26-character ULID.

    Thread-safe. Within a single millisecond the random component is incremented
    so ids stay strictly increasing; if that 80-bit space overflows, the clock
    component is nudged forward by 1 ms (the spec's overflow rule).
    """
    global _last_ms, _last_random
    with _lock:
        ms = _now_ms() if timestamp_ms is None else timestamp_ms
        if ms == _last_ms:
            _last_random += 1
            if _last_random > _RANDOM_MAX:
                _last_ms += 1
                ms = _last_ms
                _last_random = int.from_bytes(os.urandom(10), "big")
        else:
            if ms < _last_ms:
                # Clock moved backwards; keep monotonicity by staying on _last_ms.
                ms = _last_ms
                _last_random += 1
            else:
                _last_ms = ms
                _last_random = int.from_bytes(os.urandom(10), "big")
        return _encode(ms & _TIMESTAMP_MAX, _TIME_LEN) + _encode(
            _last_random & _RANDOM_MAX, _RAND_LEN
        )


def is_ulid(value: str) -> bool:
    """True if ``value`` is a syntactically valid ULID (length + alphabet)."""
    if len(value) != ULID_LEN:
        return False
    return all(ch in _DECODE for ch in value)


def ulid_timestamp_ms(value: str) -> int:
    """Recover the millisecond timestamp encoded in a ULID's first 10 chars."""
    if not is_ulid(value):
        raise ValueError(f"not a valid ULID: {value!r}")
    ms = 0
    for ch in value[:_TIME_LEN]:
        ms = (ms << 5) | _DECODE[ch]
    return ms
