import csv
import fcntl
import io
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from golfstats.__main__ import main
from golfstats.config import load_config
from golfstats.store import connect, ingest_file, list_uploads, load_shots, upload_raw
from golfstats.synth import synth_session_csv

START = datetime(2026, 10, 1, 18, 0, 0)


def write_export(folder: Path, name: str, start: datetime = START, seed: int = 1, **kw) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(synth_session_csv(start, kw.pop("plan", [("7 Iron", 20)]), 0.5, -3.0, seed=seed, **kw))
    return path


def write_conflicting(folder: Path, name: str, source: Path, column: str = "Club Speed") -> Path:
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    rows = list(csv.reader(lines[1:]))
    col = rows[0].index(column)
    for r in rows[2:]:
        r[col] = repr(float(r[col]) + 1.5) if column == "Club Speed" else "Estimated"
    buf = io.StringIO()
    buf.write(lines[0] + "\r\n")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    path = folder / name
    path.write_text(buf.getvalue())
    return path


def test_ingest_stores_a_byte_identical_copy_and_counts_shots(tmp_path):
    src = write_export(tmp_path / "in", "a.csv")
    conn = connect(tmp_path / "golf.db")
    res = ingest_file(conn, src)
    assert (res.shots_in_file, res.shots_added) == (20, 20)
    assert res.upload_id is not None and upload_raw(conn, res.upload_id) == src.read_bytes()
    assert len(load_shots(conn)) == 20
    assert not (tmp_path / "raw").exists()


def test_same_file_twice_is_skipped(tmp_path):
    src = write_export(tmp_path / "in", "a.csv")
    conn = connect(tmp_path / "golf.db")
    ingest_file(conn, src)
    again = ingest_file(conn, src)
    assert again.already_imported
    assert len(load_shots(conn)) == 20


def test_overlapping_exports_do_not_duplicate_shots(tmp_path):
    conn = connect(tmp_path / "golf.db")
    first = write_export(tmp_path / "in", "a.csv", plan=[("7 Iron", 20)])
    ingest_file(conn, first)
    both = write_export(tmp_path / "in", "b.csv", plan=[("7 Iron", 20), ("Driver", 6)])
    res = ingest_file(conn, both)
    assert (res.shots_in_file, res.shots_added) == (26, 6)
    assert len(load_shots(conn)) == 26


def test_cli_ingest_moves_good_files_and_keeps_bad_ones(cfg_file, capsys):
    cfg = load_config(cfg_file)
    good = write_export(cfg.inbox, "good.csv")
    original = good.read_bytes()
    bad = cfg.inbox / "bad.csv"
    bad.write_text("not,a,trackman,file\n1,2,3,4\n")
    assert main(["--config", str(cfg_file), "ingest"]) == 1
    out = capsys.readouterr().out
    assert "bad.csv: not imported" in out
    assert bad.exists() and not good.exists()
    conn = connect(cfg.db_path)
    stored = {u["filename"]: u for u in list_uploads(conn)}
    assert stored["good.csv"]["error"] is None and upload_raw(conn, stored["good.csv"]["id"]) == original
    assert stored["bad.csv"]["error"] and stored["bad.csv"]["shots_in_file"] == 0
    assert not (cfg.data_dir / "raw").exists()


def test_cli_files_outside_inbox_are_never_deleted(cfg_file, tmp_path):
    outside = write_export(tmp_path / "downloads", "session.csv")
    assert main(["--config", str(cfg_file), "ingest", str(outside)]) == 0
    assert outside.exists()


def test_cli_ingest_writes_report_plan_and_dashboard_then_grades_next_session(cfg_file, capsys):
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "s1.csv", plan=[("7 Iron", 24)])
    assert main(["--config", str(cfg_file), "ingest"]) == 0
    assert len(list(cfg.plans.glob("*.json"))) == 1
    write_export(cfg.inbox, "s2.csv", start=START + timedelta(days=7), seed=2, plan=[("7 Iron", 24)])
    assert main(["--config", str(cfg_file), "ingest"]) == 0
    out = capsys.readouterr().out
    assert "Next focus: 7i" in out
    reports = sorted(cfg.reports.glob("*.md"))
    assert [r.stem for r in reports] == ["2026-10-01-1800", "2026-10-08-1800"]
    second = reports[1].read_text()
    assert "## Last plan" in second and "## Compared with the previous 1 session(s)" in second
    html = cfg.dashboard_path("demo").read_text()
    assert "/*__DATA__*/null" not in html and '"sessions":[' in html and "NaN" not in html


