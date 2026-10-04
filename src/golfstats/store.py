from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .clubs import club_code
from .fields import NUMERIC_FIELDS
from .tps_csv import ParsedExport, parse_tps_csv

_NUMERIC = [f.key for f in NUMERIC_FIELDS]
_TEXT = ["player", "club", "club_code", "ball", "spin_rate_type", "tags", "condition"]
_COMPARED = ["use_in_stat", "ball", "spin_rate_type", "tags", "condition", *_NUMERIC]
_SHOT_COLS = ["ts", "use_in_stat", *_TEXT, *_NUMERIC, "raw"]
_COLS = ["upload_id", "shot_key", *_SHOT_COLS]
UPLOAD_ORDER = "u.site_id IS NULL, u.site_id, u.id"

_TABLES = [
    """CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    raw BLOB NOT NULL,
    replace_stored INTEGER NOT NULL DEFAULT 0,
    reverted_at TEXT,
    site_id INTEGER UNIQUE,
    error TEXT,
    shots_in_file INTEGER NOT NULL DEFAULT 0,
    source_units TEXT NOT NULL DEFAULT '{}',
    unmapped TEXT NOT NULL DEFAULT '[]',
    warnings TEXT NOT NULL DEFAULT '[]'
)""",
    "CREATE INDEX IF NOT EXISTS uploads_sha ON uploads(sha256)",
    f"""CREATE TABLE IF NOT EXISTS upload_shots (
    upload_id INTEGER NOT NULL REFERENCES uploads(id),
    shot_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    use_in_stat INTEGER NOT NULL,
    {", ".join(f"{c} TEXT" for c in _TEXT)},
    {", ".join(f"{c} REAL" for c in _NUMERIC)},
    raw TEXT NOT NULL,
    PRIMARY KEY (upload_id, shot_key)
)""",
    "CREATE INDEX IF NOT EXISTS upload_shots_key ON upload_shots(shot_key)",
]

_RANK_KEYS = ["(u.site_id IS NULL)", "COALESCE(u.site_id, 0)", "u.id"]
_RANK = ", ".join(f"CASE WHEN u.replace_stored = 1 THEN -{k} ELSE {k} END" for k in _RANK_KEYS)
WINNERS = f"""
SELECT s.rowid AS id, s.shot_key, s.upload_id, {", ".join(f"s.{c}" for c in _SHOT_COLS)} FROM upload_shots s
WHERE s.upload_id = (
    SELECT us.upload_id FROM upload_shots us JOIN uploads u ON u.id = us.upload_id
    WHERE us.shot_key = s.shot_key AND u.reverted_at IS NULL
    ORDER BY u.replace_stored DESC, {_RANK} LIMIT 1)
"""
_VIEW = f"CREATE VIEW shots AS {WINNERS}"


class MigrationError(RuntimeError):
    pass


@dataclass
class UploadResult:
    upload_id: int
    sha256: str
    duplicate: bool
    shots_in_file: int = 0
    error: str | None = None
    unmapped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_units: dict[str, str] = field(default_factory=dict)


