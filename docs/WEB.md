# Web upload site and box sync

This is the spec of record for the upload site in `web/` and for `bin/golf sync`. When the code
and this file disagree, fix both together.

## Shape

The site runs on Cloudflare Pages: static pages in `web/public/`, Pages Functions in
`web/functions/`, one D1 database. It does four things: sign-in, storing each uploaded CSV
with its ingestion time, revert and restore of an upload, and serving the latest published
analysis. It runs no analysis. The free plan caps a request at 10 ms of CPU, and the Python
parser alone takes longer than that on one export.

The analysis runs on the box, meaning any machine with this repo, when someone runs
`bin/golf sync`. Sync pushes local CLI ingests to the site, pulls every upload and its state,
rebuilds the local store, and publishes one dashboard per site user. Until the next sync, the
site shows a new upload's raw shots, parsed in the browser for display only.

The uploads table is a ledger. A revert never deletes anything. It marks the upload, and the
next sync rebuilds the shots from the uploads that are still active.

## Ledger rules (both sides)

- An upload is identified by the SHA-256 of its original bytes.
- An upload is active when `reverted_at` is NULL.
- The site refuses a second active upload with the same SHA-256. It answers with the existing
  upload and `duplicate: true`. A restore that would create a second active copy is refused
  with 409.
- A shot is identified by `player|timestamp|club`, as in `store.shot_key`.
- Among the active uploads that contain a shot, the winner is the most recent upload with
  `replace_stored = 1`, or else the earliest upload. Order is by site upload id. The box
  orders by `(site_id IS NULL, site_id, id)`, so an upload that is not on the site yet sorts
  after every site upload, and once pushed it takes the site's order.
- `ledger_version` is a counter in D1. Every change to an upload's active state or
  `replace_stored`, and every new upload, increments it and writes the new value into that
  upload's `changed_version`, in one D1 batch. Results written by the box do not increment it.
- A published analysis records the `ledger_version` it was built from. For a user, an upload
  is pending when it is visible to them and its `changed_version` is greater than their
  analysis's `based_on_version`, or when they have no analysis yet.

## Sign-in

The password never reaches the server, and the server never runs a slow hash.

- Login key: PBKDF2-HMAC-SHA256 over the UTF-8 password, salt = UTF-8 of
  `"golf-stats:" + username.trim().toLowerCase()`, 600000 iterations, 32 bytes, sent as 64
  lowercase hex characters. The browser derives it with WebCrypto; the CLI derives it with
  `hashlib.pbkdf2_hmac`.
- Stored: `key_hash` = lowercase hex SHA-256 of the 32 raw key bytes.
- The server hashes the presented key once and compares digests in constant time. An unknown
  username does the same work against a fixed dummy hash.
- Throttle: before checking, count `login_failures` rows from the last 15 minutes whose
  username (lowercased) or IP matches. At 10 or more, answer 429. A failure adds a row and
  deletes rows older than one day. A success deletes that username's rows. The IP is the
  `CF-Connecting-IP` header, or `"local"` when absent.
- Known risk, accepted for a few known golfers: anyone who knows a username can keep that
  user locked out by failing 10 sign-ins every 15 minutes. The IP count also means one
  person's failures on the shared sim PC lock out everyone on that PC for 15 minutes.
- Session: 32 random bytes as hex in cookie `__Host-gs`, attributes
  `Path=/; Secure; HttpOnly; SameSite=Lax`. D1 keeps only the SHA-256 of the 32 raw token
  bytes. Lifetime
  is 12 hours, or 30 days with `remember: true`, which also sets `Max-Age`. Without
  `remember` the cookie has no `Max-Age`. A login deletes that user's expired sessions.
- CSRF: every non-GET request to `/api/*` outside `/api/sync/*`, including login, must carry
  an `Origin` header equal to the origin of the request URL. Otherwise 403.
- Box token: `/api/sync/*` requires `Authorization: Bearer <SYNC_TOKEN>`. When the
  `SYNC_TOKEN` secret is unset or shorter than 32 characters, every sync route answers 503.
  Compare SHA-256 digests in constant time.
- Usernames match `^[a-z0-9][a-z0-9._-]{0,63}$`, ignoring case. `box` is reserved, because
  uploads the box pushes carry `uploaded_by = 'box'`.

## Visibility

A user can see an upload when `uploaded_by_id` equals their user id, or when the upload's
`players` list shares a name with the user's `players` list, ignoring case. The same rule
gates download, revert, restore and the replace toggle. An upload the user cannot see
answers 404. A revert affects every user, because it applies to the file.

