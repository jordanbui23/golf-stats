from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .clubs import club_order

TABLE_METRICS = (
    "carry", "carry_side", "total", "club_speed", "ball_speed", "smash_factor", "smash_index",
    "attack_angle", "club_path", "face_angle", "face_to_path", "dynamic_loft", "spin_loft",
    "launch_angle", "launch_direction", "spin_rate", "spin_axis", "max_height", "land_angle",
    "low_point", "impact_offset", "impact_height", "swing_direction", "swing_plane", "dynamic_lie",
    "last_data_length",
)

START_TOLERANCE_DEG = 2.0
STRAIGHT_AXIS_DEG = 2.0
HARD_CURVE_AXIS_DEG = 10.0


@dataclass
class Session:
    id: str
    player: str
    start: datetime
    end: datetime
    shots: list[dict] = field(default_factory=list)

    @property
    def counted(self) -> list[dict]:
        return [s for s in self.shots if s["use_in_stat"]]

    @property
    def label(self) -> str:
        return f"{self.start:%a %b %-d %Y, %-I:%M %p}"


def values(shots: list[dict], key: str) -> list[float]:
    return [s[key] for s in shots if s.get(key) is not None]


def robust_sd(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    med = statistics.median(xs)
    return 1.4826 * statistics.median(abs(x - med) for x in xs)


def summarize(xs: list[float]) -> dict | None:
    if not xs:
        return None
    return {"n": len(xs), "median": statistics.median(xs), "mean": statistics.fmean(xs), "sd": robust_sd(xs)}


def share(shots: list[dict], pred) -> float | None:
    usable = [s for s in shots if pred(s) is not None]
    return sum(1 for s in usable if pred(s)) / len(usable) if usable else None


def split_sessions(shots: list[dict], gap_minutes: float) -> list[Session]:
    gap = timedelta(minutes=gap_minutes)
    sessions: list[Session] = []
    for s in sorted(shots, key=lambda x: (x.get("player") or "", x["ts"])):
        last = sessions[-1] if sessions else None
        if last and last.player == (s.get("player") or "") and s["ts"] - last.end <= gap:
            last.shots.append(s)
            last.end = s["ts"]
        else:
            sessions.append(Session("", s.get("player") or "", s["ts"], s["ts"], [s]))
    sessions.sort(key=lambda x: x.start)
    used: set[str] = set()
    for sess in sessions:
        sid = f"{sess.start:%Y-%m-%d-%H%M}"
        if sid in used:
            sid += "-" + (re.sub(r"[^a-z0-9]+", "-", sess.player.lower()).strip("-") or "x")
        used.add(sid)
        sess.id = sid
    return sessions


def start_label(launch_direction: float | None) -> str | None:
    if launch_direction is None:
        return None
    if launch_direction < -START_TOLERANCE_DEG:
        return "pull"
    if launch_direction > START_TOLERANCE_DEG:
        return "push"
    return "straight"


def curve_label(spin_axis: float | None) -> str | None:
    if spin_axis is None:
        return None
    if abs(spin_axis) <= STRAIGHT_AXIS_DEG:
        return "straight"
    if spin_axis < 0:
        return "hook" if spin_axis < -HARD_CURVE_AXIS_DEG else "draw"
    return "slice" if spin_axis > HARD_CURVE_AXIS_DEG else "fade"


def shape_label(shot: dict) -> str | None:
    start, curve = start_label(shot.get("launch_direction")), curve_label(shot.get("spin_axis"))
    if start is None or curve is None:
        return None
    if start == "straight":
        return "straight" if curve == "straight" else curve
    return start if curve == "straight" else f"{start}-{curve}"


def side_decomposition(shots: list[dict]) -> dict | None:
    pairs = [(s["carry_side"], s["curve"]) for s in shots if s.get("carry_side") is not None and s.get("curve") is not None]
    if len(pairs) < 3:
        return None
    side = [p[0] for p in pairs]
    curve = [p[1] for p in pairs]
    start = [p[0] - p[1] for p in pairs]
    start_sd, curve_sd = robust_sd(start) or 0.0, robust_sd(curve) or 0.0
    return {
        "n": len(pairs),
        "side_median": statistics.median(side),
        "side_sd": robust_sd(side),
        "start_median": statistics.median(start),
        "start_sd": start_sd,
        "curve_median": statistics.median(curve),
        "curve_sd": curve_sd,
        "dominant": "curve" if curve_sd >= start_sd else "start",
    }


def club_summary(shots: list[dict]) -> dict:
    counted = [s for s in shots if s["use_in_stat"]]
    shapes: dict[str, int] = {}
    for s in counted:
        label = shape_label(s)
        if label:
            shapes[label] = shapes.get(label, 0) + 1
    return {
        "n": len(counted),
        "n_total": len(shots),
        "metrics": {k: summarize(values(counted, k)) for k in TABLE_METRICS},
        "shapes": dict(sorted(shapes.items(), key=lambda kv: -kv[1])),
        "sides": side_decomposition(counted),
        "estimated_spin_share": share(counted, lambda s: None if not s.get("spin_rate_type")
                                      else s["spin_rate_type"].lower() == "estimated"),
        "club_data_missing_share": share(counted, lambda s: s.get("face_angle") is None),
        "impact_missing_share": share(counted, lambda s: s.get("impact_offset") is None),
        "low_point_behind_share": share(counted, lambda s: None if s.get("low_point") is None else s["low_point"] <= 0),
    }


def by_club(shots: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for s in shots:
        groups.setdefault(s["club_code"], []).append(s)
    return dict(sorted(groups.items(), key=lambda kv: club_order(kv[0])))


def session_summary(session: Session) -> dict:
    return {code: club_summary(group) for code, group in by_club(session.shots).items()}


def is_normalized(session: Session) -> bool:
    return any("normaliz" in (s.get("condition") or "").lower() for s in session.shots)


def fmt(value: float | None, digits: int = 1, signed: bool = False) -> str:
    if value is None or math.isnan(value):
        return "–"
    value = round(value, digits) or 0.0
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
