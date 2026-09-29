from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .clubs import club_code
from .fields import NUMERIC_FIELDS
from .tps_csv import parse_tps_csv

_NUMERIC = [f.key for f in NUMERIC_FIELDS]
_TEXT = ["player", "club", "club_code", "ball", "spin_rate_type", "tags", "condition"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    archived_path TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    shots_in_file INTEGER NOT NULL,
    shots_added INTEGER NOT NULL,
    source_units TEXT NOT NULL,
    unmapped TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY,
    shot_key TEXT NOT NULL UNIQUE,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    ts TEXT NOT NULL,
    use_in_stat INTEGER NOT NULL,
    {", ".join(f"{c} TEXT" for c in _TEXT)},
    {", ".join(f"{c} REAL" for c in _NUMERIC)},
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS shots_ts ON shots(ts);
"""


@dataclass
class IngestResult:
    path: Path
    sha256: str
    already_imported: bool
    import_id: int | None = None
    shots_in_file: int = 0
    shots_added: int = 0
    archived_path: Path | None = None
    unmapped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_units: dict[str, str] = field(default_factory=dict)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def shot_key(shot: dict) -> str:
    return f"{shot.get('player', '')}|{shot['ts'].isoformat()}|{shot['club']}"


def ingest_file(conn: sqlite3.Connection, path: Path, archive_dir: Path) -> IngestResult:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if conn.execute("SELECT 1 FROM imports WHERE sha256 = ?", (sha,)).fetchone():
        return IngestResult(path, sha, already_imported=True)

    parsed = parse_tps_csv(data.decode("utf-8-sig"))
    first = min(s["ts"] for s in parsed.shots)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{first:%Y%m%d-%H%M}-{sha[:8]}.csv"
    shutil.copyfile(path, archived)

    cols = ["shot_key", "import_id", "ts", "use_in_stat", *_TEXT, *_NUMERIC, "raw"]
    sql = f"INSERT OR IGNORE INTO shots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    try:
        import_id, added = _insert(conn, sql, parsed, sha, path, archived)
    except BaseException:
        archived.unlink(missing_ok=True)
        raise
    return IngestResult(path, sha, already_imported=False, import_id=import_id, shots_in_file=len(parsed.shots),
                        shots_added=added, archived_path=archived, unmapped=parsed.unmapped,
                        warnings=parsed.warnings, source_units=parsed.source_units)


def _insert(conn: sqlite3.Connection, sql: str, parsed, sha: str, path: Path, archived: Path) -> tuple[int, int]:
    with conn:
        cur = conn.execute(
            "INSERT INTO imports (sha256, filename, archived_path, imported_at, shots_in_file, shots_added,"
            " source_units, unmapped) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (sha, path.name, str(archived), datetime.now().isoformat(timespec="seconds"), len(parsed.shots),
             json.dumps(parsed.source_units), json.dumps(parsed.unmapped)),
        )
        import_id = int(cur.lastrowid or 0)
        added = 0
        for s in parsed.shots:
            row = [shot_key(s), import_id, s["ts"].isoformat(), int(s["use_in_stat"])]
            row += [club_code(s["club"]) if c == "club_code" else s.get(c, "") for c in _TEXT]
            row += [s.get(c) for c in _NUMERIC]
            row.append(json.dumps(s["raw"]))
            added += conn.execute(sql, row).rowcount
        conn.execute("UPDATE imports SET shots_added = ? WHERE id = ?", (added, import_id))
    return import_id, added


def load_shots(conn: sqlite3.Connection) -> list[dict]:
    shots = []
    for r in conn.execute("SELECT * FROM shots ORDER BY player, ts"):
        s = {k: r[k] for k in r.keys() if k != "raw"}
        s["ts"] = datetime.fromisoformat(r["ts"])
        s["use_in_stat"] = bool(r["use_in_stat"])
        shots.append(s)
    return shots