The rule runs in D1 through indexes, so a request reads only the uploads the user can see.
`upload_players` holds one row per upload and player name, folded with JavaScript
`toLowerCase()`. The user's names are folded the same way before they are bound. Uploads
the box pushes have no `uploaded_by_id`. Deleting a user sets `uploaded_by_id` to NULL on their
uploads, and user ids are never reused, so a new user with an old username sees only what
their own players allow.

## D1 schema

`web/migrations/0001_init.sql`, applied with `wrangler d1 migrations apply`. It has not been
applied to a deployed database yet. Once it has, every schema change goes in a new
migration file.

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  players TEXT NOT NULL DEFAULT '[]',
  key_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE web_sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
) STRICT;
CREATE TABLE login_failures (username TEXT NOT NULL, ip TEXT NOT NULL, at TEXT NOT NULL) STRICT;
CREATE INDEX login_failures_at ON login_failures(at);
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) STRICT;
INSERT INTO meta (key, value) VALUES ('ledger_version', 0);
CREATE TABLE uploads (
  id INTEGER PRIMARY KEY,
  uid TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  filename TEXT NOT NULL,
  size INTEGER NOT NULL,
  encoding TEXT NOT NULL CHECK (encoding IN ('gzip', 'identity')),
  uploaded_at TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  uploaded_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  players TEXT NOT NULL DEFAULT '[]',
  shots INTEGER,
  first_shot TEXT,
  last_shot TEXT,
  replace_stored INTEGER NOT NULL DEFAULT 0,
  reverted_at TEXT,
  reverted_by TEXT,
  changed_version INTEGER NOT NULL,
  result TEXT,
  result_version INTEGER
) STRICT;
CREATE INDEX uploads_sha ON uploads(sha256);
CREATE INDEX uploads_uploader ON uploads(uploaded_by_id);
CREATE TABLE upload_players (
  name_lc TEXT NOT NULL,
  upload_id INTEGER NOT NULL REFERENCES uploads(id),
  PRIMARY KEY (name_lc, upload_id)
) STRICT;
CREATE TABLE upload_chunks (
  upload_uid TEXT NOT NULL REFERENCES uploads(uid),
  seq INTEGER NOT NULL,
  data BLOB NOT NULL,
  PRIMARY KEY (upload_uid, seq)
) STRICT;
CREATE TABLE analyses (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  based_on_version INTEGER NOT NULL,
  published_at TEXT NOT NULL,
  sessions INTEGER NOT NULL,
  html_gz BLOB
) STRICT;
```

Times on the site are ISO 8601 UTC from `new Date().toISOString()`. D1 returns a BLOB as a JS
`Array` of byte values, so code converts with `new Uint8Array(value)`. A bound BLOB parameter
does not count toward D1's 100 KB statement limit, but a single value is capped at 2 MB
(`supported`: D1 limits page plus a workerd maintainer's answer in workerd issue 3049).
Stored upload bytes are split into chunks of at most 1,000,000 bytes.

An upload's `players`, `shots`, `first_shot` and `last_shot` start as the browser's display
parse. The box replaces `players` and `shots` with its own parse when it publishes a result.
`result_version` is the `ledger_version` of the publish that wrote `result`.

## User API (cookie)

All JSON responses carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
Errors are `{"error": "<sentence for a person>"}` with a 4xx or 5xx status.

| Route | Behaviour |
|---|---|
| `POST /api/login` | JSON `{username, key, remember}`. 200 `{username, display_name}` and the cookie, 401 wrong username or password, 429 throttled |
| `POST /api/logout` | Deletes the session, clears the cookie. 204 |
| `GET /api/me` | `{username, display_name, players}`, 401 without a session |
| `GET /api/dashboard` | `{ledger_version, analysis, pending}`. `analysis` is null or `{published_at, based_on_version, sessions}`. `pending` is a list of upload summaries, newest first, at most 500 |
| `GET /api/dashboard/html` | The published dashboard HTML, stored gzipped and returned with `Content-Encoding: gzip`. 404 when there is none or it has no sessions |
| `GET /api/uploads` | `{uploads}`: visible upload summaries, newest first, at most 500 |
| `POST /api/uploads` | Multipart, see below. 201 `{upload, duplicate: false}`, 200 `{upload, duplicate: true}` |
| `GET /api/uploads/:id/raw` | The original file as `text/csv`. Gzip storage is returned with `Content-Encoding: gzip`. `Content-Disposition: attachment` unless `?inline=1` |
| `POST /api/uploads/:id/revert` | Marks it reverted by this user. Idempotent. 200 `{upload}` |
| `POST /api/uploads/:id/restore` | Clears the revert. 409 when another active upload has the same SHA-256. 200 `{upload}` |
| `POST /api/uploads/:id/replace` | JSON `{on: bool}` sets `replace_stored`. 200 `{upload}` |

Upload summary: `{id, filename, size, uploaded_at, uploaded_by, players, shots, first_shot,
last_shot, replace_stored, reverted_at, reverted_by, changed_version, result, pending}` where
`players` and `result` are parsed JSON and `pending` follows the ledger rules.

Upload multipart fields: `file` (the original bytes), `encoding` (must be `identity`),
`sha256` (hex of the file), `size` (byte count), `filename`, `players` (JSON array of
strings), `shots`, `first_shot`, `last_shot`. Limits: 1 to 16,000,000 bytes; filename up to
200 characters; at most 50 players of up to 100 characters each. The site hashes the file
and answers 400 when it does not match `sha256`, so a user cannot claim another file's hash.
The duplicate check runs on `sha256` against active uploads. When the duplicate is not
visible to the user, the answer is `{upload: null, duplicate: true}`.

Hashing and reading the form cost about 7 ms of CPU per MB. That is an observation from
2026-10-04, timing WebCrypto SHA-256 and `Response.formData()` in Node 24 on the dev box.
A 20-shot export is about 15 KB. A file above about 1 MB can exceed the free plan's 10 ms
limit, and then the request fails and nothing is stored. For a large library export, use
`bin/golf ingest` and `bin/golf sync` instead.

`/api/dashboard/html` carries its own CSP: `default-src 'none'; script-src 'unsafe-inline';
style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none';
frame-ancestors 'self'`. The page shell loads it in `<iframe sandbox="allow-scripts">`, so
the dashboard runs in an opaque origin with no access to the cookie or the API.

## Box API (bearer token)

| Route | Behaviour |
|---|---|
| `GET /api/sync/state` | One D1 batch: `{ledger_version, uploads, users}`. `uploads` items: `{id, uid, sha256, filename, size, encoding, uploaded_at, uploaded_by, replace_stored, reverted_at, changed_version}`. `users` items: `{username, display_name, players}` |
| `GET /api/sync/uploads/:id/raw` | Stored bytes as `application/octet-stream`, header `X-Encoding: gzip` or `identity`, no `Content-Encoding` |
| `POST /api/sync/uploads` | JSON `{filename, sha256, size, encoding, data_b64, uploaded_at, players, shots, first_shot, last_shot, replace_stored}`. `uploaded_by` is `box`. `replace_stored` is optional and applies only when the upload is created. `players` may hold up to 200 names. Duplicate check runs against every upload with that SHA-256, active or not, and returns the newest: 200 `{upload, duplicate: true}`. Otherwise 201 `{upload, duplicate: false}` |
| `POST /api/sync/publish` | JSON `{ledger_version, results, dashboards}`. `results`: `[{id, result}]`, also copies `result.players` and `result.shots_in_file` into the row and rewrites the upload's `upload_players` rows, only when the row's `result_version` is NULL or not above the incoming `ledger_version`. The box may send up to 200 player names, because it adds alias targets. `dashboards`: `[{username, sessions, html_gz_b64}]`, where `html_gz_b64` is null when `sessions` is 0. An analysis row is written only when the incoming `ledger_version` is not lower than the stored `based_on_version`. 400 when `ledger_version` is newer than the site's. 200 `{published: <count>}` |
| `GET /api/sync/users` | `{users: [{username, display_name, players, created_at}]}` |
| `PUT /api/sync/users/:username` | JSON with any of `display_name`, `players`, `key_hash`. Creating a user requires `key_hash`, and `display_name` defaults to the username. A new `key_hash` deletes that user's sessions. 200 `{user}` |
| `DELETE /api/sync/users/:username` | Deletes the user, their sessions and their analysis. 204, or 404 |

A publish may carry any subset of results and dashboards. The box sends results in batches
of 100 and each dashboard in its own request, so no single request grows with the number of
users or uploads.

A wrong method on a known route answers 405 with an `Allow` header.

`result` object, written by the box: `{ok, error, shots_in_file, shots_used, conflicts,
players, warnings}`. `shots_used` counts the shots this upload wins. `conflicts` counts its
shots whose winner is another upload with different values. `warnings` holds at most 10
parser warnings.

## Pages

- `/login`: username, password, and "Keep me signed in on this device", unchecked. On a
  shared sim PC, leave it unchecked and sign out when done.
- `/`: header with the user's name, an Upload button (several files at once), Uploads, Sign
  out. Below it, the analysis time and the pending uploads, each with Show shots and Undo.
  Undo reverts an active upload and restores a reverted one.
  Then the dashboard iframe. With no analysis yet, a sentence says the uploads are saved and
  the stats appear after the next analysis run.
- `/uploads`: every visible upload with time, file, uploader, players, shots, state, the box's
  result, a toggle between keeping stored values and using this file's values when the result
  has conflicts, Revert or Restore, and Download.

Before sending a file, the browser hashes it and runs the display parse. It sends the file
uncompressed, so the site can check the hash. A file the display parse cannot read as a
TrackMan export with at least one shot is not sent. A file whose players share no name with the user's players asks
for confirmation first: "This file has shots for Christian, not you. Upload anyway?".

Display parse, for preview and the upload fields only: strip a BOM; a first line starting with
`sep=` names the delimiter, otherwise `;` when the first line has more `;` than `,`; RFC 4180
quoting; the header is the first of the first 10 rows that has a `Club` column and a
`Ball Speed` or `Club Speed` column, compared trimmed, case-insensitive, without a `[unit]`
suffix; the next row is the units row when every non-empty cell is `[...]`; a shot is a later
row with a non-empty `Date` and `Club`. The preview shows the file's own values with its own
units and rounds numbers for display only. It computes no statistic.

Static pages get their headers from `web/public/_headers`: a CSP of `default-src 'self';
script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src
'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'`, plus
`X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.

