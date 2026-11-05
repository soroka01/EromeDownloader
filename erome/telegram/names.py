"""Account and group names for the console: fancy letters become plain ones, what the console cannot draw is dropped."""
from __future__ import annotations

import unicodedata
from typing import Any


INVISIBLE_CHARS = dict.fromkeys(
    [0x115F, 0x1160, 0x3164, 0xFFA0, 0x2060, 0xFEFF, *range(0x200B, 0x2010)], None
)
DISPLAY_NAME_LIMIT = 40
# What the legacy Windows console can draw with a predictable cell width: Latin, Greek, Cyrillic, common punctuation.
RENDERABLE_RANGES = ((0x20, 0x24F), (0x370, 0x52F), (0x1E00, 0x1EFF), (0x2010, 0x2027), (0x2030, 0x205E), (0x20A0, 0x20BF))


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(INVISIBLE_CHARS)  # fancy letters become plain ones
    text = " ".join(text.split())
    text = "".join(ch for ch in text if any(low <= ord(ch) <= high for low, high in RENDERABLE_RANGES))
    text = " ".join(text.split())
    return text if len(text) <= DISPLAY_NAME_LIMIT else text[: DISPLAY_NAME_LIMIT - 1] + "…"


def display_name(entity: Any) -> str:
    name = clean_text(
        " ".join(
            str(part)
            for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
            if part
        )
    )
    username = getattr(entity, "username", None)
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else str(getattr(entity, "id", "unknown")))
