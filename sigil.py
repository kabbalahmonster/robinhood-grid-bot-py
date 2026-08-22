"""Small, deterministic chaos-magick sigils for dashboard display.

An intention is composed once per process incarnation, reduced using the
common consonant/deduplication method, then committed to a SHA-256 seed.  The
dashboard receives no plain-language intention; it only receives the reduced
key and enough entropy to render a reproducible glyph.
"""

import hashlib
import secrets


SIGIL_METHOD = "spare-wheel-v1"
_VOWELS = frozenset("AEIOU")

_OPENINGS = (
    "MY WORK CULTIVATES",
    "THIS ENGINE WELCOMES",
    "MY CRAFT ATTRACTS",
    "THIS SYSTEM CREATES",
)
_QUALITIES = (
    "STEADY PROSPERITY",
    "FORTUNATE TIMING",
    "ABUNDANT OPPORTUNITY",
    "WISE AND LASTING GAINS",
)
_GUARDS = (
    "WITH CLEAR JUDGMENT",
    "THROUGH SKILLFUL ACTION",
    "IN HARMONY WITH GOOD FORTUNE",
    "WHILE PRESERVING WHAT GROWS",
)


def reduce_intention(intention: str) -> str:
    """Keep the first occurrence of each consonant, in reading order."""
    seen = set()
    reduced = []
    for char in intention.upper():
        if not char.isascii() or not char.isalpha() or char in _VOWELS or char in seen:
            continue
        seen.add(char)
        reduced.append(char)
    return "".join(reduced)


def _pick(values, digest: bytes, offset: int) -> str:
    return values[digest[offset] % len(values)]


def create_sigil(bot_id: str, nonce: bytes | None = None) -> dict:
    """Create the public sigil descriptor for one bot process incarnation."""
    nonce = nonce if nonce is not None else secrets.token_bytes(16)
    chooser = hashlib.sha256(b"sigil-grammar-v1\0" + nonce).digest()
    intention = " ".join((
        _pick(_OPENINGS, chooser, 0),
        _pick(_QUALITIES, chooser, 1),
        _pick(_GUARDS, chooser, 2),
    ))
    reduced = reduce_intention(intention)
    seed = hashlib.sha256(
        SIGIL_METHOD.encode("ascii") + b"\0" + bot_id.encode("utf-8") + b"\0" +
        intention.encode("ascii") + b"\0" + nonce
    ).hexdigest()
    return {"version": 1, "method": SIGIL_METHOD, "key": reduced, "seed": seed}
