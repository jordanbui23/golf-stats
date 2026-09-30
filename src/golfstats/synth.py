from __future__ import annotations

import csv
import io
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

TPS_HEADER = [
    "Date", "TMD No", "TMD Filename", "Player", "Club", "Ball", "Club Speed", "Attack Angle", "Club Path",
    "Low Point", "Swing Plane", "Swing Direction", "Dyn. Loft", "Face Angle", "Face To Path", "Ball Speed",
    "Smash Factor", "Launch Angle", "Launch Direction", "Spin Rate", "Spin Rate Type", "Spin Axis",
    "Max Height - Dist.", "Max Height - Height", "Max Height - Side", "Last data Point - Length",
    "Last data Point - Side", "Last data Point - Height", "Last data Point - Time", "Carry Flat - Length",
    "Carry Flat - Side", "Carry Flat - Land. Angle", "Carry Flat - Ball Speed", "Carry Flat - Time",
    "Est. Total Flat - Length", "Est. Total Flat - Side", "Dynamic Lie", "Impact Offset", "Impact Height",
    "Curve", "Spin Axis (Sim)", "Curve (Sim)", "Carry (Sim)", "Total (Sim)", "Carry Side (Sim)",
    "Total side (Sim)", "Landing Angle (Sim)", "Spin Loft", "Swing Radius", "Low Point Height",
    "Low Point Side", "D Plane Tilt", "Ball Speed Diff", "Smash Index", "Spin Rate Diff", "Spin Index",
    "Gyro Angle", "Use In Stat", "Tags", "Condition", "Email",
]
TPS_UNITS = [
    "", "", "", "", "", "", "[mph]", "[deg]", "[deg]", "[in]", "[deg]", "[deg]", "[deg]", "[deg]", "[deg]",
    "[mph]", "[]", "[deg]", "[deg]", "[rpm]", "[]", "[deg]", "[ft]", "[ft]", "[ft]", "[yds]", "[yds]",
    "[yds]", "[s]", "[yds]", "[yds]", "[deg]", "[mph]", "[s]", "[yds]", "[yds]", "[deg]", "[mm]", "[mm]",
    "[ft]", "[deg]", "[ft]", "[yds]", "[yds]", "[ft/in]", "[ft/in]", "[deg]", "[deg]", "[in]", "[mm]", "[mm]",
    "[deg]", "[mph]", "[%]", "[rpm]", "[%]", "[deg]", "", "", "", "",
]


@dataclass
class ClubModel:
    name: str
    speed: float
    smash: float
    attack: float
    loft: float
    spin: float
    carry: float
    face_weight: float


CLUBS = {
    "Driver": ClubModel("Driver", 101, 1.46, 2.0, 13.5, 2700, 238, 0.85),
    "7 Iron": ClubModel("7 Iron", 82, 1.36, -4.5, 20.0, 6300, 160, 0.75),
    "Pitching Wedge": ClubModel("Pitching Wedge", 74, 1.24, -5.2, 30.0, 8600, 124, 0.70),
}


def synth_shot(rng: random.Random, club: ClubModel, face_bias: float, path_bias: float,
               strike_sd: float = 6.0) -> dict:
    speed = rng.gauss(club.speed, 2.0)
    attack = rng.gauss(club.attack, 0.8)
    path = rng.gauss(path_bias, 1.8)
    face = rng.gauss(face_bias, 1.6)
    f2p = face - path
    offset = rng.gauss(0, strike_sd)
    height = rng.gauss(3.0 if club.name == "Driver" else 0.0, 5.0)
    smash_index = min(104.0, 101.0 - abs(offset) * 0.45 + rng.gauss(0, 1.2))
    smash = club.smash * smash_index / 100
    ball = speed * smash
    loft = rng.gauss(club.loft, 1.2)
    launch = loft * (0.83 if club.name == "Driver" else 0.78)
    axis = 2.0 * f2p + rng.gauss(0, 1.0)
    carry = club.carry * (ball / (club.speed * club.smash)) ** 1.4 * rng.gauss(1.0, 0.015)
    launch_dir = club.face_weight * face + (1 - club.face_weight) * path
    curve_yds = carry * 0.009 * axis
    side = carry * math.tan(math.radians(launch_dir)) + curve_yds
    low_point = (-1.5 if club.name == "Driver" else -0.8) * attack + rng.gauss(0, 0.6)
    spin = club.spin * rng.gauss(1.0, 0.06) + height * -60
    return {
        "club_speed": speed, "attack": attack, "path": path, "face": face, "f2p": f2p, "ball": ball,
        "smash": smash, "launch": launch, "launch_dir": launch_dir, "spin": spin, "axis": axis,
        "carry": carry, "side": side, "curve_ft": curve_yds * 3, "low_point": low_point, "offset": offset,
        "height": height, "smash_index": smash_index, "loft": loft,
    }


