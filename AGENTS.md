# golf-stats

Personal project under `~/projects`. Read `README.md` for the architecture and
`docs/TRACKMAN.md` for what the TrackMan export looks like.

- **Auto-commit scoped changes without asking, then stop.** Inspect `git status`/`diff`/history
  first and stage only the change's files. Do not push. Never force-push.
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

@../AGENTS.md
