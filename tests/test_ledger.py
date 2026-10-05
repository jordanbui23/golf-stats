import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from golfstats import store
from golfstats.clubs import club_code
from golfstats.fields import NUMERIC_FIELDS
from golfstats.store import (
    MigrationError, add_upload, connect, ingest_file, list_uploads, load_shots, mirror_upload, set_replace,
    set_reverted, upload_result,
)
from golfstats.tps_csv import parse_tps_csv
from test_store_cli import write_conflicting, write_export

_NUMERIC = [f.key for f in NUMERIC_FIELDS]
_TEXT = ["player", "club", "club_code", "ball", "spin_rate_type", "tags", "condition"]
OLD_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    archived_path TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    shots_in_file INTEGER NOT NULL,
    shots_added INTEGER NOT NULL,
    source_units TEXT NOT NULL,
    unmapped TEXT NOT NULL,
    pending_conflicts TEXT NOT NULL DEFAULT '[]'
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
_OLD_COLS = ["shot_key", "import_id", "ts", "use_in_stat", *_TEXT, *_NUMERIC, "raw"]


def old_ingest(conn: sqlite3.Connection, path: Path, archive: Path, replace: bool = False) -> Path:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    parsed = parse_tps_csv(data.decode("utf-8-sig"))
    archive.mkdir(parents=True, exist_ok=True)
    archived = archive / f"{sha[:8]}.csv"
    shutil.copyfile(path, archived)
    with conn:
        cur = conn.execute("INSERT INTO imports (sha256, filename, archived_path, imported_at, shots_in_file,"
                           " shots_added, source_units, unmapped) VALUES (?, ?, ?, '2026-10-01T20:00:00', ?, 0,"
                           " '{}', '[]')", (sha, path.name, str(archived), len(parsed.shots)))
        import_id = cur.lastrowid
        for s in parsed.shots:
            row = [store.shot_key(s), import_id, s["ts"].isoformat(), int(s["use_in_stat"])]
            row += [club_code(s["club"]) if c == "club_code" else s.get(c, "") for c in _TEXT]
            row += [s.get(c) for c in _NUMERIC] + [json.dumps(s["raw"])]
            if conn.execute(f"INSERT OR IGNORE INTO shots ({', '.join(_OLD_COLS)}) VALUES"
                            f" ({', '.join('?' * len(_OLD_COLS))})", row).rowcount or not replace:
                continue
            conn.execute(f"UPDATE shots SET {', '.join(f'{c} = ?' for c in _OLD_COLS[1:])} WHERE shot_key = ?",
                         [*row[1:], row[0]])
    return archived


def old_db(tmp_path: Path) -> tuple[Path, list[Path]]:
    db = tmp_path / "data" / "golf.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    a = write_export(tmp_path / "in", "a.csv", plan=[("7 Iron", 20)])
    b = write_conflicting(tmp_path / "in", "b.csv", a)
    c = write_export(tmp_path / "in", "c.csv", plan=[("7 Iron", 20), ("Driver", 6)])
    archive = tmp_path / "data" / "raw"
    archived = [old_ingest(conn, a, archive), old_ingest(conn, b, archive, replace=True), old_ingest(conn, c, archive)]
    conn.close()
    return db, archived


def old_shots(db: Path) -> dict[str, dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["shot_key"]: dict(r) for r in conn.execute("SELECT * FROM shots")}
    conn.close()
    return rows