def test_player_filter(cfg_file, tmp_path):
    cfg_file.write_text(cfg_file.read_text() + '[player]\nname = "Someone Else"\n')
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "s1.csv")
    main(["--config", str(cfg_file), "ingest"])
    assert main(["--config", str(cfg_file), "report"]) == 1


def test_conflicting_values_are_reported_kept_then_replaced_on_request(tmp_path):
    conn = connect(tmp_path / "golf.db")
    first = write_export(tmp_path / "in", "a.csv", seed=1)
    ingest_file(conn, first)
    before = {s["shot_key"]: s["club_speed"] for s in load_shots(conn)}
    other = write_conflicting(tmp_path / "in", "b.csv", first)
    res = ingest_file(conn, other)
    assert (res.shots_added, len(res.conflicts), res.replaced) == (0, 20, 0)
    assert {s["shot_key"]: s["club_speed"] for s in load_shots(conn)} == before
    again = ingest_file(conn, other, replace=True)
    assert not again.already_imported and again.replaced == 20
    after = {s["shot_key"]: s["club_speed"] for s in load_shots(conn)}
    assert after.keys() == before.keys() and all(after[k] != before[k] for k in after)


def test_cli_keeps_a_conflicting_inbox_file_until_replace(cfg_file, tmp_path, capsys):
    cfg = load_config(cfg_file)
    a = write_export(tmp_path / "keep", "a.csv", seed=1)
    main(["--config", str(cfg_file), "ingest", str(a)])
    b = write_conflicting(cfg.inbox, "b.csv", a)
    main(["--config", str(cfg_file), "ingest"])
    assert b.exists() and "--replace" in capsys.readouterr().out
    main(["--config", str(cfg_file), "ingest"])
    out = capsys.readouterr().out
    assert b.exists() and "already imported" in out and "20 shot(s) are already stored" in out
    assert main(["--config", str(cfg_file), "ingest", "--replace"]) == 0
    assert not b.exists() and "replaced 20" in capsys.readouterr().out


def test_outside_symlink_pointing_into_the_inbox_is_not_deleted(cfg_file, tmp_path):
    cfg = load_config(cfg_file)
    target = write_export(cfg.inbox, "real.csv")
    link = tmp_path / "link.csv"
    link.symlink_to(target)
    assert main(["--config", str(cfg_file), "ingest", str(link)]) == 0
    assert link.is_symlink() and target.exists()


def test_inbox_symlink_to_an_outside_file_removes_only_the_link(cfg_file, tmp_path):
    cfg = load_config(cfg_file)
    outside = write_export(tmp_path / "downloads", "session.csv")
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    (cfg.inbox / "session.csv").symlink_to(outside)
    assert main(["--config", str(cfg_file), "ingest"]) == 0
    assert outside.exists() and not (cfg.inbox / "session.csv").exists()


def test_dashboard_payload_cannot_close_or_comment_out_its_script(tmp_path):
    from golfstats.config import Config
    from golfstats.dashboard import render_dashboard
    from golfstats.stats import split_sessions

    evil = "</script><!--<script>&"
    shots = [{"ts": START + timedelta(minutes=i), "player": evil, "club": "7 Iron", "club_code": "7i",
              "use_in_stat": True, "carry": 150.0} for i in range(3)]
    html = render_dashboard(split_sessions(shots, 90), Config(data_dir=tmp_path))
    script = html.split("const DATA = ", 1)[1].split(";</script>", 1)[0]
    assert "<" not in script and ">" not in script and "&" not in script
    assert html.count("</script>") == 2


def test_a_change_in_spin_rate_type_alone_is_a_conflict(tmp_path):
    conn = connect(tmp_path / "golf.db")
    first = write_export(tmp_path / "in", "a.csv", seed=1)
    ingest_file(conn, first)
    res = ingest_file(conn, write_conflicting(tmp_path / "in", "b.csv", first, "Spin Rate Type"))
    assert len(res.conflicts) > 0 and res.shots_added == 0