CLUB_DATA_HEADERS = (
    "Attack Angle", "Club Path", "Low Point", "Dyn. Loft", "Face Angle", "Face To Path", "Spin Axis", "Curve",
    "Dynamic Lie", "Impact Offset", "Impact Height", "Spin Loft", "Swing Radius", "Low Point Height",
    "Low Point Side", "D Plane Tilt", "Smash Index",
)


def _row(ts: datetime, player: str, club: ClubModel, v: dict, use: bool, estimated: bool,
         drop_club_data: bool, no_club_data: bool = False) -> list[str]:
    def n(x: float) -> str:
        return repr(float(x))

    total = v["carry"] * (1.12 if club.name == "Driver" else 1.04)
    row = {h: "" for h in TPS_HEADER}
    row.update({
        "Date": f"{ts.month}/{ts.day}/{ts.year} {ts.strftime('%I:%M:%S %p').lstrip('0')}",
        "Player": player, "Club": club.name, "Ball": "Premium",
        "Club Speed": n(v["club_speed"]), "Attack Angle": n(v["attack"]), "Club Path": n(v["path"]),
        "Low Point": n(v["low_point"]), "Swing Plane": n(60 + v["attack"] * 0.2),
        "Swing Direction": n(v["path"] - v["attack"] * 0.9), "Ball Speed": n(v["ball"]),
        "Smash Factor": n(v["smash"]), "Launch Angle": n(v["launch"]), "Launch Direction": n(v["launch_dir"]),
        "Spin Rate": n(v["spin"]), "Spin Rate Type": "Estimated" if estimated else "Measured",
        "Spin Axis": n(v["axis"]), "Max Height - Dist.": n(v["carry"] * 3 * 0.6),
        "Max Height - Height": n(v["launch"] * 5.2), "Max Height - Side": n(v["side"] * 1.5),
        "Last data Point - Length": n(3.2), "Last data Point - Side": n(0.0),
        "Last data Point - Height": n(0.9), "Last data Point - Time": n(0.06),
        "Carry Flat - Length": n(v["carry"]), "Carry Flat - Side": n(v["side"]),
        "Carry Flat - Land. Angle": n(35 + v["launch"]), "Carry Flat - Ball Speed": n(v["ball"] * 0.45),
        "Carry Flat - Time": n(v["carry"] / 30), "Est. Total Flat - Length": n(total),
        "Est. Total Flat - Side": n(v["side"] * 1.1), "Dynamic Lie": n(61.0),
        "Impact Offset": n(v["offset"]), "Impact Height": n(v["height"]), "Curve": n(v["curve_ft"]),
        "Spin Loft": n(v["loft"] - v["attack"]), "Swing Radius": n(38.5), "Low Point Height": n(-4),
        "Low Point Side": n(-8), "D Plane Tilt": n(v["axis"] * 1.1), "Smash Index": n(v["smash_index"]),
        "Use In Stat": "TRUE" if use else "FALSE", "Condition": "",
    })
    if not drop_club_data:
        row.update({"Dyn. Loft": n(v["loft"]), "Face Angle": n(v["face"]), "Face To Path": n(v["f2p"])})
    if no_club_data:
        row.update({h: "" for h in CLUB_DATA_HEADERS})
        row["Carry Flat - Side"] = n(v["carry"] * math.tan(math.radians(v["launch_dir"])))
    return [row[h] for h in TPS_HEADER]


def synth_session_csv(start: datetime, plan: list[tuple[str, int]], face_bias: float, path_bias: float,
                      seed: int, player: str = "Demo", strike_sd: float = 6.0, club_data_every: int = 1) -> str:
    rng = random.Random(seed)
    buf = io.StringIO()
    buf.write("\ufeffsep=,\r\n")
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(TPS_HEADER)
    w.writerow(TPS_UNITS)
    ts = start
    for club_name, count in plan:
        club = CLUBS[club_name]
        for i in range(count):
            v = synth_shot(rng, club, face_bias, path_bias, strike_sd)
            w.writerow(_row(ts, player, club, v, use=(i % 17 != 16), estimated=(i % 11 == 10),
                            drop_club_data=(i % 23 == 22), no_club_data=(i % club_data_every != 0)))
            ts += timedelta(seconds=rng.randint(35, 70))
    return buf.getvalue()


def demo_exports() -> list[tuple[str, str]]:
    plan = [("Pitching Wedge", 12), ("7 Iron", 30), ("Driver", 14)]
    arcs = [(0.6, -3.6, 9.0), (0.4, -2.8, 7.5), (0.1, -1.9, 6.0), (-0.2, -1.2, 5.0)]
    out = []
    for i, (face, path, strike) in enumerate(arcs):
        start = datetime(2026, 9, 7 + 7 * i, 18, 5)
        out.append((f"demo-{start:%Y%m%d}.csv", synth_session_csv(start, plan, face, path, 100 + i, strike_sd=strike)))
    return out
