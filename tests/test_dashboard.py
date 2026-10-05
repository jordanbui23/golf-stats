from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from golfstats.config import Config
from golfstats.dashboard import above_baseline, build_data, render_dashboard
from golfstats.stats import split_sessions

T0 = datetime(2026, 10, 1, 18, 0)


def shot(i: int, day: int, club: str = "7i", mishit: bool = False, **kw) -> dict:
    return {"ts": T0 + timedelta(days=day, minutes=i), "player": "J", "club": club, "club_code": club,
            "use_in_stat": True, "spin_rate_type": "Estimated", "ball_speed": 110.0,
            "smash_factor": 1.0 if mishit else 1.35, "carry": 110.0 if mishit else 150.0,
            "launch_direction": 15.0 if mishit else 0.0, **kw}


def iron_session(day: int, mishits: int) -> list[dict]:
    return [shot(i, day, mishit=i < mishits) for i in range(20)]


def three_sessions() -> list[dict]:
    extra_b = [shot(30 + i, 1, club="DR") for i in range(3)] + [shot(40, 1, club="DR", ball_speed=None)]
    extra_c = [shot(30, 2, club="PW", ball_speed=None)]
    return iron_session(0, 6) + iron_session(1, 2) + extra_b + iron_session(2, 10) + extra_c


def test_overview_counts_sessions_shots_and_clubs_across_every_session():
    data = build_data(split_sessions(three_sessions(), 90), Config(data_dir=Path("/nonexistent")))
    ov = data["overview"]
    assert (ov["sessions"], ov["counted"], ov["no_reads"]) == (3, 63, 2)
    assert ov["clubs"] == [{"club": "DR", "shots": 3, "sessions": 1, "last": 1},
                           {"club": "7i", "shots": 60, "sessions": 3, "last": 2}]


def test_overview_counts_plans_that_beat_the_share_in_window_when_set():
    data = build_data(split_sessions(three_sessions(), 90), Config(data_dir=Path("/nonexistent")))
    grades = [[(g["hits"], g["n"], g["baseline"]["hits"], g["baseline"]["n"], g["above_baseline"])
               for g in s["grades"] or []] for s in data["sessions"]]
    assert grades == [[], [(18, 20, 14, 20, True)], [(10, 20, 18, 20, False)]]
    assert (data["overview"]["plans_graded"], data["overview"]["plans_above_baseline"]) == (2, 1)


@pytest.mark.parametrize("hits,n,base_hits,base_n,expected", [
    (3, 4, 2, 3, True), (2, 4, 1, 2, False), (1, 3, 2, 3, False), (0, 0, 1, 2, None), (1, 2, 0, 0, None),
])
def test_above_baseline_needs_a_strictly_higher_share(hits, n, base_hits, base_n, expected):
    assert above_baseline({"hits": hits, "n": n, "baseline": {"hits": base_hits, "n": base_n}}) is expected


def test_overview_with_two_players_covers_only_the_latest_one():
    other = [{**s, "player": "K", "ts": s["ts"] + timedelta(days=10)} for s in iron_session(0, 0)[:9]]
    data = build_data(split_sessions(three_sessions() + other, 90), Config(data_dir=Path("/nonexistent")))
    ov = data["overview"]
    assert [s["player"] for s in data["sessions"]] == ["J", "J", "J", "K"]
    assert (ov["player"], ov["session_indexes"], ov["sessions"], ov["counted"]) == ("K", [3], 1, 9)
    assert ov["clubs"] == [{"club": "7i", "shots": 9, "sessions": 1, "last": 3}]


def chrome() -> str | None:
    if os.environ.get("GOLF_TEST_CHROME"):
        return os.environ["GOLF_TEST_CHROME"]
    found = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    return found[-1] if found else None


CHROME = chrome()

PROBE = """<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const view = () => ({ overview: !$("overview").hidden, session: !$("sessionView").hidden, select: $("session").value,
                        home: $("home").classList.contains("on") });
  const click = (node) => node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  const steps = { landing: view() };
  click(document.querySelector('#ovClubs tr[data-k="DR"]'));
  steps.picked = document.querySelector("#clubs .chip.on").textContent;
  click(document.querySelector('#sessions tr[data-k="1"]'));
  steps.row = { ...view(), club: document.querySelector("#clubs .chip.on").textContent };
  click($("home"));
  steps.home = view();
  click(document.querySelector('#volume rect[data-k="0"]'));
  steps.bar = view();
  $("session").value = "2";
  $("session").dispatchEvent(new Event("change"));
  steps.menu = view();
  $("session").value = "overview";
  $("session").dispatchEvent(new Event("change"));
  steps.select = view();
  const probe = document.createElement("pre");
  probe.id = "probe";
  probe.textContent = JSON.stringify(steps);
  document.body.appendChild(probe);
})();
</script>
</body>"""


def browser_dom(tmp_path: Path, html: str) -> str:
    assert CHROME
    page = tmp_path / "dashboard.html"
    page.write_text(html)
    return subprocess.run([CHROME, "--no-sandbox", "--disable-gpu", "--virtual-time-budget=2000", "--dump-dom",
                           page.as_uri()], capture_output=True, text=True, timeout=60).stdout


@pytest.mark.skipif(CHROME is None, reason="needs a headless Chromium, or GOLF_TEST_CHROME")
def test_the_dashboard_lands_on_the_overview_in_a_browser(tmp_path):
    dom = browser_dom(tmp_path, render_dashboard(split_sessions(three_sessions(), 90), Config(data_dir=tmp_path)))
    assert '<div id="overview">' in dom and '<div id="sessionView" hidden="">' in dom
    rows = re.findall(r'<tr class="link" tabindex="0" data-k="(\d+)">', dom)
    assert rows == ["2", "1", "0"]
    assert '<option value="overview">All sessions</option>' in dom


@pytest.mark.skipif(CHROME is None, reason="needs a headless Chromium, or GOLF_TEST_CHROME")
def test_rows_bars_the_menu_and_the_overview_button_switch_views_in_a_browser(tmp_path):
    html = render_dashboard(split_sessions(three_sessions(), 90), Config(data_dir=tmp_path))
    dom = browser_dom(tmp_path, html.replace("</body>", PROBE, 1))
    steps = json.loads(dom.split('<pre id="probe">', 1)[1].split("</pre>", 1)[0])
    overview = {"overview": True, "session": False, "select": "overview", "home": True}
    assert steps["landing"] == overview and steps["picked"] == "DR 3"
    assert steps["row"] == {"overview": False, "session": True, "select": "1", "home": False, "club": "7i 20"}
    assert steps["home"] == overview
    assert steps["bar"] == {"overview": False, "session": True, "select": "0", "home": False}
    assert steps["menu"] == {"overview": False, "session": True, "select": "2", "home": False}
    assert steps["select"] == overview
