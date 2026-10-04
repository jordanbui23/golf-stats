from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

from .clubs import club_order, is_iron_or_wedge
from .config import Config
from .fields import FIELDS_BY_KEY
from .stats import Session, by_club, club_summary, fmt, is_counted, robust_sd, values
from .strike import direction_words, strike_references, strike_summary

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
    counts = {code: sum(1 for s in group if is_counted(s)) for code, group in by_club(session.shots).items()}
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


def _enough(summary: dict | None, cfg: Config) -> bool:
    return bool(summary) and summary["n"] >= cfg.min_shots


def _outside(summary: dict, window: tuple[float, float]) -> bool:
    return not in_window(summary["median"], window) or (summary["sd"] or 0) > (window[1] - window[0]) / 2


def _window_text(window: tuple[float, float], unit: str) -> str:
    u = "°" if unit == "deg" else f" {unit}"
    return f"window {window[0]:+.0f}{u} to {window[1]:+.0f}{u}"


def _club_data_note(m: dict, key: str, n: int) -> str:
    have = m[key]["n"] if m[key] else 0
    return f"{FIELDS_BY_KEY[key].label} was measured on only {have} of {n} shots, so this uses "


def pick_focus(session: Session, cfg: Config, history: list[Session] | None = None) -> dict | None:
    club = primary_club(session, cfg)
    if club is None:
        return None
    shots = [s for s in session.shots if s["club_code"] == club and is_counted(s)]
    summ = club_summary(shots)
    m, n = summ["metrics"], summ["n"]

    refs = strike_references(session, history or [session], cfg.mishit_smash_ratio, cfg.min_shots)
    st = strike_summary(shots, refs.get(club))
    if st and st["n"] >= cfg.min_shots and st["share"] > cfg.mishit_share_max:
        cost = (f" They started {direction_words(st['start_mishit'])} on median and carried "
                f"{st['carry_lost']:.0f} yds less than your solid strikes." if st["carry_lost"] is not None else "")
        return _focus(club, "smash_factor", (st["threshold"], 200.0), shots,
                      f"{st['mishits']} of {st['n']} shots were mishits: smash factor under {st['threshold']:.3f}, "
                      f"which is {cfg.mishit_smash_ratio:.0%} of your best with this club ({st['best']:.2f})."
                      + cost + " Strike comes before direction.")

    lp, lp_behind = m["low_point"], summ["low_point_behind_share"]
    if is_iron_or_wedge(club) and _enough(lp, cfg) and lp_behind > cfg.low_point_behind_share:
        behind = round(lp_behind * lp["n"])
        return _focus(club, "low_point", LOW_POINT_WINDOW, shots,
                      f"The club bottomed out at or behind the ball on {behind} of {lp['n']} shots "
                      f"(median low point {fmt(lp['median'], 1, True)} in). "
                      "Strike comes before direction.")

    si = m["smash_index"]
    if _enough(si, cfg) and si["median"] < cfg.smash_index_min:
        return _focus(club, "smash_index", (cfg.smash_index_min, 200.0), shots,
                      f"Median smash index {si['median']:.0f}% (target {cfg.smash_index_min:.0f}% or more). "
                      "Contact is costing ball speed.")

    off = m["impact_offset"]
    if _enough(off, cfg) and off["sd"] is not None and off["sd"] > cfg.impact_offset_spread_mm:
        t = cfg.impact_offset_spread_mm
        return _focus(club, "impact_offset", (-t, t), shots,
                      f"Heel-to-toe impact spread is ±{off['sd']:.0f} mm (target ±{t:.0f} mm), "
                      f"median {off['median']:+.0f} mm.")

    f2p_w, face_w = cfg.face_to_path_window, cfg.face_window
    sides = summ["sides"] if summ["sides"] and summ["sides"]["n"] >= cfg.min_shots else None
    path = m["club_path"] if _enough(m["club_path"], cfg) else None
    curve = m["face_to_path"] if _enough(m["face_to_path"], cfg) else None
    start_metric = "face_angle" if _enough(m["face_angle"], cfg) else "launch_direction"
    start = m[start_metric]

    checks = []
    if curve and _outside(curve, f2p_w):
        spread = (f"Curve explains more of the side miss than start line (curve ±{sides['curve_sd']:.1f} yds, "
                  f"start ±{sides['start_sd']:.1f} yds). " if sides and sides["dominant"] == "curve" else "")
        detail = (f"Face to path median {curve['median']:+.1f}°, spread ±{curve['sd'] or 0:.1f}° "
                  f"({_window_text(f2p_w, 'deg')}). "
                  + _path_words(path["median"] if path else None, curve["median"]))
        checks.append(("curve", _focus(club, "face_to_path", f2p_w, shots, spread + detail)))
    if _enough(start, cfg) and _outside(start, face_w):
        spread = (f"Start line explains more of the side miss than curve (start ±{sides['start_sd']:.1f} yds, "
                  f"curve ±{sides['curve_sd']:.1f} yds). " if sides and sides["dominant"] == "start" else "")
        if start_metric == "face_angle":
            detail = (f"Face angle median {start['median']:+.1f}°, spread ±{start['sd'] or 0:.1f}° "
                      f"({_window_text(face_w, 'deg')}). The ball starts close to where the face points.")
        else:
            detail = (_club_data_note(m, "face_angle", n)
                      + f"launch direction, which TrackMan measures on every shot and which mostly follows "
                      f"the face. Median {start['median']:+.1f}°, spread ±{start['sd'] or 0:.1f}° "
                      f"({_window_text(face_w, 'deg')}). Curve is not checked: without club data TrackMan "
                      "has no spin axis and draws every flight straight.")
        checks.append(("start", _focus(club, start_metric, face_w, shots, spread + detail)))
    if checks:
        dominant = sides["dominant"] if sides else "curve"
        checks.sort(key=lambda c: c[0] != dominant)
        return checks[0][1]

    hold = ("face_to_path", f2p_w) if curve else (start_metric, face_w) if _enough(start, cfg) else None
    if hold is None:
        return None
    return _focus(club, hold[0], hold[1], shots,
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


def plan_for(session: Session, sessions: list[Session], cfg: Config) -> dict | None:
    earlier = [s for s in sessions if s.player == session.player and s.start < session.start]
    if not earlier:
        return None
    prev = max(earlier, key=lambda s: s.start)
    focus = pick_focus(prev, cfg, sessions)
    return make_plan(prev, focus, cfg) if focus else None


def grade_plan(plan: dict, session: Session) -> list[dict]:
    grades = []
    for block in plan["blocks"]:
        window = tuple(block["window"])
        shots = [s for s in session.shots if s["club_code"] == block["club"] and is_counted(s)]
        shots = shots[: block["shots"]]
        hits, n = window_hits(shots, block["metric"], window)
        field = FIELDS_BY_KEY[block["metric"]]
        grades.append({**block, "hits": hits, "n": n, "label": field.label, "unit": field.unit})
    return grades
