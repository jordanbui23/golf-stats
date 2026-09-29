from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

from .clubs import club_order, is_iron_or_wedge
from .config import Config
from .fields import FIELDS_BY_KEY
from .stats import Session, by_club, club_summary, fmt, robust_sd, values

LOW_POINT_WINDOW = (0.5, 5.0)


def in_window(value: float | None, window: tuple[float, float]) -> bool | None:
    if value is None:
        return None
    return window[0] <= value <= window[1]


def window_hits(shots: list[dict], metric: str, window: tuple[float, float]) -> tuple[int, int]:
    flags = [in_window(s.get(metric), window) for s in shots]
    usable = [f for f in flags if f is not None]
    return sum(usable), len(usable)


def primary_club(session: Session, cfg: Config) -> str | None:
    counts = {code: sum(1 for s in group if s["use_in_stat"]) for code, group in by_club(session.shots).items()}
    eligible = [c for c, n in counts.items() if n >= cfg.min_shots and c != "PT"]
    if not eligible:
        return None
    return min(eligible, key=lambda c: (-counts[c], club_order(c)))


def _path_words(path: float | None, f2p: float | None) -> str:
    if path is None or f2p is None:
        return ""
    direction = "in-to-out" if path > 1 else "out-to-in" if path < -1 else "neutral"
    face = "open to" if f2p > 0 else "closed to" if f2p < 0 else "square to"
    return f"Path {path:+.1f}° ({direction}), face {face} it by {abs(f2p):.1f}°."


def _focus(club: str, metric: str, window: tuple[float, float], shots: list[dict], reason: str) -> dict:
    hits, n = window_hits(shots, metric, window)
    xs = values(shots, metric)
    return {
        "club": club,
        "metric": metric,
        "label": FIELDS_BY_KEY[metric].label,
        "unit": FIELDS_BY_KEY[metric].unit,
        "window": list(window),
        "today": {"hits": hits, "n": n, "median": statistics.median(xs) if xs else None, "sd": robust_sd(xs)},
        "reason": reason,
    }


def pick_focus(session: Session, cfg: Config) -> dict | None:
    club = primary_club(session, cfg)
    if club is None:
        return None
    shots = [s for s in session.shots if s["club_code"] == club and s["use_in_stat"]]
    summ = club_summary(shots)
    m = summ["metrics"]

    lp_behind = summ["low_point_behind_share"]
    if is_iron_or_wedge(club) and lp_behind is not None and lp_behind > cfg.low_point_behind_share:
        n_lp = len(values(shots, "low_point"))
        behind = round(lp_behind * n_lp)
        return _focus(club, "low_point", LOW_POINT_WINDOW, shots,
                      f"The club bottomed out at or behind the ball on {behind} of {n_lp} shots "
                      f"(median low point {fmt(m['low_point']['median'], 1, True)} in). "
                      "Strike comes before direction.")

    si = m["smash_index"]
    if si and si["median"] < cfg.smash_index_min:
        return _focus(club, "smash_index", (cfg.smash_index_min, 200.0), shots,
                      f"Median smash index {si['median']:.0f}% (target {cfg.smash_index_min:.0f}% or more). "
                      "Contact is costing ball speed.")

    off = m["impact_offset"]
    if off and off["sd"] is not None and off["sd"] > cfg.impact_offset_spread_mm:
        t = cfg.impact_offset_spread_mm
        return _focus(club, "impact_offset", (-t, t), shots,
                      f"Heel-to-toe impact spread is ±{off['sd']:.0f} mm (target ±{t:.0f} mm), "
                      f"median {off['median']:+.0f} mm.")

    f2p_w, face_w = cfg.face_to_path_window, cfg.face_window
    sides = summ["sides"]
    f2p, face, path = m["face_to_path"], m["face_angle"], m["club_path"]
    checks = []
    if f2p:
        f2p_bad = not in_window(f2p["median"], f2p_w) or (f2p["sd"] or 0) > (f2p_w[1] - f2p_w[0]) / 2
        if f2p_bad:
            spread = (f"Curve explains more of the side miss than start line (curve ±{sides['curve_sd']:.1f} yds, "
                      f"start ±{sides['start_sd']:.1f} yds). " if sides and sides["dominant"] == "curve" else "")
            checks.append(("curve", _focus(
                club, "face_to_path", f2p_w, shots,
                spread + f"Face to path median {f2p['median']:+.1f}°, spread ±{f2p['sd'] or 0:.1f}° "
                f"(window {f2p_w[0]:+.0f}° to {f2p_w[1]:+.0f}°). "
                + _path_words(path["median"] if path else None, f2p["median"]))))
    if face:
        face_bad = not in_window(face["median"], face_w) or (face["sd"] or 0) > (face_w[1] - face_w[0]) / 2
        if face_bad:
            spread = (f"Start line explains more of the side miss than curve (start ±{sides['start_sd']:.1f} yds, "
                      f"curve ±{sides['curve_sd']:.1f} yds). " if sides and sides["dominant"] == "start" else "")
            checks.append(("start", _focus(
                club, "face_angle", face_w, shots,
                spread + f"Face angle median {face['median']:+.1f}°, spread ±{face['sd'] or 0:.1f}° "
                f"(window {face_w[0]:+.0f}° to {face_w[1]:+.0f}°). The ball starts close to where the face points.")))
    if checks:
        dominant = sides["dominant"] if sides else "curve"
        checks.sort(key=lambda c: c[0] != dominant)
        return checks[0][1]

    if not f2p:
        return None
    return _focus(club, "face_to_path", f2p_w, shots,
                  "Every median and spread is inside its window. Keep the same target and get more "
                  "single shots inside it.")


def make_plan(session: Session, focus: dict, cfg: Config) -> dict:
    return {
        "created": datetime.now().isoformat(timespec="seconds"),
        "source_session": session.id,
        "player": session.player,
        "after": session.end.isoformat(),
        "blocks": [{
            "club": focus["club"],
            "metric": focus["metric"],
            "window": focus["window"],
            "shots": cfg.plan_shots,
            "baseline": {"hits": focus["today"]["hits"], "n": focus["today"]["n"]},
        }],
        "reason": focus["reason"],
    }


def save_plan(plan: dict, plans_dir: Path) -> Path:
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan['source_session']}.json"
    path.write_text(json.dumps(plan, indent=2) + "\n")
    return path


def plan_for(session: Session, sessions: list[Session], plans_dir: Path) -> dict | None:
    earlier = [s for s in sessions if s.player == session.player and s.start < session.start]
    if not earlier:
        return None
    path = plans_dir / f"{max(earlier, key=lambda s: s.start).id}.json"
    if not path.exists():
        return None
    plan = json.loads(path.read_text())
    return plan if plan.get("player", session.player) == session.player else None


def grade_plan(plan: dict, session: Session) -> list[dict]:
    grades = []
    for block in plan["blocks"]:
        window = tuple(block["window"])
        shots = [s for s in session.shots if s["club_code"] == block["club"] and s["use_in_stat"]]
        shots = shots[: block["shots"]]
        hits, n = window_hits(shots, block["metric"], window)
        grades.append({**block, "hits": hits, "n": n, "label": FIELDS_BY_KEY[block["metric"]].label})
    return grades
