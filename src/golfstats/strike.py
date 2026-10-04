from __future__ import annotations

import math
import statistics

from .clubs import club_order
from .stats import Session, robust_sd

BEST_PERCENTILE = 0.9


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def history_until(session: Session, history: list[Session]) -> list[Session]:
    earlier = [h for h in history if h.player == session.player and h.start <= session.start and h.id != session.id]
    return [*earlier, session]


def strike_references(session: Session, history: list[Session], ratio: float, min_n: int) -> dict[str, dict]:
    pool: dict[str, list[float]] = {}
    for h in history_until(session, history):
        for s in h.counted:
            if s.get("smash_factor") is not None:
                pool.setdefault(s["club_code"], []).append(s["smash_factor"])
    refs = {}
    for club, xs in pool.items():
        if len(xs) >= min_n:
            best = percentile(xs, BEST_PERCENTILE)
            refs[club] = {"best": best, "threshold": math.ceil(ratio * best * 1000 - 1e-9) / 1000, "n": len(xs)}
    return refs


def strike_class(shot: dict, refs: dict[str, dict]) -> str | None:
    ref = refs.get(shot["club_code"])
    if ref is None or shot.get("smash_factor") is None:
        return None
    return "mishit" if shot["smash_factor"] < ref["threshold"] else "solid"


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def strike_summary(shots: list[dict], ref: dict | None) -> dict | None:
    if ref is None:
        return None
    rated = [s for s in shots if s.get("smash_factor") is not None]
    mishits = [s for s in rated if s["smash_factor"] < ref["threshold"]]
    solid = [s for s in rated if s["smash_factor"] >= ref["threshold"]]
    carry_solid = _median([s["carry"] for s in solid if s.get("carry") is not None])
    carry_mishit = _median([s["carry"] for s in mishits if s.get("carry") is not None])
    return {
        "n": len(rated),
        "unrated": len(shots) - len(rated),
        "mishits": len(mishits),
        "share": len(mishits) / len(rated) if rated else None,
        "threshold": ref["threshold"],
        "best": ref["best"],
        "start_mishit": _median([s["launch_direction"] for s in mishits if s.get("launch_direction") is not None]),
        "start_solid": _median([s["launch_direction"] for s in solid if s.get("launch_direction") is not None]),
        "carry_solid": carry_solid,
        "carry_mishit": carry_mishit,
        "carry_lost": carry_solid - carry_mishit if carry_solid is not None and carry_mishit is not None else None,
        "club_data_mishit": sum(1 for s in mishits if s.get("face_angle") is not None),
        "club_data_solid": sum(1 for s in solid if s.get("face_angle") is not None),
        "solid": len(solid),
    }


def direction_words(deg: float | None) -> str:
    if deg is None:
        return "–"
    if abs(deg) < 0.5:
        return "straight"
    return f"{abs(deg):.0f}° {'right' if deg > 0 else 'left'}"


def bag(session: Session, history: list[Session], refs: dict[str, dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for h in history_until(session, history):
        for s in h.counted:
            if s.get("carry") is not None:
                groups.setdefault(s["club_code"], []).append(s)
    rows = []
    for club in sorted(groups, key=club_order):
        shots = groups[club]
        rated = club in refs
        use = [s for s in shots if strike_class(s, refs) == "solid"] if rated else shots
        carries = [s["carry"] for s in use]
        if not carries:
            continue
        rows.append({"club": club, "n": len(carries), "solid_only": rated, "carry": statistics.median(carries),
                     "sd": robust_sd(carries), "gap": None})
    for longer, shorter in zip(rows, rows[1:]):
        longer["gap"] = longer["carry"] - shorter["carry"]
    return rows
