import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from golfstats.synth import synth_session_csv  # noqa: E402

FIXTURES = ROOT / "web" / "test" / "fixtures"
SPECS = {
    "jordan.csv": (datetime(2026, 10, 1, 18), [("7 Iron", 20)], 1, "Jordan"),
    "christian.csv": (datetime(2026, 10, 2, 18), [("Driver", 8), ("Pitching Wedge", 6)], 2, "Christian"),
}


def fixture_bytes(name: str) -> bytes:
    start, plan, seed, player = SPECS[name]
    return synth_session_csv(start, plan, 0.5, -3.0, seed=seed, player=player).encode("utf-8")


def test_web_fixtures_are_synthetic():
    assert sorted(p.name for p in FIXTURES.iterdir()) == sorted(SPECS)
    for name in SPECS:
        assert (FIXTURES / name).read_bytes() == fixture_bytes(name), (
            f"web/test/fixtures/{name} is not synth.py output. Run: .venv/bin/python tests/test_web_fixtures.py")


if __name__ == "__main__":
    for name in SPECS:
        (FIXTURES / name).write_bytes(fixture_bytes(name))
        print(f"wrote web/test/fixtures/{name}")
