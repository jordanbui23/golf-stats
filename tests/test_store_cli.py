import csv
import io
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


def write_conflicting(folder: Path, name: str, source: Path) -> Path:
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    rows = list(csv.reader(lines[1:]))
    col = rows[0].index("Club Speed")
    for r in rows[2:]:
        r[col] = repr(float(r[col]) + 1.5)
    buf = io.StringIO()
    buf.write(lines[0] + "\r\n")
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    path = folder / name
    path.write_text(buf.getvalue())
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


def test_conflicting_values_are_reported_kept_then_replaced_on_request(tmp_path):
    conn = connect(tmp_path / "golf.db")
    first = write_export(tmp_path / "in", "a.csv", seed=1)
    ingest_file(conn, first, tmp_path / "raw")
    before = {s["shot_key"]: s["club_speed"] for s in load_shots(conn)}
    other = write_conflicting(tmp_path / "in", "b.csv", first)
    res = ingest_file(conn, other, tmp_path / "raw")
    assert (res.shots_added, len(res.conflicts), res.replaced) == (0, 20, 0)
    assert {s["shot_key"]: s["club_speed"] for s in load_shots(conn)} == before
    again = ingest_file(conn, other, tmp_path / "raw", replace=True)
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
