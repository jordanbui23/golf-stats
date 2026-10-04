from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from golfstats.config import Config
from golfstats.clubs import club_code
from golfstats.focus import grade_plan, make_plan, pick_focus, plan_for, save_plan
from golfstats.stats import curve_label, robust_sd, shape_label, side_decomposition, split_sessions, start_label

T0 = datetime(2026, 10, 1, 18, 0)


def cfg(tmp_path=None) -> Config:
    from pathlib import Path
    return Config(data_dir=Path(tmp_path or "/nonexistent"))


def shot(i: int, club: str = "7i", **metrics) -> dict:
    base = {"ts": T0 + timedelta(minutes=i), "player": "J", "club": club, "club_code": club, "use_in_stat": True,
            "spin_rate_type": "Measured", "ball_speed": 110.0}
    return {**base, **metrics}


def iron_shots(n: int = 20, **fixed) -> list[dict]:
    out = []
    for i in range(n):
        wobble = (i % 5 - 2) * 0.3
        m: dict[str, object] = {
            "low_point": 3.0 + wobble, "smash_index": 99.0, "impact_offset": wobble * 3, "face_angle": wobble,
            "club_path": -0.5 + wobble, "face_to_path": 0.5, "carry": 155 + wobble, "carry_side": wobble * 2,
            "curve": wobble}
        m.update({k: (v(i) if callable(v) else v) for k, v in fixed.items()})
        out.append({**shot(i), **m})
    return out


def session(shots: list[dict]):
    return split_sessions(shots, 90)[0]


def focus_for(shots: list[dict], c: Config | None = None) -> dict:
    f = pick_focus(session(shots), c or cfg())
    assert f is not None
    return f


def test_sessions_split_on_gap_and_player():
    shots = [shot(0), shot(10), shot(200), {**shot(201), "player": "K"}]
    sessions = split_sessions(shots, 90)
    assert [len(s.shots) for s in sessions] == [2, 1, 1]
    assert sessions[0].id == "2026-10-01-1800"


def test_same_start_different_players_get_distinct_ids():
    sessions = split_sessions([shot(0), {**shot(0), "player": "K"}], 90)
    assert len({s.id for s in sessions}) == 2


def test_robust_sd_ignores_one_shank():
    sd = robust_sd([150.0, 151.0, 152.0, 153.0, 154.0, 155.0, 60.0])
    assert sd is not None and sd < 5


@pytest.mark.parametrize("ld,axis,label", [
    (0.5, 0.0, "straight"), (0.0, 6.0, "fade"), (0.0, 14.0, "slice"), (-4.0, -5.0, "pull-draw"),
    (3.0, 0.5, "push"), (-3.0, -12.0, "pull-hook"),
])
def test_shape_labels(ld, axis, label):
    assert shape_label({"launch_direction": ld, "spin_axis": axis}) == label
    assert start_label(None) is None and curve_label(None) is None


def test_side_decomposition_finds_curve_dominant_miss():
    shots = [shot(i, carry_side=c + 1.0, curve=c) for i, c in enumerate([-12, -6, 0, 6, 12, 18])]
    d = side_decomposition(shots)
    assert d is not None and d["dominant"] == "curve" and d["start_sd"] == pytest.approx(0.0)


def test_no_focus_without_enough_shots():
    assert pick_focus(session(iron_shots(5)), cfg()) is None


def test_clean_session_holds_the_pattern():
    f = focus_for(iron_shots())
    assert f["metric"] == "face_to_path" and "is inside its window" in f["reason"]


def test_low_point_behind_the_ball_comes_first():
    f = focus_for(iron_shots(low_point=lambda i: -1.0 if i % 2 else 2.0, face_to_path=6.0))
    assert f["metric"] == "low_point" and f["today"]["hits"] == 10


def test_low_point_check_skips_driver():
    shots = [{**s, "club": "DR", "club_code": "DR", "low_point": -3.0} for s in iron_shots()]
    assert focus_for(shots)["metric"] != "low_point"


def test_low_smash_index_is_a_strike_focus():
    f = focus_for(iron_shots(smash_index=91.0))
    assert f["metric"] == "smash_index" and f["today"]["hits"] == 0


def test_wide_impact_spread_is_a_strike_focus():
    f = focus_for(iron_shots(impact_offset=lambda i: (i % 5 - 2) * 9.0))
    assert f["metric"] == "impact_offset"


def test_curve_dominated_miss_picks_face_to_path():
    shots = iron_shots(face_to_path=lambda i: 4.0 + (i % 5 - 2), face_angle=3.0,
                       curve=lambda i: (i % 5 - 2) * 6.0, carry_side=lambda i: (i % 5 - 2) * 6.0 + 1)
    f = focus_for(shots)
    assert f["metric"] == "face_to_path" and "Curve explains more" in f["reason"]


