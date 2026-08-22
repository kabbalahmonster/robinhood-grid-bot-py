"""Small, deterministic chaos-magick sigils for dashboard display.

An intention is selected once per process incarnation, reduced using the
common consonant/deduplication method, then committed to a SHA-256 seed.  The
dashboard receives no plain-language intention; it only receives the reduced
key and enough entropy to render a reproducible glyph.
"""

import hashlib
import json
from pathlib import Path
import secrets


SIGIL_METHOD = "spare-wheel-v1"
_VOWELS = frozenset("AEIOU")

_INTENTIONS_FILE = Path(__file__).with_name("sigil_intentions.json")
_FALLBACK_INTENTION = "PROSPERITY FLOWS THROUGH MY WORK WITH EASE AND INTEGRITY"


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


def load_intentions(path: Path = _INTENTIONS_FILE) -> tuple[str, ...]:
    """Load and validate the curated, versioned intention grimoire."""
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)
    entries = document.get("intentions") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(entries, list) or len(entries) != 23:
        raise ValueError("sigil intention grimoire must contain exactly 23 version-1 entries")
    ids = set()
    intentions = []
    for entry in entries:
        item_id = entry.get("id") if isinstance(entry, dict) else None
        text = entry.get("text") if isinstance(entry, dict) else None
        if not isinstance(item_id, str) or not item_id or item_id in ids:
            raise ValueError("sigil intention IDs must be non-empty and unique")
        if not isinstance(text, str) or not text or not text.isascii() or text != text.upper():
            raise ValueError("sigil intentions must be non-empty uppercase ASCII strings")
        ids.add(item_id)
        intentions.append(text)
    return tuple(intentions)


def create_sigil(bot_id: str, nonce: bytes | None = None) -> dict:
    """Create the public sigil descriptor for one bot process incarnation."""
    nonce = nonce if nonce is not None else secrets.token_bytes(16)
    try:
        intentions = load_intentions()
    except (OSError, ValueError, json.JSONDecodeError):
        # Dashboard ornamentation must never prevent the trading bot starting.
        intentions = (_FALLBACK_INTENTION,)
    chooser = hashlib.sha256(b"sigil-grimoire-v1\0" + nonce).digest()
    intention = intentions[int.from_bytes(chooser[:8], "big") % len(intentions)]
    reduced = reduce_intention(intention)
    seed = hashlib.sha256(
        SIGIL_METHOD.encode("ascii") + b"\0" + bot_id.encode("utf-8") + b"\0" +
        intention.encode("ascii") + b"\0" + nonce
    ).hexdigest()
    return {"version": 1, "method": SIGIL_METHOD, "key": reduced, "seed": seed}
