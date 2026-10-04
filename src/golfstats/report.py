from __future__ import annotations

import statistics
from pathlib import Path

from .config import Config
from .stats import MIN_SPREAD_N, Session, by_club, fmt, is_normalized, session_summary, values
from .strike import bag, direction_words, strike_references, strike_summary

_TABLE = (
    ("Carry", "carry", 0, False, True),
    ("Side", "carry_side", 0, True, True),
    ("Club spd", "club_speed", 1, False, False),
    ("Smash", "smash_factor", 2, False, False),
    ("AoA", "attack_angle", 1, True, False),
    ("Path", "club_path", 1, True, True),
    ("Face", "face_angle", 1, True, True),
    ("F2P", "face_to_path", 1, True, True),
    ("Start", "launch_direction", 1, True, True),
    ("Curve", "curve", 1, True, True),
    ("Launch", "launch_angle", 1, False, False),
    ("Spin", "spin_rate", 0, False, False),
    ("Low pt", "low_point", 1, True, False),
)

_TREND = (
    ("Carry median", "carry", "median", 0, False),
    ("Carry spread", "carry", "sd", 1, False),
    ("Side spread", "carry_side", "sd", 1, False),
    ("Face to path", "face_to_path", "median", 1, True),
    ("Face spread", "face_angle", "sd", 1, False),
    ("Club speed", "club_speed", "median", 1, False),
    ("Smash index", "smash_index", "median", 0, False),
)


def unit_suffix(unit: str) -> str:
    return "°" if unit == "deg" else unit if unit == "%" else f" {unit}" if unit else ""


def _cell(summary: dict | None, digits: int, signed: bool, with_sd: bool, club_n: int) -> str:
    if not summary:
        return "–"
    text = fmt(summary["median"], digits, signed)
    if with_sd and summary["sd"] is not None:
        text += f" ±{summary['sd']:.{digits}f}"
    if summary["n"] < club_n:
        text += f" ({summary['n']})"
    return text


def _warnings(session: Session, summ: dict) -> list[str]:
    out = []
    if is_normalized(session):
        out.append("This export is **normalized** (TPS \"Normalize data\" was on). Export again with it unchecked "
                   "for raw numbers.")
    counted = session.counted
    tracked = values(counted, "last_data_length")
    if tracked and statistics.median(tracked) < 20:
        out.append(f"The radar tracked the ball for a median {statistics.median(tracked):.1f} yds, so carry, side "
                   "and curve are predicted from launch and spin. Club and launch numbers are the measured ones.")
    est = [s for s in counted if (s.get("spin_rate_type") or "").lower() == "estimated"]
    if est:
        out.append(f"Spin was estimated, not measured, on {len(est)} of {len(counted)} shots. RCT balls fix this.")
    no_face = [s for s in counted if s.get("face_angle") is None]
    if no_face:
        out.append(f"Club data (face, path, face to path, attack angle, low point, spin axis) is missing on "
                   f"{len(no_face)} of {len(counted)} shots. Without a spin axis TrackMan draws those flights "
                   "straight, so their side is start line only and their curve is unknown. A check that needs "
                   "club data runs only with at least `focus.min_shots` measured values. Otherwise start line "
                   "is checked with launch direction.")

    no_impact = [s for s in counted if s.get("impact_offset") is None]
    if no_impact and len(no_impact) == len(counted):
        out.append("No impact location on any shot. Check that OERT is on and the hitting area is lit.")
    excluded = sum(1 for s in session.shots if not s["use_in_stat"])
    if excluded:
        out.append(f"{excluded} shot(s) marked \"Use In Stat = FALSE\" in TPS are left out of every number.")
    if session.no_reads:
        out.append(f"{len(session.no_reads)} shot(s) have no ball data (a misread, TPS shows zeros) and are left "
                   "out of every number.")
    return out


def bound_text(x: float) -> str:
    return f"{x:.3f}" if abs(x) < 10 else f"{x:g}"


def _strike_lines(session: Session, refs: dict[str, dict], cfg: Config) -> list[str]:
    rows, mishits, solid = [], 0, 0
    data_mishit = data_solid = 0
    for code, group in by_club(session.counted).items():
        st = strike_summary(group, refs.get(code))
        if not st or not st["n"]:
            continue
        mishits, solid = mishits + st["mishits"], solid + st["solid"]
        data_mishit, data_solid = data_mishit + st["club_data_mishit"], data_solid + st["club_data_solid"]
        text = f"- **{code}**: {st['mishits']} of {st['n']} mishits (smash factor under {st['threshold']:.3f})."
        if st["mishits"] and st["solid"]:
            lost = f" and carried {st['carry_lost']:.0f} yds less" if st["carry_lost"] is not None else ""
            carried = f" and carried {st['carry_solid']:.0f} yds" if st["carry_solid"] is not None else ""
            text += (f" Mishits started {direction_words(st['start_mishit'])}{lost}. "
                     f"Solid strikes started {direction_words(st['start_solid'])}{carried}.")
        if st["unrated"]:
            text += f" {st['unrated']} shot(s) had no club speed, so they are not rated."
        rows.append(text)
    if not rows:
        return []
    lines = ["## Strike", "",
             f"A mishit is a shot with smash factor under {cfg.mishit_smash_ratio:.0%} of your best with that club "
             f"(the 90th percentile of every session so far, at least {cfg.min_shots} shots). Smash factor needs "
             "only club speed and ball speed, so this works on shots without face and path data.", "", *rows]
    unrated = [code for code in by_club(session.counted) if code not in refs]
    if unrated:
        lines += ["", f"Not rated yet: {', '.join(unrated)}, fewer than {cfg.min_shots} shots with smash factor "
                      "so far."]
    if mishits:
        lines += ["", f"TrackMan recorded club data on {data_mishit} of {mishits} mishits and {data_solid} of {solid} "
                      "solid strikes. Poor contact is one reason club data goes missing."]
    return lines + [""]