def tables(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    kinds = {name: kind for name, kind in conn.execute("SELECT name, type FROM sqlite_master")}
    conn.close()
    return kinds


def speeds(conn) -> dict[str, float]:
    return {s["shot_key"]: s["club_speed"] for s in load_shots(conn)}


def test_the_same_bytes_twice_are_one_upload(tmp_path):
    conn = connect(tmp_path / "golf.db")
    data = write_export(tmp_path / "in", "a.csv").read_bytes()
    first, again = add_upload(conn, data, "a.csv"), add_upload(conn, data, "copy.csv")
    assert (first.duplicate, again.duplicate, again.upload_id) == (False, True, first.upload_id)
    assert len(list_uploads(conn)) == 1 and len(load_shots(conn)) == 20


def test_overlapping_exports_share_shots_and_the_earlier_upload_keeps_them(tmp_path):
    conn = connect(tmp_path / "golf.db")
    a = ingest_file(conn, write_export(tmp_path / "in", "a.csv", plan=[("7 Iron", 20)]))
    b = ingest_file(conn, write_export(tmp_path / "in", "b.csv", plan=[("7 Iron", 20), ("Driver", 6)]))
    assert (b.shots_in_file, b.shots_added, b.conflicts) == (26, 6, [])
    assert (upload_result(conn, a.upload_id)["shots_used"], upload_result(conn, b.upload_id)["shots_used"]) == (20, 6)


def test_a_conflict_keeps_the_stored_values_until_replace(tmp_path):
    conn = connect(tmp_path / "golf.db")
    a = write_export(tmp_path / "in", "a.csv")
    ingest_file(conn, a)
    before = speeds(conn)
    b = ingest_file(conn, write_conflicting(tmp_path / "in", "b.csv", a))
    assert len(b.conflicts) == 20 and speeds(conn) == before
    again = ingest_file(conn, tmp_path / "in" / "b.csv", replace=True)
    assert again.replaced == 20 and all(speeds(conn)[k] == pytest.approx(v + 1.5) for k, v in before.items())
    assert [u["replace_stored"] for u in list_uploads(conn)] == [False, True]


def test_revert_drops_only_what_that_upload_alone_held_and_restore_brings_it_back(tmp_path):
    conn = connect(tmp_path / "golf.db")
    a = ingest_file(conn, write_export(tmp_path / "in", "a.csv", plan=[("7 Iron", 20)]))
    plain = speeds(conn)
    both = write_export(tmp_path / "in", "b.csv", plan=[("7 Iron", 20), ("Driver", 6)])
    b = ingest_file(conn, both)
    c = ingest_file(conn, write_conflicting(tmp_path / "in", "c.csv", both), replace=True)
    assert c.replaced == 26 and {s["upload_id"] for s in load_shots(conn)} == {c.upload_id}

    set_reverted(conn, c.upload_id, "2026-10-02T10:00:00.000Z")
    shots = load_shots(conn)
    assert len(shots) == 26 and {s["upload_id"] for s in shots} == {a.upload_id, b.upload_id}
    assert all(speeds(conn)[k] == v for k, v in plain.items())

    set_reverted(conn, b.upload_id, "2026-10-02T10:01:00.000Z")
    shots = load_shots(conn)
    assert len(shots) == 20 and {s["club_code"] for s in shots} == {"7i"} and speeds(conn) == plain

    set_reverted(conn, c.upload_id, None)
    assert len(load_shots(conn)) == 26 and {s["upload_id"] for s in load_shots(conn)} == {c.upload_id}


def test_the_latest_replace_wins_over_an_earlier_replace(tmp_path):
    conn = connect(tmp_path / "golf.db")
    a = write_export(tmp_path / "in", "a.csv")
    ingest_file(conn, a)
    b = ingest_file(conn, write_conflicting(tmp_path / "in", "b.csv", a), replace=True)
    c = ingest_file(conn, write_conflicting(tmp_path / "in", "c.csv", a, "Spin Rate Type"), replace=True)
    shots = load_shots(conn)
    assert {s["upload_id"] for s in shots} == {c.upload_id} and {s["spin_rate_type"] for s in shots} == {"Estimated"}
    set_replace(conn, c.upload_id, False)
    assert {s["upload_id"] for s in load_shots(conn)} == {b.upload_id}


def test_a_file_that_fails_to_parse_is_stored_with_its_error_and_no_shots(tmp_path):
    conn = connect(tmp_path / "golf.db")
    up = add_upload(conn, b"not,a,trackman,file\n1,2,3,4\n", "bad.csv")
    assert up.error and not up.duplicate and up.shots_in_file == 0
    result = upload_result(conn, up.upload_id)
    assert result == {"ok": False, "error": up.error, "shots_in_file": 0, "shots_used": 0, "conflicts": 0,
                      "players": [], "warnings": []}
    assert load_shots(conn) == []


def test_a_known_errored_upload_is_parsed_again_when_the_file_returns(tmp_path, monkeypatch):
    conn = connect(tmp_path / "golf.db")
    path = write_export(tmp_path / "in", "a.csv")
    real = store._parse

    def broken(_data):
        raise ValueError("parser bug")
    monkeypatch.setattr(store, "_parse", broken)
    first = ingest_file(conn, path)
    assert first.error == "parser bug" and load_shots(conn) == []
    monkeypatch.setattr(store, "_parse", real)
    again = ingest_file(conn, path)
    assert (again.error, again.upload_id, again.shots_added) == (None, first.upload_id, 20)
    assert len(list_uploads(conn)) == 1 and len(load_shots(conn)) == 20


def mirror(conn, data: bytes, site_id: int, **kw) -> int:
    args = dict(filename=f"site-{site_id}.csv", uploaded_by="jordan", uploaded_at="2026-10-02T01:00:00.000Z",
                replace=False, reverted_at=None, sha256=hashlib.sha256(data).hexdigest())
    return mirror_upload(conn, data, site_id=site_id, **{**args, **kw})


def test_mirror_upload_stores_duplicates_and_orders_site_uploads_first(tmp_path):
    conn = connect(tmp_path / "golf.db")
    data = write_export(tmp_path / "in", "a.csv").read_bytes()
    local = add_upload(conn, data, "a.csv").upload_id
    late, early = mirror(conn, data, site_id=9), mirror(conn, data, site_id=4)
    assert len(list_uploads(conn)) == 3
    assert [u["site_id"] for u in list_uploads(conn)] == [4, 9, None]
    assert {s["upload_id"] for s in load_shots(conn)} == {early}
    assert upload_result(conn, late)["shots_used"] == 0 and upload_result(conn, local)["conflicts"] == 0
    assert mirror(conn, data, site_id=4, reverted_at="2026-10-03T00:00:00.000Z") == early
    assert {s["upload_id"] for s in load_shots(conn)} == {late}


def test_mirror_upload_with_the_wrong_sha_is_stored_as_an_error(tmp_path):
    conn = connect(tmp_path / "golf.db")
    data = write_export(tmp_path / "in", "a.csv").read_bytes()
    up = mirror(conn, data, site_id=1, sha256="0" * 64)
    result = upload_result(conn, up)
    assert not result["ok"] and "SHA-256" in result["error"] and load_shots(conn) == []


def test_upload_result_counts_used_shots_conflicts_and_players(tmp_path):
    conn = connect(tmp_path / "golf.db")
    a = write_export(tmp_path / "in", "a.csv", player="Jordan", plan=[("7 Iron", 20), ("Driver", 4)])
    a_id = ingest_file(conn, a).upload_id
    b_id = ingest_file(conn, write_conflicting(tmp_path / "in", "b.csv", a)).upload_id
    c_id = ingest_file(conn, write_export(tmp_path / "in", "c.csv", player="Jordan",
                                          plan=[("7 Iron", 20), ("Driver", 4), ("Pitching Wedge", 5)])).upload_id
    assert upload_result(conn, a_id) == {"ok": True, "error": None, "shots_in_file": 24, "shots_used": 24,
                                         "conflicts": 0, "players": ["Jordan"], "warnings": []}
    assert upload_result(conn, b_id)["shots_used"] == 0 and upload_result(conn, b_id)["conflicts"] == 24
    assert (upload_result(conn, c_id)["shots_used"], upload_result(conn, c_id)["conflicts"]) == (5, 0)


def test_an_old_database_migrates_to_the_ledger(tmp_path):
    db, archived = old_db(tmp_path)
    before = old_shots(db)
    conn = connect(db)
    backups = list(db.parent.glob("golf.db.bak-*"))
    assert len(backups) == 1 and old_shots(backups[0]) == before
    kinds = tables(db)
    assert "imports" not in kinds and kinds["shots"] == "view"
    after = {s["shot_key"]: s for s in load_shots(conn)}
    assert after.keys() == before.keys() and len(after) == 26
    for key, old in before.items():
        assert all(store._same(old[c], int(after[key][c]) if c == "use_in_stat" else after[key][c])
                   for c in store._COMPARED), key
    uploads = list_uploads(conn)
    assert [u["replace_stored"] for u in uploads] == [False, True, False]
    assert {after[k]["upload_id"] for k in after if k.endswith("|7 Iron")} == {2}
    assert [store.upload_raw(conn, u["id"]) for u in uploads] == [p.read_bytes() for p in archived]
    conn.close()
    assert connect(db) and len(list(db.parent.glob("golf.db.bak-*"))) == 1


def test_a_corrupted_archive_stops_the_migration_with_the_old_tables_intact(tmp_path):
    db, archived = old_db(tmp_path)
    before = old_shots(db)
    archived[0].write_bytes(archived[0].read_bytes() + b"x")
    with pytest.raises(MigrationError, match=str(archived[0])):
        connect(db)
    kinds = tables(db)
    assert kinds["imports"] == "table" and kinds["shots"] == "table" and "uploads" not in kinds
    assert old_shots(db) == before


def test_a_missing_archive_is_named(tmp_path):
    db, archived = old_db(tmp_path)
    archived[1].unlink()
    with pytest.raises(MigrationError, match=str(archived[1])):
        connect(db)
    assert tables(db)["shots"] == "table"


@pytest.mark.parametrize("column, change", [
    ("carry", "carry + 10"), ("raw", "raw || ' '"), ("club_code", "'XX'"), ("ts", "ts || '.5'"),
    ("player", "player || 'x'"),
])
def test_a_view_that_would_differ_from_the_old_table_rolls_back(tmp_path, column, change):
    db, _ = old_db(tmp_path)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(f"UPDATE shots SET {column} = {change} WHERE rowid = (SELECT MIN(rowid) FROM shots)")
    conn.close()
    before = old_shots(db)
    with pytest.raises(MigrationError, match=f"differs in {column}"):
        connect(db)
    assert old_shots(db) == before and "uploads" not in tables(db)
