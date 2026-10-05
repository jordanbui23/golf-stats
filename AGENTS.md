# golf-stats

Personal project under `~/projects`. Read `README.md` for the architecture and
`docs/TRACKMAN.md` for what the TrackMan export looks like.

- **Auto-commit and auto-push scoped changes without asking.** Inspect `git status`/`diff`/history
  first and stage only the change's files. Push after each commit, following the pre-push file
  check in `~/projects/AGENTS.md`: this remote is PUBLIC. Never force-push.
- **Adversarial review before calling a code change done.** Run `/xreview` on the diff of the
  unit of work. A high or critical finding is a hard gate.
- **Never commit shot data.** `data/` is gitignored: exports, the database, reports and plans.
  Tests use `src/golfstats/synth.py`, never a real export.
- **`docs/TRACKMAN.md` is the spec of record for the export format and sign conventions.**
  Every load-bearing claim carries `confirmed`, `supported` or `unverified`. When a real export
  contradicts it, fix the doc and the parser together.
- **Numbers stay in code.** The focus, windows and grades are computed deterministically. Prose
  explains them and never invents a figure.
- **Gitignore `.opencode/` and `.omo/`.** Already done.
- **Keep `README.md` current** on structural changes.
- **A push does not update the upload site. Publish it yourself after the push, without asking.**
  The `jbui-golf` Pages project is not connected to Git, and dashboards are HTML rendered on this
  box from `data/golf.db`, which never reaches the repo.
  - A change under `src/golfstats/` or to `config.toml` that alters what a dashboard or an upload
    result shows: run `bin/golf sync`, then report the sessions it published per user.
  - A change under `web/`: deploy with `cd web && npx wrangler pages deploy public`, and apply any
    new migration with `npx wrangler d1 migrations apply golf-stats --remote` first.

@../AGENTS.md
