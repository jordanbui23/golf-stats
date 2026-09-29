from __future__ import annotations

import statistics

from .stats import Session, fmt, is_normalized, session_summary, values

_TABLE = (
    ("Carry", "carry", 0, False, True),
    ("Side", "carry_side", 0, True, True),
    ("Club spd", "club_speed", 1, False, False),
    ("Smash", "smash_factor", 2, False, False),
    ("AoA", "attack_angle", 1, True, False),
    ("Path", "club_path", 1, True, True),
    ("Face", "face_angle", 1, True, True),
    ("F2P", "face_to_path", 1, True, True),
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


def _cell(summary: dict | None, digits: int, signed: bool, with_sd: bool) -> str:
    if not summary:
        return "–"
    text = fmt(summary["median"], digits, signed)
    if with_sd and summary["sd"] is not None:
        text += f" ±{summary['sd']:.{digits}f}"
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
        out.append(f"Club data (face, face to path, loft) is missing on {len(no_face)} of {len(counted)} shots.")
    no_impact = [s for s in counted if s.get("impact_offset") is None]
    if no_impact and len(no_impact) == len(counted):
        out.append("No impact location on any shot. Check that OERT is on and the hitting area is lit.")
    excluded = len(session.shots) - len(counted)
    if excluded:
        out.append(f"{excluded} shot(s) marked \"Use In Stat = FALSE\" in TPS are left out of every number.")
    return out


def render_report(session: Session, history: list[Session], focus: dict | None, grades: list[dict] | None,
                  plan_path: str | None = None) -> str:
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
        unit = "°" if focus["unit"] == "deg" else f" {focus['unit']}"
        window = f"at least {lo:.0f}{unit}" if hi >= 200 else f"between {lo:+g}{unit} and {hi:+g}{unit}"
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
            pct = f"{g['hits'] / g['n']:.0%}" if g["n"] else "–"
            before = f"{base['hits']} of {base['n']}" if base["n"] else "–"
            lines.append(f"- {g['club']} {g['label'].lower()} in [{lo:+g}, {hi:+g}]: **{g['hits']} of {g['n']}** "
                         f"({pct}), planned {g['shots']}. When the plan was set: {before}.")
        lines.append("")

    lines += ["## By club", "", "Medians, with ± a robust spread (1.4826 × MAD). Distances in yds, angles in °, "
              "speeds in mph, spin in rpm, low point in inches (+ is ahead of the ball).", ""]
    lines.append("| Club | Shots | " + " | ".join(h for h, *_ in _TABLE) + " |")
    lines.append("|---|---|" + "---|" * len(_TABLE))
    for code, c in summ.items():
        cells = [_cell(c["metrics"][k], d, sg, sd) for _, k, d, sg, sd in _TABLE]
        lines.append(f"| {code} | {c['n']} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Shot shape and side miss", ""]
    for code, c in summ.items():
        shapes = ", ".join(f"{k} {v}" for k, v in c["shapes"].items()) or "–"
        sides = c["sides"]
        split = ""
        if sides:
            split = (f" Side miss ±{sides['side_sd'] or 0:.1f} yds: start line ±{sides['start_sd']:.1f}, "
                     f"curve ±{sides['curve_sd']:.1f} ({sides['dominant']} dominates).")
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
