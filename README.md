# golf-stats

Turns TrackMan 4 session exports into one practice focus per session, a graded plan for the
next session, and a local dashboard. Everything runs on your own files. There is no server
and no account.

## After each session

1. In TPS: Practice, Shot Analysis, Library, pick the session, switch to Table View, select
   all shots, click the export icon, choose **TrackMan CSV File**, leave **Normalize data**
   unchecked, Export All. Save it to a USB stick.
2. Copy the file into `data/inbox/`, for example `scp session.csv cloud:~/projects/golf-stats/data/inbox/`.
3. Run `bin/golf ingest`.
4. Open `data/dashboard.html`. The report for the session is in `data/reports/`.

Ingest archives each export byte for byte in `data/raw/`, removes it from the inbox, skips a
file it has seen, and skips shots already stored, so exporting the whole library again is safe.
A file that fails to parse stays in the inbox and the error names the column or unit at fault.
Files passed by path from outside the inbox are never deleted.

## Commands

| Command | What it does |
|---|---|
| `bin/golf ingest [files...]` | Import exports (default: everything in `data/inbox`), write reports and plans for the sessions they touch, rebuild the dashboard |
| `bin/golf report [session]` | Print the report for a session (default: the latest) |
| `bin/golf sessions` | List sessions |
| `bin/golf dashboard` | Rebuild `data/dashboard.html` |
| `bin/golf demo` | Build a dashboard from four synthetic sessions in `data/demo/` |

## How the focus is picked

The focus picker looks at the club with the most counted shots, once it has at least
`focus.min_shots`. It checks strike before direction and stops at the first check that fails:

1. Irons and wedges: too many shots bottoming out at or behind the ball.
2. Median Smash Index below target.
3. Heel-to-toe impact spread too wide.
4. Direction. The side miss is split into start line (side minus curve, driven by face angle)
   and curve (driven by face to path). Whichever spread is larger is checked first against
   its window.

If every check passes, it keeps face to path as the target so the pattern has to repeat.
The chosen target becomes a plan in `data/plans/`, and the next session's report grades it:
how many of the planned shots landed in the window, against the count when it was set.

Every threshold and window is in `config.toml`. They are starting heuristics, not TrackMan
guidance. `player.preferred_shape` moves the face to path window for a draw or a fade.

## Layout

```
bin/golf                  CLI wrapper (uses .venv/bin/python when present)
config.toml               player, session gap, focus thresholds and windows
docs/TRACKMAN.md          what the export and parameters look like, with confidence labels
src/golfstats/
  fields.py               TPS header names -> canonical keys and units
  tps_csv.py              parser: sep line, header, units row, unit conversion, dates
  store.py                SQLite store and ingest (dedupe by file hash and by shot)
  clubs.py                club names -> codes (DR, 7i, PW) and bag order
  stats.py                sessions, robust medians and spreads, shot shape, side-miss split
  focus.py                focus picker, plan writing and grading
  report.py               markdown session report
  dashboard.py            builds the dashboard data
  dashboard.html          dashboard template (vanilla JS, inline SVG, works offline)
  synth.py                synthetic TPS exports for tests and the demo
tests/                    pytest suite, offline
data/                     gitignored: golf.db, inbox/, raw/, reports/, plans/, dashboard.html
```

Data flow: CSV, then `tps_csv` converts every value to mph, yds, ft, in, mm, deg and rpm by
its units row, then `store` writes the shots to SQLite with the original row kept as JSON,
then `stats` groups shots into sessions by time gap, and `focus`, `report` and `dashboard`
read those sessions.

## Setup

Python 3.12, standard library only at runtime. Pandas is not used because numpy does not
build on this box's compiler.

```
/home/jbui/.local/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Known limits

- Indoors the radar tracks about 3 yds of flight, so carry, side and curve are TrackMan's
  predictions from launch and spin. The club numbers and launch numbers are measured.
- The export format comes from one real TPS export by another golfer. The first export from
  this unit is the real test. See `docs/TRACKMAN.md`.
- Right-handed only. The heel/toe sign of impact offset is unverified.
