# TrackMan export facts

Spec of record for what the parser and the analysis assume about TrackMan data. Every
load-bearing claim carries a label. `confirmed` means TrackMan's own documentation says it.
`supported` means one good source. `unverified` means no source yet. Update this file whenever
an export from the apartment unit contradicts it or TPS changes.

Checked 2026-09-30, after the first export from the apartment unit (TPS version unknown, two
players, 90 shots, 8 and 9 irons). The figures below from that export come from loading it
with `store.load_shots` and counting per shot.

## Export formats

- TPS offers five export formats: Stroke File, Excel File, CSV File, Support File and Model
  File. `confirmed`: the export dialog screenshot in TrackMan's help article "Shot Analysis |
  How To Export A CSV File From TPS" (article 12985883274139, read through the help center
  API at `support.trackmangolf.com/api/v2/help_center/en-us/articles/<id>.json`).
- The dialog has a "Normalize data" checkbox (0 m altitude, 25 C, premium ball). `confirmed`,
  same screenshot. Export with it unchecked.
- Portal "Report Sessions" are PDF reports. `confirmed`, article 38142841966747.

## CSV layout

All `supported` by one real TPS CSV export from another golfer, published in the
`open-flight/openflight` repo as `session_logs/OpenFlight-Test.Normalized.csv` (May 2026,
TPS version unknown). A second repo's parser (`craigjhudson-source/Trackman-Shaft-Fitting-App`,
`core/trackman.py`) handles the same shape.

- UTF-8 with a BOM, then a `sep=,` line, then the header row, then a units row such as
  `[mph]`, `[deg]`, `[yds]`, `[ft]`, `[mm]`, `[]`, then one row per shot. The apartment unit's
  export starts with two BOMs (`EF BB BF EF BB BF`). The parser strips every leading BOM.
- The apartment unit's header and units rows match the sample and `synth.py` exactly, all 61
  columns. `supported`: two independent exports agree.
- Dates look like `5/6/2026 6:58:02 PM`.
- 61 columns. The header names the parser maps are in `src/golfstats/fields.py`, and the
  fixture generator in `src/golfstats/synth.py` reproduces the header and units rows.
- `Use In Stat` is `TRUE` or `FALSE`. `Spin Rate Type` is `Measured` or `Estimated`.
- `Condition` holds text such as "Data are normalized to no wind conditions ..." when
  Normalize is on. Its content with Normalize off is `unverified`.
- The `(Sim)` columns were empty in both exports. `Email`, `TMD No` and `TMD Filename` were
  empty in the apartment export, and `Ball` was `Medium` on every shot.
- Units follow the TPS display settings. In that export, curve was in feet while side was in
  yards. `supported`. The parser converts every column by its units row, so this matters only
  if a units row is missing, and then it refuses the file.
- Non-US locales may use `;` and decimal commas. `unverified`: a Danish screenshot shows
  decimal commas in the TPS table, but no CSV from such a setup was found. The parser picks one
  decimal mark per file from the numeric cells and refuses a file that mixes them or contains
  a value it cannot read in that notation.

## Sign conventions (right-handed)

- Face angle, club path, launch direction, spin axis, side: positive is right. Face to path
  positive means the face is open to the path and the ball curves right. `confirmed`:
  TrackMan parameter articles 39724466444059, 39724275421211, 39724525751707, 39726408967323.
- Spin axis between -2 and +2 degrees is a straight shot. `confirmed`, article 39726408967323.
- Curve is the sideways movement from the launch direction line to the carry side, so side
  minus curve is the start-line part of the miss. `confirmed` definition, article
  39726823283099. On the sample export, side - curve differed from carry x tan(launch
  direction) by a median 0.02 yds and at most 1.9 yds over 24 shots. `supported`. Measured
  2026-09-29 by parsing the sample with `tps_csv.parse_tps_csv` and comparing the two per shot.
- Low point positive means the club bottoms out ahead of the ball. `supported`: TrackMan says a
  negative attack angle puts the low point after impact (article 39724600685339), and the
  sample's iron shots pair negative attack angles with positive low points.
- Impact offset sign (heel versus toe) and impact height sign: `unverified`.
- Left-handed conventions: `unverified`. The tool refuses `handedness = "L"`.

## Trackman 4 indoors

- Club data can be missing on most shots. In the apartment export, face angle, club path, face
  to path, attack angle, low point, dynamic loft, smash index and spin axis were present on only
  12 of 90 shots, and always together. Impact offset was missing on all 90. `supported`, one
  export. The cause is `unverified`.
- A shot without a spin axis is drawn straight: its carry side equals carry × tan(launch
  direction) within 0.5 yds on 71 of 78 such shots, and within 5.3 yds on all of them.
  `supported`, same export. So curve is unknown on those shots, and their side miss is start
  line only. The analysis never derives curve for them.
- Launch direction follows the face: on the 12 shots with club data it matched
  0.75 × face angle + 0.25 × club path within 0.14°. `supported`, same export, irons only. The
  focus picker checks start line with launch direction when a club has fewer than
  `focus.min_shots` face angles.
- `Spin Rate Type` was `Measured` on 1 of 90 shots, a 9 iron at 1,676 rpm next to estimated
  values near 6,500 rpm. Treat a lone measured value without RCT balls as suspect. `unverified`.

- Indoors the radar sees only part of the flight, so carry and total are less accurate.
  `confirmed`, article 36721370071707. The sample's `Last data Point - Length` was about 3 yds.
- Titleist RCT balls give measured rather than estimated indoor spin. `confirmed`, articles
  38067361673627 and 42441011433115.
- Impact location needs OERT on (Settings, Advanced, General) and impact lighting. `confirmed`,
  articles 36772396790427 and 7001184301851.
- Smash Index is measured smash over the club's maximum. Spin Index is measured spin over
  expected spin. `confirmed`, articles on Smash Index and 43770482090139.
