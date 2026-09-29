from __future__ import annotations

import re

_WORDS = {"pitching": "PW", "gap": "GW", "approach": "AW", "sand": "SW", "lob": "LW"}


def club_code(name: str) -> str:
    s = name.strip().lower()
    if not s:
        return "?"
    if "driver" in s:
        return "DR"
    if "putter" in s:
        return "PT"
    for word, code in _WORDS.items():
        if word in s and "wedge" in s:
            return code
    m = re.match(r"^(\d+)\s*(wood|hybrid|iron|w|h|i)\b", s)
    if m:
        return m.group(1) + {"wood": "W", "w": "W", "hybrid": "H", "h": "H", "iron": "i", "i": "i"}[m.group(2)]
    m = re.match(r"^(\d{2})\s*(deg|°)?\s*wedge", s) or re.match(r"^wedge\s*(\d{2})", s)
    if m:
        return m.group(1) + "°"
    return re.sub(r"[^\w °.+-]", "", name.strip())[:24] or "?"


def club_order(code: str) -> tuple[int, int, str]:
    if code == "DR":
        return (0, 0, code)
    m = re.match(r"^(\d+)(W|H|i)$", code)
    if m:
        return ({"W": 1, "H": 2, "i": 3}[m.group(2)], int(m.group(1)), code)
    wedges = {"PW": 0, "GW": 1, "AW": 1, "SW": 2, "LW": 3}
    if code in wedges:
        return (4, wedges[code], code)
    m = re.match(r"^(\d{2})°$", code)
    if m:
        return (4, int(m.group(1)), code)
    if code == "PT":
        return (6, 0, code)
    return (5, 0, code)


def is_iron_or_wedge(code: str) -> bool:
    return club_order(code)[0] in (3, 4)