def test_start_dominated_miss_picks_face_angle():
    shots = iron_shots(face_to_path=4.0, face_angle=lambda i: (i % 5 - 2) * 2.0,
                       curve=1.0, carry_side=lambda i: (i % 5 - 2) * 7.0 + 1)
    f = focus_for(shots)
    assert f["metric"] == "face_angle" and "Start line explains more" in f["reason"]


def test_preferred_draw_moves_the_face_to_path_window():
    c = cfg()
    c.preferred_shape = "draw"
    f = focus_for(iron_shots(face_to_path=-2.0), c)
    assert f["window"] == [-4.0, 0.0] and "is inside its window" in f["reason"]


def test_plan_round_trip_and_grading(tmp_path):
    c = cfg(tmp_path)
    first = session(iron_shots(face_to_path=5.0))
    focus = pick_focus(first, c)
    assert focus is not None
    save_plan(make_plan(first, focus, c), c.plans)
    later = split_sessions([{**s, "ts": s["ts"] + timedelta(days=7),
                             "face_to_path": 0.0 if i < 12 else 5.0} for i, s in enumerate(iron_shots(30))], 90)[0]
    sessions = [first, later]
    plan = plan_for(later, sessions, c.plans)
    assert plan and plan["source_session"] == first.id
    assert plan_for(first, sessions, c.plans) is None
    grade = grade_plan(plan, later)[0]
    assert (grade["hits"], grade["n"], grade["baseline"]["hits"]) == (12, 20, 0)


def test_a_plan_only_grades_the_next_session_of_the_same_player(tmp_path):
    c = cfg(tmp_path)
    first = session(iron_shots(face_to_path=5.0))
    focus = pick_focus(first, c)
    assert focus is not None
    save_plan(make_plan(first, focus, c), c.plans)
    moved = lambda days, player="J": [{**s, "ts": s["ts"] + timedelta(days=days), "player": player} for s in iron_shots(5)]
    second, third = session(moved(7)), session(moved(14))
    other = session(moved(7, "K"))
    sessions = [first, second, third, other]
    assert plan_for(second, sessions, c.plans) is not None
    assert plan_for(third, sessions, c.plans) is None
    assert plan_for(other, sessions, c.plans) is None


def test_unknown_club_names_cannot_carry_markup():
    code = club_code('<img src=x onerror="alert(1)">')
    assert not set(code) & set('<>"=()')
    assert club_code("56° Wedge") == "56°" and club_code("Pitching Wedge") == "PW" and club_code("4 Hybrid") == "4H"


def sparse(n: int = 30, every: int = 5, **club_data) -> list[dict]:
    out = []
    for s in iron_shots(n, launch_direction=lambda i: (i % 5 - 2) * 1.5):
        if s["ts"].minute % every:
            for k in ("low_point", "smash_index", "impact_offset", "face_angle", "club_path", "face_to_path", "curve"):
                s[k] = None
        else:
            s.update({k: (v(s) if callable(v) else v) for k, v in club_data.items()})
        out.append(s)
    return out


def test_club_data_on_too_few_shots_never_drives_the_focus():
    shots = sparse(low_point=-2.0, face_angle=6.0, face_to_path=9.0)
    f = pick_focus(session(shots), cfg())
    assert f["metric"] == "launch_direction"
    assert "measured on only 6 of 30 shots" in f["reason"]


def test_the_same_club_data_on_enough_shots_does_drive_it():
    shots = sparse(every=1, low_point=-2.0)
    assert pick_focus(session(shots), cfg())["metric"] == "low_point"


def test_sparse_club_data_checks_start_line_with_launch_direction():
    shots = sparse(launch_direction=None)
    for s in shots:
        s["launch_direction"] = 4.0 + (s["ts"].minute % 3) * 0.2
    f = pick_focus(session(shots), cfg())
    assert f["metric"] == "launch_direction" and f["window"] == [-2.0, 2.0]
    assert f["today"]["n"] == 30 and f["today"]["hits"] == 0


def test_sparse_club_data_holds_on_launch_direction_when_it_is_in_window():
    shots = sparse()
    for s in shots:
        s["launch_direction"] = (s["ts"].minute % 3 - 1) * 0.3
    f = pick_focus(session(shots), cfg())
    assert f["metric"] == "launch_direction" and "inside its window" in f["reason"]


def test_spreads_and_the_side_miss_split_need_five_shots():
    assert robust_sd([1.0, 2.0, 3.0, 4.0]) is None and robust_sd([1.0, 2.0, 3.0, 4.0, 5.0]) is not None
    few = iron_shots(4, carry_side=lambda i: i * 2.0, curve=lambda i: i * 1.5)
    assert side_decomposition(few) is None and side_decomposition(iron_shots(5)) is not None