@dataclass
class IngestResult:
    path: Path
    sha256: str
    already_imported: bool
    upload_id: int | None = None
    shots_in_file: int = 0
    shots_added: int = 0
    unmapped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_units: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    replaced: int = 0
    error: str | None = None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        kinds = {r["name"]: r["type"] for r in conn.execute("SELECT name, type FROM sqlite_master")}
        if kinds.get("imports") == "table" and kinds.get("shots") == "table":
            migrate(conn, db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        with transaction(conn):
            for stmt in [*_TABLES, "DROP VIEW IF EXISTS shots", _VIEW]:
                conn.execute(stmt)
    except BaseException:
        conn.close()
        raise
    return conn


def shot_key(shot: dict) -> str:
    return f"{shot.get('player', '')}|{shot['ts'].isoformat()}|{shot['club']}"


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    if a is None or b is None:
        return a is b
    return a == b


def _row(s: dict, upload_id: int) -> list:
    row = [upload_id, shot_key(s), s["ts"].isoformat(), int(s["use_in_stat"])]
    row += [club_code(s["club"]) if c == "club_code" else s.get(c, "") for c in _TEXT]
    row += [s.get(c) for c in _NUMERIC]
    row.append(json.dumps(s["raw"]))
    return row


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse(data: bytes) -> ParsedExport:
    return parse_tps_csv(data.decode("utf-8-sig"))


def _store_parse(conn: sqlite3.Connection, upload_id: int, data: bytes, error: str | None = None) -> None:
    parsed = None
    if error is None:
        try:
            parsed = _parse(data)
        except ValueError as exc:
            error = str(exc) or type(exc).__name__
    conn.execute("DELETE FROM upload_shots WHERE upload_id = ?", (upload_id,))
    if parsed is None:
        conn.execute("UPDATE uploads SET error = ?, shots_in_file = 0, source_units = '{}', unmapped = '[]',"
                     " warnings = '[]' WHERE id = ?", (error, upload_id))
        return
    insert = f"INSERT OR IGNORE INTO upload_shots ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})"
    conn.executemany(insert, [_row(s, upload_id) for s in parsed.shots])
    conn.execute("UPDATE uploads SET error = NULL, shots_in_file = ?, source_units = ?, unmapped = ?, warnings = ?"
                 " WHERE id = ?", (len(parsed.shots), json.dumps(parsed.source_units), json.dumps(parsed.unmapped),
                                   json.dumps(parsed.warnings), upload_id))


def _upload_row(conn: sqlite3.Connection, upload_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if row is None:
        raise KeyError(f"no upload {upload_id}")
    return row


def _result(row: sqlite3.Row, duplicate: bool) -> UploadResult:
    return UploadResult(row["id"], row["sha256"], duplicate, row["shots_in_file"], row["error"],
                        json.loads(row["unmapped"]), json.loads(row["warnings"]), json.loads(row["source_units"]))


def _active_by_sha(conn: sqlite3.Connection, sha: str) -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM uploads u WHERE sha256 = ? AND reverted_at IS NULL ORDER BY {UPLOAD_ORDER}"
                        " LIMIT 1", (sha,)).fetchone()


def add_upload(conn: sqlite3.Connection, data: bytes, filename: str, *, uploaded_by: str = "cli",
               uploaded_at: str | None = None, replace: bool = False) -> UploadResult:
    sha = hashlib.sha256(data).hexdigest()
    with transaction(conn):
        known = _active_by_sha(conn, sha)
        if known is not None:
            if known["error"] is not None:
                _store_parse(conn, known["id"], bytes(known["raw"]))
            return _result(_upload_row(conn, known["id"]), duplicate=True)
        cur = conn.execute("INSERT INTO uploads (sha256, filename, uploaded_at, uploaded_by, raw, replace_stored)"
                           " VALUES (?, ?, ?, ?, ?, ?)",
                           (sha, filename, uploaded_at or _now(), uploaded_by, data, int(replace)))
        upload_id = int(cur.lastrowid or 0)
        _store_parse(conn, upload_id, data)
        return _result(_upload_row(conn, upload_id), duplicate=False)


def mirror_upload(conn: sqlite3.Connection, data: bytes, *, site_id: int, filename: str, uploaded_by: str,
                  uploaded_at: str, replace: bool, reverted_at: str | None, sha256: str) -> int:
    actual = hashlib.sha256(data).hexdigest()
    error = None if actual == sha256 else f"SHA-256 mismatch: the site says {sha256}, the bytes hash to {actual}"
    values = (sha256, filename, uploaded_at, uploaded_by, data, int(replace), reverted_at)
    with transaction(conn):
        known = conn.execute("SELECT id FROM uploads WHERE site_id = ?", (site_id,)).fetchone()
        if known is None:
            cur = conn.execute("INSERT INTO uploads (sha256, filename, uploaded_at, uploaded_by, raw, replace_stored,"
                               " reverted_at, site_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*values, site_id))
            upload_id = int(cur.lastrowid or 0)
        else:
            upload_id = known["id"]
            conn.execute("UPDATE uploads SET sha256 = ?, filename = ?, uploaded_at = ?, uploaded_by = ?, raw = ?,"
                         " replace_stored = ?, reverted_at = ? WHERE id = ?", (*values, upload_id))
        _store_parse(conn, upload_id, data, error)
    return upload_id


def set_reverted(conn: sqlite3.Connection, upload_id: int, reverted_at: str | None) -> None:
    with transaction(conn):
        conn.execute("UPDATE uploads SET reverted_at = ? WHERE id = ?", (reverted_at, upload_id))


def set_replace(conn: sqlite3.Connection, upload_id: int, on: bool) -> None:
    with transaction(conn):
        conn.execute("UPDATE uploads SET replace_stored = ? WHERE id = ?", (int(on), upload_id))


def link_site_id(conn: sqlite3.Connection, upload_id: int, site_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE uploads SET site_id = ? WHERE id = ?", (site_id, upload_id))


def delete_upload(conn: sqlite3.Connection, upload_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM upload_shots WHERE upload_id = ?", (upload_id,))
        conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))


def upload_raw(conn: sqlite3.Connection, upload_id: int) -> bytes:
    return bytes(_upload_row(conn, upload_id)["raw"])


def shot_span(conn: sqlite3.Connection, upload_id: int) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM upload_shots WHERE upload_id = ?", (upload_id,)).fetchone()
    return row[0], row[1]


def conflicts_of(conn: sqlite3.Connection, upload_id: int) -> list[str]:
    cols = ", ".join(f"s.{c} AS s_{c}, w.{c} AS w_{c}" for c in _COMPARED)
    rows = conn.execute(f"SELECT s.shot_key, {cols} FROM upload_shots s JOIN shots w ON w.shot_key = s.shot_key"
                        " WHERE s.upload_id = ? AND w.upload_id != ? ORDER BY s.rowid", (upload_id, upload_id))
    return [r["shot_key"] for r in rows if not all(_same(r[f"s_{c}"], r[f"w_{c}"]) for c in _COMPARED)]


def _won(conn: sqlite3.Connection, upload_id: int, keys: list[str] | None = None) -> int:
    if keys is None:
        return conn.execute("SELECT COUNT(*) FROM shots WHERE upload_id = ?", (upload_id,)).fetchone()[0]
    won = {r[0] for r in conn.execute("SELECT shot_key FROM shots WHERE upload_id = ?", (upload_id,))}
    return sum(1 for k in keys if k in won)


def _sole(conn: sqlite3.Connection, upload_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM upload_shots s WHERE s.upload_id = ? AND NOT EXISTS (SELECT 1 FROM upload_shots o"
        " JOIN uploads u ON u.id = o.upload_id WHERE o.shot_key = s.shot_key AND o.upload_id != s.upload_id"
        " AND u.reverted_at IS NULL)", (upload_id,)).fetchone()[0]


def upload_result(conn: sqlite3.Connection, upload_id: int) -> dict:
    row = _upload_row(conn, upload_id)
    players = sorted({r[0] for r in conn.execute("SELECT DISTINCT player FROM upload_shots WHERE upload_id = ?",
                                                 (upload_id,)) if r[0]})
    return {
        "ok": row["error"] is None,
        "error": row["error"],
        "shots_in_file": row["shots_in_file"],
        "shots_used": _won(conn, upload_id),
        "conflicts": len(conflicts_of(conn, upload_id)),
        "players": players,
        "warnings": json.loads(row["warnings"])[:10],
    }


def list_uploads(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"SELECT u.*, (SELECT COUNT(*) FROM shots w WHERE w.upload_id = u.id) AS shots_used"
                        f" FROM uploads u ORDER BY {UPLOAD_ORDER}")
    out = []
    for r in rows:
        u = {k: r[k] for k in r.keys() if k != "raw"}
        u["replace_stored"] = bool(u["replace_stored"])
        for k in ("source_units", "unmapped", "warnings"):
            u[k] = json.loads(u[k])
        out.append(u)
    return out


def ingest_file(conn: sqlite3.Connection, path: Path, replace: bool = False) -> IngestResult:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    with transaction(conn):
        known = _active_by_sha(conn, sha)
        if known is not None and known["error"] is None:
            conflicts = conflicts_of(conn, known["id"])
            res = IngestResult(path, sha, already_imported=True, upload_id=known["id"],
                               shots_in_file=known["shots_in_file"], conflicts=conflicts)
            if replace and conflicts and not known["replace_stored"]:
                set_replace(conn, known["id"], True)
                res.already_imported, res.replaced = False, _won(conn, known["id"], conflicts)
            return res
        up = add_upload(conn, data, path.name)
        res = IngestResult(path, sha, already_imported=False, upload_id=up.upload_id, shots_in_file=up.shots_in_file,
                           unmapped=up.unmapped, warnings=up.warnings, source_units=up.source_units, error=up.error)
        if up.error is not None:
            return res
        res.shots_added = _sole(conn, up.upload_id)
        res.conflicts = conflicts_of(conn, up.upload_id)
        if replace and res.conflicts:
            set_replace(conn, up.upload_id, True)
            res.replaced = _won(conn, up.upload_id, res.conflicts)
        return res


def load_shots(conn: sqlite3.Connection) -> list[dict]:
    shots = []
    for r in conn.execute("SELECT * FROM shots ORDER BY player, ts"):
        s = {k: r[k] for k in r.keys() if k != "raw"}
        s["ts"] = datetime.fromisoformat(r["ts"])
        s["use_in_stat"] = bool(r["use_in_stat"])
        shots.append(s)
    return shots


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    base = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    target, n = base, 2
    while target.exists():
        target, n = base.with_name(f"{base.name}-{n}"), n + 1
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(conn: sqlite3.Connection, db_path: Path) -> Path:
    backup = _backup(conn, db_path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT type FROM sqlite_master WHERE name = 'imports'").fetchone() is None:
            conn.execute("ROLLBACK")
            return backup
        for stmt in _TABLES:
            conn.execute(stmt)
        imports = conn.execute("SELECT * FROM imports ORDER BY id").fetchall()
        for imp in imports:
            archived = Path(imp["archived_path"])
            try:
                data = archived.read_bytes()
            except OSError as exc:
                raise MigrationError(f"import {imp['id']} ({imp['filename']}): archived file {archived} "
                                     f"cannot be read: {exc}") from exc
            sha = hashlib.sha256(data).hexdigest()
            if sha != imp["sha256"]:
                raise MigrationError(f"import {imp['id']} ({imp['filename']}): archived file {archived} "
                                     "does not match its SHA-256")
            conn.execute("INSERT INTO uploads (id, sha256, filename, uploaded_at, uploaded_by, raw)"
                         " VALUES (?, ?, ?, ?, 'cli', ?)", (imp["id"], sha, imp["filename"], imp["imported_at"], data))
            _store_parse(conn, imp["id"], data)
            err = conn.execute("SELECT error FROM uploads WHERE id = ?", (imp["id"],)).fetchone()[0]
            if err is not None:
                raise MigrationError(f"import {imp['id']} ({imp['filename']}): archived file {archived} "
                                     f"no longer parses: {err}")
        conn.execute("UPDATE uploads SET replace_stored = 1 WHERE id IN (SELECT DISTINCT o.import_id FROM shots o"
                     " JOIN upload_shots e ON e.shot_key = o.shot_key AND e.upload_id < o.import_id)")
        _verify(conn)
        conn.execute("DROP INDEX IF EXISTS shots_ts")
        conn.execute("DROP TABLE shots")
        conn.execute("DROP TABLE imports")
        conn.execute(_VIEW)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return backup


def _verify(conn: sqlite3.Connection) -> None:
    cols = ", ".join(["shot_key", *_COMPARED])
    old = {r["shot_key"]: r for r in conn.execute(f"SELECT {cols} FROM shots")}
    new = {r["shot_key"]: r for r in conn.execute(f"SELECT {cols} FROM ({WINNERS})")}
    if old.keys() != new.keys():
        missing, extra = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
        raise MigrationError(f"migration check failed: {len(missing)} stored shot(s) missing, {len(extra)} extra, "
                             f"first {(missing or extra)[0]}")
    for key, row in old.items():
        bad = [c for c in _COMPARED if not _same(row[c], new[key][c])]
        if bad:
            raise MigrationError(f"migration check failed: shot {key} differs in {', '.join(bad)}")
