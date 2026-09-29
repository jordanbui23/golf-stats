from __future__ import annotations

import json
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from .config import Config
from .fields import FIELDS_BY_KEY
from .focus import grade_plan, pick_focus, plan_before
from .stats import Session, by_club, club_summary, shape_label

SHOT_COLUMNS = (
    "carry", "carry_side", "curve", "face_angle", "club_path", "face_to_path", "launch_direction",
    "impact_offset", "impact_height", "low_point", "attack_angle", "club_speed", "ball_speed",
    "smash_factor", "smash_index", "spin_rate", "spin_axis", "launch_angle", "dynamic_loft", "total",
)
TREND_METRICS = (
    ("carry", "median"), ("carry", "sd"), ("carry_side", "sd"), ("face_to_path", "median"),
    ("face_to_path", "sd"), ("face_angle", "median"), ("face_angle", "sd"), ("club_path", "median"),
    ("club_speed", "median"), ("smash_index", "median"), ("attack_angle", "median"), ("low_point", "median"),
    ("impact_offset", "sd"), ("spin_rate", "median"),
)


def _round(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)


def _club_block(shots: list[dict]) -> dict:
    c = club_summary(shots)
    metrics = {k: {"median": _round(v["median"]), "sd": _round(v["sd"]), "n": v["n"]}
               for k, v in c["metrics"].items() if v}
    sides = {k: (_round(v) if isinstance(v, float) else v) for k, v in c["sides"].items()} if c["sides"] else None
    return {"n": c["n"], "n_total": c["n_total"], "metrics": metrics, "shapes": c["shapes"], "sides": sides}


def build_data(sessions: list[Session], cfg: Config) -> dict:
    out_sessions, shots = [], []
    for idx, sess in enumerate(sessions):
        plan = plan_before(sess, cfg.plans)
        out_sessions.append({
            "id": sess.id, "label": sess.label, "player": sess.player, "start": sess.start.isoformat(),
            "shots": len(sess.shots), "counted": len(sess.counted),
            "clubs": {code: _club_block(group) for code, group in by_club(sess.shots).items()},
            "focus": pick_focus(sess, cfg),
            "grades": grade_plan(plan, sess) if plan else None,
        })
        for s in sess.shots:
            shots.append([idx, s["club_code"], int(s["use_in_stat"]), shape_label(s),
                          s["ts"].strftime("%H:%M:%S"), *(_round(s.get(k)) for k in SHOT_COLUMNS)])
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "shot_columns": ["session", "club", "use", "shape", "time", *SHOT_COLUMNS],
        "labels": {k: {"label": f.label, "unit": f.unit} for k, f in FIELDS_BY_KEY.items()},
        "trend_metrics": [list(t) for t in TREND_METRICS],
        "windows": {"face_to_path": list(cfg.face_to_path_window), "face_angle": list(cfg.face_window)},
        "sessions": out_sessions,
        "shots": shots,
    }


def render_dashboard(sessions: list[Session], cfg: Config) -> str:
    template = files("golfstats").joinpath("dashboard.html").read_text()
    payload = json.dumps(build_data(sessions, cfg), separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*__DATA__*/null", payload)


def write_dashboard(sessions: list[Session], cfg: Config, path: Path | None = None) -> Path:
    path = path or cfg.dashboard
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dashboard(sessions, cfg))
    return path
