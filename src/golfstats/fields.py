from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    headers: tuple[str, ...]
    kind: str
    unit: str
    label: str


SPEED = "speed"
LENGTH = "length"
ANGLE = "angle"
SPIN = "spin"
TIME = "time"
RATIO = "ratio"
PERCENT = "percent"

NUMERIC_FIELDS: tuple[Field, ...] = (
    Field("club_speed", ("Club Speed",), SPEED, "mph", "Club speed"),
    Field("attack_angle", ("Attack Angle",), ANGLE, "deg", "Attack angle"),
    Field("club_path", ("Club Path",), ANGLE, "deg", "Club path"),
    Field("low_point", ("Low Point", "Low Point Distance"), LENGTH, "in", "Low point"),
    Field("swing_plane", ("Swing Plane",), ANGLE, "deg", "Swing plane"),
    Field("swing_direction", ("Swing Direction",), ANGLE, "deg", "Swing direction"),
    Field("dynamic_loft", ("Dyn. Loft", "Dynamic Loft"), ANGLE, "deg", "Dynamic loft"),
    Field("face_angle", ("Face Angle",), ANGLE, "deg", "Face angle"),
    Field("face_to_path", ("Face To Path",), ANGLE, "deg", "Face to path"),
    Field("ball_speed", ("Ball Speed",), SPEED, "mph", "Ball speed"),
    Field("smash_factor", ("Smash Factor",), RATIO, "", "Smash factor"),
    Field("launch_angle", ("Launch Angle",), ANGLE, "deg", "Launch angle"),
    Field("launch_direction", ("Launch Direction",), ANGLE, "deg", "Launch direction"),
    Field("spin_rate", ("Spin Rate",), SPIN, "rpm", "Spin rate"),
    Field("spin_axis", ("Spin Axis",), ANGLE, "deg", "Spin axis"),
    Field("max_height_dist", ("Max Height - Dist.",), LENGTH, "yds", "Apex distance"),
    Field("max_height", ("Max Height - Height", "Max Height", "Height"), LENGTH, "ft", "Max height"),
    Field("max_height_side", ("Max Height - Side",), LENGTH, "yds", "Apex side"),
    Field("last_data_length", ("Last data Point - Length",), LENGTH, "yds", "Tracked length"),
    Field("last_data_side", ("Last data Point - Side",), LENGTH, "yds", "Tracked side"),
    Field("last_data_height", ("Last data Point - Height",), LENGTH, "yds", "Tracked height"),
    Field("last_data_time", ("Last data Point - Time",), TIME, "s", "Tracked time"),
    Field("carry", ("Carry Flat - Length", "Carry", "Carry Distance"), LENGTH, "yds", "Carry"),
    Field("carry_side", ("Carry Flat - Side", "Carry Side", "Side"), LENGTH, "yds", "Carry side"),
    Field("land_angle", ("Carry Flat - Land. Angle", "Landing Angle", "Land. Angle"), ANGLE, "deg", "Landing angle"),
    Field("land_ball_speed", ("Carry Flat - Ball Speed",), SPEED, "mph", "Landing ball speed"),
    Field("hang_time", ("Carry Flat - Time", "Hang Time"), TIME, "s", "Hang time"),
    Field("total", ("Est. Total Flat - Length", "Total", "Total Distance"), LENGTH, "yds", "Total"),
    Field("total_side", ("Est. Total Flat - Side", "Side Total", "Total Side"), LENGTH, "yds", "Total side"),
    Field("dynamic_lie", ("Dynamic Lie",), ANGLE, "deg", "Dynamic lie"),
    Field("impact_offset", ("Impact Offset",), LENGTH, "mm", "Impact offset"),
    Field("impact_height", ("Impact Height",), LENGTH, "mm", "Impact height"),
    Field("curve", ("Curve",), LENGTH, "yds", "Curve"),
    Field("spin_loft", ("Spin Loft",), ANGLE, "deg", "Spin loft"),
    Field("swing_radius", ("Swing Radius",), LENGTH, "in", "Swing radius"),
    Field("low_point_height", ("Low Point Height",), LENGTH, "mm", "Low point height"),
    Field("low_point_side", ("Low Point Side",), LENGTH, "mm", "Low point side"),
    Field("d_plane_tilt", ("D Plane Tilt",), ANGLE, "deg", "D-plane tilt"),
    Field("ball_speed_diff", ("Ball Speed Diff",), SPEED, "mph", "Ball speed diff"),
    Field("smash_index", ("Smash Index",), PERCENT, "%", "Smash index"),
    Field("spin_rate_diff", ("Spin Rate Diff",), SPIN, "rpm", "Spin rate diff"),
    Field("spin_index", ("Spin Index",), PERCENT, "%", "Spin index"),
    Field("gyro_angle", ("Gyro Angle",), ANGLE, "deg", "Gyro angle"),
)

TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "date": ("Date",),
    "tmd_no": ("TMD No",),
    "player": ("Player",),
    "club": ("Club",),
    "ball": ("Ball",),
    "spin_rate_type": ("Spin Rate Type",),
    "use_in_stat": ("Use In Stat",),
    "tags": ("Tags",),
    "condition": ("Condition",),
}

DROPPED_HEADERS: frozenset[str] = frozenset({"email", "tmdfilename"})

KNOWN_UNMAPPED: frozenset[str] = frozenset({
    "spinaxissim", "curvesim", "carrysim", "totalsim",
    "carrysidesim", "totalsidesim", "landinganglesim",
})

FIELDS_BY_KEY: dict[str, Field] = {f.key: f for f in NUMERIC_FIELDS}


def norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _build_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for key, headers in TEXT_FIELDS.items():
        for h in headers:
            lookup[norm_header(h)] = key
    for f in NUMERIC_FIELDS:
        for h in f.headers:
            lookup.setdefault(norm_header(h), f.key)
    return lookup


HEADER_LOOKUP: dict[str, str] = _build_lookup()