def _bag_lines(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    lines = ["## Bag", "",
             "Median carry per club over every session so far, solid strikes only where strike is rated. Gap is "
             "the distance to the next shorter club. Indoors, carry is predicted from launch and spin.", "",
             "| Club | Carry | Spread | Gap | Shots |", "|---|---|---|---|---|"]
    for r in rows:
        spread = f"±{r['sd']:.0f}" if r["sd"] is not None else "–"
        gap = f"{r['gap']:.0f}" if r["gap"] is not None else "–"
        who = "" if r["solid_only"] else " (all)"
        lines.append(f"| {r['club']} | {r['carry']:.0f} | {spread} | {gap} | {r['n']}{who} |")
    return lines + [""]


def render_report(session: Session, history: list[Session], focus: dict | None, grades: list[dict] | None,
                  plan_path: str | None = None, cfg: Config | None = None) -> str:
    cfg = cfg or Config(data_dir=Path("."))
    refs = strike_references(session, history, cfg.mishit_smash_ratio, cfg.min_shots)
    summ = session_summary(session)
    minutes = (session.end - session.start).total_seconds() / 60
    lines = [
        f"# Session {session.id}" + (f" ({session.player})" if session.player else ""),
        "",
        f"{session.label}. {len(session.shots)} shots, {len(session.counted)} counted, {minutes:.0f} min. "
        f"Clubs: {', '.join(summ)}.",
        "",
    ]
    warnings = _warnings(session, summ)
    if warnings:
        lines += ["## Data notes", ""] + [f"- {w}" for w in warnings] + [""]

    lines += ["## Focus for next session", ""]
    if focus:
        lo, hi = focus["window"]
        unit = unit_suffix(focus["unit"])
        window = f"at least {bound_text(lo)}{unit}" if hi >= 200 else f"between {lo:+g}{unit} and {hi:+g}{unit}"
        t = focus["today"]
        lines += [
            f"**{focus['club']}: {focus['label'].lower()} {window}.**",
            "",
            focus["reason"],
            "",
            f"Today {t['hits']} of {t['n']} shots were in that window.",
        ]
        if plan_path:
            lines += ["", f"Plan saved to `{plan_path}`. The next ingest grades it."]
    else:
        lines.append("No focus yet: hit at least a few shots with one club so the checks have enough data.")
    lines.append("")

    if grades:
        lines += ["## Last plan", ""]
        for g in grades:
            lo, hi = g["window"]
            base = g["baseline"]
            if not g["n"]:
                lines.append(f"- {g['club']} {g['label'].lower()}: not graded, no {g['club']} shots with that "
                             "number this session.")
                continue
            pct = f"{g['hits'] / g['n']:.0%}"
            before = f"{base['hits']} of {base['n']}" if base["n"] else "–"
            window = f"at least {bound_text(lo)}" if hi >= 200 else f"in [{lo:+g}, {hi:+g}]"
            lines.append(f"- {g['club']} {g['label'].lower()} {window}: **{g['hits']} of {g['n']}** "
                         f"({pct}), planned {g['shots']}. When the plan was set: {before}.")
        lines.append("")

    lines += _strike_lines(session, refs, cfg)
    lines += _bag_lines(bag(session, history, refs))

    lines += ["## By club", "", "Medians, with ± a robust spread (1.4826 × MAD). Distances in yds, angles in °, "
              "speeds in mph, spin in rpm, low point in inches (+ is ahead of the ball). Start is launch "
              "direction. A number in brackets is how many shots had that value, when fewer than all. "
              f"A spread needs at least {MIN_SPREAD_N} shots.", ""]
    lines.append("| Club | Shots | " + " | ".join(h for h, *_ in _TABLE) + " |")
    lines.append("|---|---|" + "---|" * len(_TABLE))
    for code, c in summ.items():
        cells = [_cell(c["metrics"][k], d, sg, sd, c["n"]) for _, k, d, sg, sd in _TABLE]
        lines.append(f"| {code} | {c['n']} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Shot shape and side miss", ""]
    for code, c in summ.items():
        shapes = ", ".join(f"{k} {v}" for k, v in c["shapes"].items()) or "–"
        sides = c["sides"]
        split = ""
        if sides:
            split = (f" On the {sides['n']} shots with curve, side miss ±{sides['side_sd'] or 0:.1f} yds: start "
                     f"line ±{sides['start_sd']:.1f}, curve ±{sides['curve_sd']:.1f} ({sides['dominant']} dominates).")
        lines.append(f"- **{code}**: {shapes}.{split}")
    lines.append("")

    earlier = [h for h in history if h.start < session.start and h.player == session.player][-5:]
    if earlier:
        prev = [session_summary(h) for h in earlier]
        lines += [f"## Compared with the previous {len(earlier)} session(s)", "",
                  "| Club | Metric | Today | Previous avg |", "|---|---|---|---|"]
        for code, c in summ.items():
            for label, key, stat, digits, signed in _TREND:
                today = (c["metrics"][key] or {}).get(stat)
                before = [p[code]["metrics"][key][stat] for p in prev
                          if code in p and p[code]["metrics"][key] and p[code]["metrics"][key][stat] is not None]
                if today is None or not before:
                    continue
                lines.append(f"| {code} | {label} | {fmt(today, digits, signed)} | "
                             f"{fmt(statistics.fmean(before), digits, signed)} |")
        lines.append("")
    return "\n".join(lines)
