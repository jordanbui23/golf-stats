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

from .clubs import club_order
from .config import Config
from .fields import FIELDS_BY_KEY
from .focus import grade_plan, pick_focus, plan_for
from .stats import MIN_SPREAD_N, Session, by_club, club_summary, is_counted, shape_label
from .strike import bag, strike_class, strike_references, strike_summary

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
    ("launch_direction", "sd"), ("curve", "median"), ("curve", "sd"), ("mishit_pct", "median"),
)


def _round(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(x, digits)


def _club_block(shots: list[dict], ref: dict | None) -> dict:
    c = club_summary(shots)
    metrics = {k: {"median": _round(v["median"]), "sd": _round(v["sd"]), "n": v["n"]}
               for k, v in c["metrics"].items() if v}
    strike = strike_summary([s for s in shots if is_counted(s)], ref)
    if strike and strike["n"]:
        metrics["mishit_pct"] = {"median": _round(100 * strike["share"], 1), "sd": None, "n": strike["n"]}
    strike = {k: _round(v, 3 if k in ("threshold", "best") else 2) if isinstance(v, float) else v
              for k, v in strike.items()} if strike else None
    sides = {k: (_round(v) if isinstance(v, float) else v) for k, v in c["sides"].items()} if c["sides"] else None
    return {"n": c["n"], "n_total": c["n_total"], "metrics": metrics, "shapes": c["shapes"], "sides": sides,
            "strike": strike}


def above_baseline(grade: dict) -> bool | None:
    base = grade["baseline"]
    if not grade["n"] or not base["n"]:
        return None
    return grade["hits"] * base["n"] > base["hits"] * grade["n"]


def overview(out_sessions: list[dict]) -> dict:
    player = out_sessions[-1]["player"] if out_sessions else ""
    mine = [(idx, sess) for idx, sess in enumerate(out_sessions) if sess["player"] == player]
    clubs: dict[str, dict] = {}
    for idx, sess in mine:
        for code, block in sess["clubs"].items():
            if not block["n"]:
                continue
            c = clubs.setdefault(code, {"club": code, "shots": 0, "sessions": 0, "last": idx})
            c["shots"] += block["n"]
            c["sessions"] += 1
            c["last"] = idx
    graded = [g["above_baseline"] for _, sess in mine for g in sess["grades"] or []
              if g["above_baseline"] is not None]
    return {
        "player": player,
        "session_indexes": [idx for idx, _ in mine],
        "sessions": len(mine),
        "counted": sum(s["counted"] for _, s in mine),
        "no_reads": sum(s["no_reads"] for _, s in mine),
        "clubs": [clubs[code] for code in sorted(clubs, key=club_order)],
        "plans_graded": len(graded),
        "plans_above_baseline": sum(graded),
    }


def build_data(sessions: list[Session], cfg: Config, player: str = "") -> dict:
    out_sessions, shots = [], []
    for idx, sess in enumerate(sessions):
        plan = plan_for(sess, sessions, cfg)
        refs = strike_references(sess, sessions, cfg.mishit_smash_ratio, cfg.min_shots)
        out_sessions.append({
            "id": sess.id, "label": sess.label, "player": sess.player, "start": sess.start.isoformat(),
            "shots": len(sess.shots), "counted": len(sess.counted), "no_reads": len(sess.no_reads),
            "clubs": {code: _club_block(group, refs.get(code)) for code, group in by_club(sess.shots).items()},
            "focus": pick_focus(sess, cfg, sessions),
            "grades": [{**g, "above_baseline": above_baseline(g)} for g in grade_plan(plan, sess)] if plan else None,
            "bag": [{k: _round(v) if isinstance(v, float) else v for k, v in row.items()}
                    for row in bag(sess, sessions, refs)],
        })
        for s in sess.shots:
            shots.append([idx, s["club_code"], int(is_counted(s)), shape_label(s), strike_class(s, refs),
                          s["ts"].strftime("%H:%M:%S"), *(_round(s.get(k)) for k in SHOT_COLUMNS)])
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "player": player,
        "min_shots": cfg.min_shots,
        "min_spread_n": MIN_SPREAD_N,
        "shot_columns": ["session", "club", "use", "shape", "strike", "time", *SHOT_COLUMNS],
        "labels": {**{k: {"label": f.label, "unit": f.unit} for k, f in FIELDS_BY_KEY.items()},
                   "mishit_pct": {"label": "Mishit rate", "unit": "%"}},
        "mishit_ratio": cfg.mishit_smash_ratio,
        "trend_metrics": [list(t) for t in TREND_METRICS],
        "windows": {"face_to_path": list(cfg.face_to_path_window), "face_angle": list(cfg.face_window),
                    "launch_direction": list(cfg.face_window)},
        "sessions": out_sessions,
        "overview": overview(out_sessions),
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
