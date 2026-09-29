from datetime import datetime, timedelta
from pathlib import Path

import pytest

from golfstats.__main__ import main
from golfstats.config import load_config
from golfstats.store import connect, ingest_file, load_shots
from golfstats.synth import synth_session_csv

START = datetime(2026, 10, 1, 18, 0, 0)


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    return path


def write_export(folder: Path, name: str, start: datetime = START, seed: int = 1, **kw) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(synth_session_csv(start, kw.pop("plan", [("7 Iron", 20)]), 0.5, -3.0, seed=seed, **kw))
    return path


def test_ingest_archives_a_byte_identical_copy_and_counts_shots(tmp_path):
    src = write_export(tmp_path / "in", "a.csv")
    conn = connect(tmp_path / "golf.db")
    res = ingest_file(conn, src, tmp_path / "raw")
    assert (res.shots_in_file, res.shots_added) == (20, 20)
    assert res.archived_path and res.archived_path.read_bytes() == src.read_bytes()
    assert len(load_shots(conn)) == 20


def test_same_file_twice_is_skipped(tmp_path):
    src = write_export(tmp_path / "in", "a.csv")
    conn = connect(tmp_path / "golf.db")
    ingest_file(conn, src, tmp_path / "raw")
    again = ingest_file(conn, src, tmp_path / "raw")
    assert again.already_imported
    assert len(load_shots(conn)) == 20


def test_overlapping_exports_do_not_duplicate_shots(tmp_path):
    conn = connect(tmp_path / "golf.db")
    first = write_export(tmp_path / "in", "a.csv", plan=[("7 Iron", 20)])
    ingest_file(conn, first, tmp_path / "raw")
    both = write_export(tmp_path / "in", "b.csv", plan=[("7 Iron", 20), ("Driver", 6)])
    res = ingest_file(conn, both, tmp_path / "raw")
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
    archived = list(cfg.archive.glob("*.csv"))
    assert len(archived) == 1 and archived[0].read_bytes() == original


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
    html = cfg.dashboard.read_text()
    assert "/*__DATA__*/null" not in html and '"sessions":[' in html and "NaN" not in html


def test_player_filter(cfg_file, tmp_path):
    cfg_file.write_text(cfg_file.read_text() + '[player]\nname = "Someone Else"\n')
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "s1.csv")
    main(["--config", str(cfg_file), "ingest"])
    assert main(["--config", str(cfg_file), "report"]) == 1