## Box side

Local store, `src/golfstats/store.py`:

- `uploads(id, sha256, filename, uploaded_at, uploaded_by, raw BLOB, replace_stored,
  reverted_at, site_id UNIQUE, error, shots_in_file, source_units, unmapped, warnings)` holds
  every file byte for byte. `data/raw/` is no longer written.
- `upload_shots` holds each upload's parsed shots, keyed by `(upload_id, shot_key)`. Within
  one file the first row for a key wins.
- `shots` is a view that applies the winner rule over active uploads. It exposes the old
  `shots` columns, with `upload_id` in place of `import_id`. Its `id` is the `upload_shots`
  rowid.
- A file that fails to parse is still stored, with `error` set and no shots.
- `bin/golf ingest --replace` sets `replace_stored` only when the file has conflicts. On a
  file that is already marked it changes nothing. To make it win over a later upload that
  also replaces, revert the later one. On a file that is already on the site it changes
  nothing and points to the site's Uploads page, because sync takes the site's choice. The
  file stays in the inbox, and the command exits 1.
- When a download does not match the site's SHA-256, a new row is stored with `error` set
  and no shots. A known row keeps its old bytes and gets `error` and no shots. The next sync
  downloads it again.
- A local upload keeps a local time with no zone and `uploaded_by = 'cli'`. Sync sends that
  time as UTC, and the site records the upload as `box`. A mirrored upload keeps the site's
  time and uploader.
