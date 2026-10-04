from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from golfstats.config import Config
from golfstats.focus import pick_focus
from golfstats.report import render_report
from golfstats.stats import split_sessions
from golfstats.strike import bag, strike_class, strike_references, strike_summary

T0 = datetime(2026, 10, 1, 18, 0)


def cfg() -> Config:
    return Config(data_dir=Path("/nonexistent"))


def shot(i: int, club: str = "7i", smash: float | None = 1.35, carry: float = 150.0, start: float = 0.0,
         day: int = 0, player: str = "J", **kw) -> dict:
    return {"ts": T0 + timedelta(days=day, minutes=i), "player": player, "club": club, "club_code": club,
            "use_in_stat": True, "spin_rate_type": "Estimated", "ball_speed": 110.0, "smash_factor": smash,
            "carry": carry, "launch_direction": start, **kw}


def with_mishits(bad: int, n: int = 20, club: str = "7i", day: int = 0, player: str = "J") -> list[dict]:
    return [shot(i, club, smash=1.0 if i < bad else 1.35, carry=110.0 if i < bad else 150.0,
                 start=15.0 if i < bad else 0.0, day=day, player=player) for i in range(n)]


def sessions(shots: list[dict]):
    return split_sessions(shots, 90)


def test_a_shot_with_no_ball_data_is_not_counted_and_the_report_says_so():
    misread = {**shot(30), "ball_speed": None, "smash_factor": None, "carry": 0.0}
    sess = sessions([*with_mishits(0), misread])[0]
    assert len(sess.shots) == 21 and len(sess.counted) == 20
    assert "1 shot(s) have no ball data" in render_report(sess, [sess], None, None, cfg=cfg())


def test_a_mishit_is_well_below_your_best_smash_with_that_club():
    sess = sessions(with_mishits(5))[0]
    refs = strike_references(sess, [sess], 0.85, 8)
    assert refs["7i"]["threshold"] == 1.148
    st = strike_summary(sess.counted, refs["7i"])
    assert st is not None
    assert (st["mishits"], st["n"], st["carry_lost"], st["start_mishit"], st["start_solid"]) == (5, 20, 40.0, 15.0, 0.0)


def test_strike_is_not_rated_until_a_club_has_enough_shots():
    sess = sessions(with_mishits(2, n=7))[0]
    assert strike_references(sess, [sess], 0.85, 8) == {}


def test_a_later_session_or_another_player_never_moves_the_threshold():
    early = with_mishits(5)
    later = [shot(i, smash=1.6, day=7) for i in range(20)]
    other = [shot(i, smash=1.6, player="K") for i in range(20)]
    all_sessions = sessions([*early, *later, *other])
    first = next(s for s in all_sessions if s.player == "J" and s.start == T0)
    second = next(s for s in all_sessions if s.player == "J" and s.start > T0)
    assert strike_references(first, all_sessions, 0.85, 8)["7i"]["threshold"] == 1.148
    assert strike_references(second, all_sessions, 0.85, 8)["7i"]["n"] == 40


def test_focus_is_strike_when_too_many_shots_are_mishits():
    sess = sessions(with_mishits(6))[0]
    f = pick_focus(sess, cfg(), [sess])
    assert f is not None
    assert f["metric"] == "smash_factor" and f["window"][1] == 200.0
    assert f["window"][0] == 1.148 and "smash factor under 1.148" in f["reason"]
    assert f["reason"].startswith("6 of 20 shots were mishits") and "40 yds less" in f["reason"]
    assert f["today"]["hits"] == 14


def test_focus_moves_on_to_start_line_when_mishits_are_rare():
    shots = with_mishits(2)
    for i, s in enumerate(shots[2:]):
        s["launch_direction"] = 6.0 + (i % 3)
    sess = sessions(shots)[0]
    f = pick_focus(sess, cfg(), [sess])
    assert f is not None and f["metric"] == "launch_direction"


def test_bag_uses_solid_strikes_and_measures_the_gap_to_the_next_shorter_club():
    shots = [*with_mishits(6), *[shot(30 + i, "9i", carry=125.0) for i in range(20)]]
    sess = sessions(shots)[0]
    rows = bag(sess, [sess], strike_references(sess, [sess], 0.85, 8))
    assert [(r["club"], r["carry"], r["gap"], r["n"], r["solid_only"]) for r in rows] == [
        ("7i", 150.0, 25.0, 14, True), ("9i", 125.0, None, 20, True)]


def test_bag_keeps_a_club_with_no_smash_factor_using_every_shot():
    shots = [*[shot(i, "DR", smash=None, carry=220.0) for i in range(5)], *with_mishits(0, day=0)]
    sess = sessions(shots)[0]
    rows = bag(sess, [sess], strike_references(sess, [sess], 0.85, 8))
    assert rows[0]["club"] == "DR" and rows[0]["solid_only"] is False and rows[0]["n"] == 5


def test_report_has_strike_and_bag_sections():
    sess = sessions(with_mishits(6))[0]
    text = render_report(sess, [sess], None, None, cfg=cfg())
    assert "- **7i**: 6 of 20 mishits (smash factor under 1.148)." in text
    assert "| 7i | 150 |" in text


def test_your_best_is_the_90th_percentile_not_the_median():
    sess = sessions([shot(i, smash=1.20 + 0.01 * i) for i in range(20)])[0]
    assert strike_references(sess, [sess], 0.85, 8)["7i"]["best"] == pytest.approx(1.371)


def test_a_shot_just_under_the_threshold_is_a_mishit():
    sess = sessions([*[shot(i, smash=1.37) for i in range(19)], shot(19, smash=1.16)])[0]
    refs = strike_references(sess, [sess], 0.85, 8)
    assert refs["7i"]["threshold"] == 1.165
    assert strike_class(sess.counted[-1], refs) == "mishit"


def test_strike_is_rated_and_reported_when_no_shot_has_carry():
    sess = sessions([{**s, "carry": None} for s in with_mishits(6)])[0]
    text = render_report(sess, [sess], pick_focus(sess, cfg(), [sess]), None, cfg=cfg())
    assert "- **7i**: 6 of 20 mishits" in text and "yds less" not in text


def test_the_dashboard_shows_the_same_threshold_that_grades():
    import json
    from golfstats.dashboard import render_dashboard
    html = render_dashboard(sessions(with_mishits(6)), cfg())
    data = json.loads(html.split("const DATA = ", 1)[1].split(";</script>", 1)[0])
    assert data["sessions"][0]["clubs"]["7i"]["strike"]["threshold"] == 1.148
    assert data["sessions"][0]["focus"]["window"][0] == 1.148
