from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib.resources import files
from pathlib import Path

from .config import Config
from .fields import FIELDS_BY_KEY
from .focus import grade_plan, pick_focus, plan_for
from .stats import MIN_SPREAD_N, Session, by_club, club_summary, shape_label

SHOT_COLUMNS = (
    "carry", "carry_side", "curve", "face_angle", "club_path", "face_to_path", "launch_direction",
    "impact_offset", "impact_height", "low_point", "attack_angle", "club_speed", "ball_speed",
    "smash_factor", "smash_index", "spin_rate", "spin_axis", "launch_angle", "dynamic_loft", "total",
)
TREND_METRICS = (
    ("carry", "median"), ("carry", "sd"), ("carry_side", "sd"), ("face_to_path", "median"),
    ("face_to_path", "sd"), ("face_angle", "median"), ("face_angle", "sd"), ("club_path", "median"),
    ("club_speed", "median"), ("smash_index", "median"), ("attack_angle", "median"), ("low_point", "median"),
    ("impact_offset", "sd"), ("spin_rate", "median"), ("launch_direction", "median"),
    ("launch_direction", "sd"), ("curve", "median"), ("curve", "sd"),
)


def _round(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)


def _club_block(shots: list[dict]) -> dict:
    c = club_summary(shots)
    metrics = {k: {"median": _round(v["median"]), "sd": _round(v["sd"]), "n": v["n"]}
               for k, v in c["metrics"].items() if v}
    sides = {k: (_round(v) if isinstance(v, float) else v) for k, v in c["sides"].items()} if c["sides"] else None
    return {"n": c["n"], "n_total": c["n_total"], "metrics": metrics, "shapes": c["shapes"], "sides": sides}


def build_data(sessions: list[Session], cfg: Config, player: str = "") -> dict:
    out_sessions, shots = [], []
    for idx, sess in enumerate(sessions):
        plan = plan_for(sess, sessions, cfg)
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
        "player": player,
        "min_shots": cfg.min_shots,
        "min_spread_n": MIN_SPREAD_N,
        "shot_columns": ["session", "club", "use", "shape", "time", *SHOT_COLUMNS],
        "labels": {k: {"label": f.label, "unit": f.unit} for k, f in FIELDS_BY_KEY.items()},
        "trend_metrics": [list(t) for t in TREND_METRICS],
        "windows": {"face_to_path": list(cfg.face_to_path_window), "face_angle": list(cfg.face_window),
                    "launch_direction": list(cfg.face_window)},
        "sessions": out_sessions,
        "shots": shots,
    }


def render_dashboard(sessions: list[Session], cfg: Config, player: str = "") -> str:
    template = files("golfstats").joinpath("dashboard.html").read_text()
    payload = json.dumps(build_data(sessions, cfg, player), separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return template.replace("/*__DATA__*/null", payload)


def player_slug(player: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", player.lower()).strip("-") or "player"


@contextmanager
def dashboard_lock(cfg: Config) -> Iterator[None]:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.data_dir / ".dashboards.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dashboard_slugs(players: list[str], cfg: Config) -> dict[str, str]:
    path = cfg.dashboard_index
    slugs: dict[str, str] = json.loads(path.read_text()) if path.exists() else {}
    used = set(slugs.values())
    for player in sorted(set(players) - set(slugs), key=lambda p: (p.lower(), p)):
        slug = base = player_slug(player)
        suffix = 2
        while slug in used:
            slug, suffix = f"{base}-{suffix}", suffix + 1
        slugs[player] = slug
        used.add(slug)
    write_atomic(path, json.dumps(slugs, indent=2, sort_keys=True) + "\n")
    return slugs


def write_dashboards(sessions: list[Session], cfg: Config) -> list[tuple[str, Path]]:
    by_player: dict[str, list[Session]] = {}
    for sess in sessions:
        by_player.setdefault(sess.player, []).append(sess)
    written = []
    with dashboard_lock(cfg):
        slugs = dashboard_slugs(list(by_player), cfg)
        for player in sorted(by_player, key=lambda p: (p.lower(), p)):
            path = cfg.dashboard_path(slugs[player])
            write_atomic(path, render_dashboard(by_player[player], cfg, player))
            written.append((player, path))
    return written