- A database in the old layout (`imports` plus a `shots` table) migrates on first open. The
  migration copies `golf.db` to `golf.db.bak-YYYYmmdd-HHMMSS` with the SQLite backup API
  first. It reads each import's archived file, marks an import `replace_stored` when it owns
  a stored shot that an earlier import's file also holds, and checks that the view equals
  the old table before it drops the old tables, in one transaction. It stops and keeps the
  old tables when an archived file is missing, does not match its SHA-256, or no longer
  parses.

`bin/golf sync`, in order:

1. Push each active local upload with no `site_id` and no parse error to
   `POST /api/sync/uploads`, and record the id. `players` is sent with the alias targets
   added. When the site answers with a duplicate that is already mirrored here, and the
   mirrored copy parses and its bytes hash to the same SHA-256, the local copy is deleted.
   Otherwise the local copy is kept and reported. This also happens when the site copy is
   reverted, because the duplicate check includes reverted uploads. To bring such a file
   back, restore it on the site.
2. Read `GET /api/sync/state`.
3. Mirror each site upload: download new ones, check the SHA-256, store them with their site
   id, uploader, time and state; copy `reverted_at` and `replace_stored` onto known ones. The
   site's state wins. A gzip download is decompressed to at most `size + 1` bytes, so an
   oversized one fails the hash check instead of filling memory.
4. Publish a result for every site upload, and one dashboard per site user, built from the
   shots of that user's players with the player renamed to the user's display name, at the
   `ledger_version` read in step 2. `[player.aliases]` in `config.toml` merges player names
   before the user's players are matched, as it does for the local dashboard. Each result's
   `players` lists the file's names plus their alias targets, so the site shows an upload
   to the user whose dashboard uses it. When an active upload failed its hash check in step
   3, the results are published and the dashboards are not, because they would miss that
   file. Reverting it on the site, or a later good download, unblocks them.

Site settings live in `data/site.json` as `{url, token}` with mode 0600, written by
`bin/golf site`. The URL must be `https://`, or `http://` to `localhost` or `127.0.0.1`.