def test_replacing_an_earlier_session_refreshes_later_reports(cfg_file, tmp_path, capsys):
    cfg = load_config(cfg_file)
    a = write_export(tmp_path / "keep", "a.csv", plan=[("7 Iron", 24)])
    main(["--config", str(cfg_file), "ingest", str(a)])
    write_export(cfg.inbox, "s2.csv", start=START + timedelta(days=7), seed=2, plan=[("7 Iron", 24)])
    main(["--config", str(cfg_file), "ingest"])
    later = cfg.reports / "2026-10-08-1800.md"
    later.write_text("stale")
    write_conflicting(cfg.inbox, "a2.csv", a)
    capsys.readouterr()
    assert main(["--config", str(cfg_file), "ingest", "--replace"]) == 0
    assert "Refreshed 1 later report(s)" in capsys.readouterr().out
    assert "## Last plan" in later.read_text()


def test_pending_conflicts_are_rechecked_once_another_file_resolves_them(tmp_path):
    conn = connect(tmp_path / "golf.db")
    first = write_export(tmp_path / "in", "a.csv", seed=1)
    ingest_file(conn, first)
    b = write_conflicting(tmp_path / "in", "b.csv", first)
    assert len(ingest_file(conn, b).conflicts) == 20
    c = tmp_path / "in" / "c.csv"
    c.write_bytes(b.read_bytes() + b"\r\n")
    ingest_file(conn, c, replace=True)
    assert ingest_file(conn, b).conflicts == []


def test_each_player_gets_a_dashboard_with_only_their_sessions(cfg_file, capsys):
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "a.csv", player="Jordan", plan=[("7 Iron", 24)])
    write_export(cfg.inbox, "b.csv", player="Christian", seed=2, plan=[("7 Iron", 24)])
    assert main(["--config", str(cfg_file), "ingest"]) == 0
    out = capsys.readouterr().out
    assert "(Jordan)" in out and "(Christian)" in out and out.count("Next focus:") == 2
    mine, his = cfg.dashboard_path("jordan").read_text(), cfg.dashboard_path("christian").read_text()
    assert '"player":"Jordan"' in mine and '"player":"Christian"' not in mine
    assert '"player":"Christian"' in his and '"player":"Jordan"' not in his


def test_a_dashboard_file_keeps_its_player_when_a_similar_name_arrives_later(cfg_file):
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "a.csv", player="jo-bui")
    main(["--config", str(cfg_file), "ingest"])
    write_export(cfg.inbox, "b.csv", player="Jo Bui", seed=2, start=START + timedelta(days=1))
    main(["--config", str(cfg_file), "ingest"])
    first, second = cfg.dashboard_path("jo-bui").read_text(), cfg.dashboard_path("jo-bui-2").read_text()
    assert '"player":"jo-bui"' in first and '"player":"Jo Bui"' not in first
    assert '"player":"Jo Bui"' in second and '"player":"jo-bui"' not in second


def test_dashboard_writes_wait_for_another_run_to_finish(cfg_file):
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "a.csv", player="Jordan")
    main(["--config", str(cfg_file), "ingest"])
    src = Path(__file__).resolve().parents[1] / "src"
    with open(cfg.data_dir / ".dashboards.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        before = cfg.dashboard_path("jordan").stat().st_mtime_ns
        proc = subprocess.Popen([sys.executable, "-m", "golfstats", "--config", str(cfg_file), "dashboard"],
                                env={**os.environ, "PYTHONPATH": str(src)}, stdout=subprocess.PIPE)
        time.sleep(1.5)
        assert proc.poll() is None
        assert cfg.dashboard_path("jordan").stat().st_mtime_ns == before
    assert proc.wait(timeout=30) == 0
    assert cfg.dashboard_path("jordan").stat().st_mtime_ns > before


def test_a_failed_index_write_leaves_the_old_index_intact(cfg_file, monkeypatch):
    from golfstats import dashboard
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "a.csv", player="Jordan")
    main(["--config", str(cfg_file), "ingest"])
    before = cfg.dashboard_index.read_text()
    write_export(cfg.inbox, "b.csv", player="Christian", seed=2)

    def fail(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(dashboard.os, "replace", fail)
    with pytest.raises(OSError):
        main(["--config", str(cfg_file), "ingest"])
    assert cfg.dashboard_index.read_text() == before
    assert not [p for p in cfg.data_dir.iterdir() if p.name.startswith(".dashboards.json.")]
