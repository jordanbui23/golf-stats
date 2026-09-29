from __future__ import annotations

import math
from datetime import datetime

import pytest

from golfstats.synth import TPS_HEADER, TPS_UNITS, synth_session_csv
from golfstats.tps_csv import ParseError, parse_date, parse_number, parse_tps_csv

START = datetime(2026, 10, 1, 18, 0, 0)


def export(**kw) -> str:
    return synth_session_csv(START, kw.pop("plan", [("7 Iron", 20)]), 0.5, -3.0, seed=kw.pop("seed", 1), **kw)


def tiny_csv(header: list[str], units: list[str] | None, rows: list[list[str]], sep: str = ",") -> str:
    lines = [f"sep={sep}", sep.join(header)]
    if units is not None:
        lines.append(sep.join(units))
    lines += [sep.join(r) for r in rows]
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


def test_real_tps_layout_parses_every_row():
    parsed = parse_tps_csv(export(plan=[("7 Iron", 20), ("Driver", 5)]))
    assert len(parsed.shots) == 25
    assert parsed.delimiter == ","
    assert parsed.unmapped == []
    assert parsed.source_units["curve"] == "ft"
    assert {s["club"] for s in parsed.shots} == {"7 Iron", "Driver"}
    assert parsed.shots[0]["ts"] == START


def test_curve_feet_become_yards_and_side_stays_yards():
    parsed = parse_tps_csv(export())
    shot = parsed.shots[3]
    assert shot["curve"] == pytest.approx(float(shot["raw"]["Curve"]) / 3)
    assert shot["carry_side"] == pytest.approx(float(shot["raw"]["Carry Flat - Side"]))


def test_side_minus_curve_is_the_start_line_component():
    for shot in parse_tps_csv(export()).shots:
        start = shot["carry"] * math.tan(math.radians(shot["launch_direction"]))
        assert shot["carry_side"] - shot["curve"] == pytest.approx(start, abs=0.01)


def test_use_in_stat_false_and_missing_club_data():
    parsed = parse_tps_csv(export(plan=[("7 Iron", 24)]))
    assert [s["use_in_stat"] for s in parsed.shots].count(False) == 1
    assert sum(1 for s in parsed.shots if s["face_angle"] is None) == 1
    assert all(s["club_speed"] is not None for s in parsed.shots)


def test_email_and_filename_columns_are_dropped():
    shot = parse_tps_csv(export()).shots[0]
    assert "Email" not in shot["raw"] and "TMD Filename" not in shot["raw"]


def test_semicolon_export_with_decimal_commas_and_metric_units():
    header = ["Date", "Player", "Club", "Club Speed", "Ball Speed", "Carry Flat - Length", "Curve", "Low Point"]
    units = ["", "", "", "[m/s]", "[km/h]", "[m]", "[m]", "[cm]"]
    row = ["14.10.2026 18:03:11", "J", "7 Iron", "36,5", "180,0", "146,3", "---", "10,16"]
    shot = parse_tps_csv(tiny_csv(header, units, [row], sep=";")).shots[0]
    assert shot["ts"] == datetime(2026, 10, 14, 18, 3, 11)
    assert shot["club_speed"] == pytest.approx(81.65, abs=0.01)
    assert shot["ball_speed"] == pytest.approx(111.85, abs=0.01)
    assert shot["carry"] == pytest.approx(160.0, abs=0.01)
    assert shot["curve"] is None
    assert shot["low_point"] == pytest.approx(4.0)


def test_units_embedded_in_header_names():
    header = ["Date", "Club", "Club Speed [mph]", "Carry [yds]", "Smash Factor"]
    shot = parse_tps_csv(tiny_csv(header, None, [["5/6/2026 6:58:02 PM", "PW", "74", "121", "1.24"]])).shots[0]
    assert (shot["club_speed"], shot["carry"], shot["smash_factor"]) == (74.0, 121.0, 1.24)


def test_missing_units_row_is_refused():
    header = ["Date", "Club", "Club Speed", "Carry Flat - Length"]
    with pytest.raises(ParseError, match="no units"):
        parse_tps_csv(tiny_csv(header, None, [["5/6/2026 6:58:02 PM", "PW", "33", "110"]]))


def test_unknown_unit_is_refused():
    header = ["Date", "Club", "Club Speed", "Carry Flat - Length"]
    with pytest.raises(ParseError, match="unknown length unit"):
        parse_tps_csv(tiny_csv(header, ["", "", "[mph]", "[furlong]"], [["5/6/2026 6:58:02 PM", "PW", "74", "1"]]))


def test_new_columns_are_reported_not_dropped():
    header = [*TPS_HEADER, "Launch Wobble"]
    units = [*TPS_UNITS, "[deg]"]
    text = export().replace(",".join(TPS_HEADER), ",".join(header), 1).replace(",".join(TPS_UNITS), ",".join(units), 1)
    parsed = parse_tps_csv(text)
    assert parsed.unmapped == ["Launch Wobble"]


def test_no_header_is_refused():
    with pytest.raises(ParseError, match="header"):
        parse_tps_csv("a,b,c\n1,2,3\n")


@pytest.mark.parametrize("text,expected", [
    ("5/6/2026 6:58:02 PM", datetime(2026, 5, 6, 18, 58, 2)),
    ("2026-05-06 18:58:02", datetime(2026, 5, 6, 18, 58, 2)),
    ("06.05.2026 18:58:02", datetime(2026, 5, 6, 18, 58, 2)),
])
def test_date_formats(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text,expected", [("", None), ("---", None), ("-1.5", -1.5), ("3,25", 3.25), ("\u22122", -2.0)])
def test_parse_number(text, expected):
    assert parse_number(text) == expected
