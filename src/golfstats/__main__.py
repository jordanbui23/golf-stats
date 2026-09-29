from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, load_config
from .dashboard import write_dashboard
from .focus import grade_plan, make_plan, pick_focus, plan_for, save_plan
from .report import render_report
from .stats import Session, split_sessions
from .store import connect, ingest_file, load_shots
from .synth import demo_exports
from .tps_csv import ParseError


def _sessions(cfg: Config) -> list[Session]:
    conn = connect(cfg.db_path)
    shots = load_shots(conn)
    conn.close()
    if cfg.player:
        shots = [s for s in shots if (s.get("player") or "").lower() == cfg.player.lower()]
    return split_sessions(shots, cfg.gap_minutes)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _write_report(session: Session, sessions: list[Session], cfg: Config, save: bool) -> tuple[Path, dict | None]:
    focus = pick_focus(session, cfg)
    plan = plan_for(session, sessions, cfg.plans)
    grades = grade_plan(plan, session) if plan else None
    plan_path = save_plan(make_plan(session, focus, cfg), cfg.plans) if (focus and save) else None
    text = render_report(session, sessions, focus, grades, _rel(plan_path) if plan_path else None)
    cfg.reports.mkdir(parents=True, exist_ok=True)
    path = cfg.reports / f"{session.id}.md"
    path.write_text(text + "\n")
    return path, focus


def _focus_line(focus: dict | None) -> str:
    if not focus:
        return "Next focus: none yet (not enough shots with one club)."
    lo, hi = focus["window"]
    t = focus["today"]
    window = f">= {lo:g}" if hi >= 200 else f"{lo:+g} to {hi:+g}"
    return (f"Next focus: {focus['club']} {focus['label'].lower()} {window} {focus['unit']}"
            f" (today {t['hits']} of {t['n']} in window).")


def in_inbox(path: Path, inbox: Path) -> bool:
    return path.absolute().parent.resolve() == inbox.resolve()


def cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    paths = [Path(p) for p in args.files] or sorted(p for p in cfg.inbox.iterdir() if p.suffix.lower() == ".csv")
    if not paths:
        print(f"No CSV files given and none in {_rel(cfg.inbox)}.")
        return 1
    conn = connect(cfg.db_path)
    failed = 0
    new_imports: set[int] = set()
    for path in paths:
        try:
            res = ingest_file(conn, path, cfg.archive, replace=args.replace)
        except (ParseError, UnicodeDecodeError, OSError) as exc:
            print(f"{path.name}: not imported. {exc}")
            failed += 1
            continue
        if res.already_imported:
            print(f"{path.name}: already imported, skipped.")
        else:
            archived = _rel(res.archived_path) if res.archived_path else "?"
            print(f"{path.name}: {res.shots_in_file} shots, {res.shots_added} new. Archived to {archived}.")
            units = ", ".join(f"{k} [{v}]" for k, v in res.source_units.items()
                              if k in ("club_speed", "carry", "curve", "low_point", "impact_offset"))
            print(f"  units: {units}")
            if res.unmapped:
                print(f"  columns not used yet: {', '.join(res.unmapped)}")
            for w in res.warnings[:10]:
                print(f"  warning: {w}")
            if res.conflicts and args.replace:
                print(f"  replaced {res.replaced} stored shot(s) with this file's values.")
            if (res.shots_added or res.replaced) and res.import_id is not None:
                new_imports.add(res.import_id)
        if res.conflicts and not args.replace:
            where = "it stays in the inbox, so run `bin/golf ingest --replace`" if in_inbox(path, cfg.inbox) \
                else f"run `bin/golf ingest --replace {path}`"
            print(f"  {len(res.conflicts)} shot(s) are already stored with different values (for example a "
                  f"normalized export). Kept the stored values. To use this file's values, {where}. "
                  f"First: {res.conflicts[0]}")
        if in_inbox(path, cfg.inbox) and not (res.conflicts and not args.replace):
            path.unlink()
    conn.close()

    sessions = _sessions(cfg)
    touched = [s for s in sessions if any(sh["import_id"] in new_imports for sh in s.shots)]
    for sess in touched:
        path, focus = _write_report(sess, sessions, cfg, save=True)
        print(f"Session {sess.id}: {len(sess.counted)} counted shots. Report {_rel(path)}")
    touched_ids = {s.id for s in touched}
    later = [s for s in sessions
             if s.id not in touched_ids and any(s.player == t.player and s.start > t.start for t in touched)]
    for sess in later:
        _write_report(sess, sessions, cfg, save=False)
    if later:
        print(f"Refreshed {len(later)} later report(s) whose comparisons or plan grades depend on these sessions.")
    if touched:
        print(_focus_line(pick_focus(touched[-1], cfg)))
    if sessions:
        print(f"Dashboard: {_rel(write_dashboard(sessions, cfg))}")
    return 1 if failed else 0


def cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    sessions = _sessions(cfg)
    if not sessions:
        print("No sessions yet. Run `bin/golf ingest` first.")
        return 1
    target = sessions[-1] if args.session in (None, "latest") else next((s for s in sessions if s.id == args.session), None)
    if target is None:
        print(f"No session {args.session!r}. Try `bin/golf sessions`.")
        return 1
    path, _ = _write_report(target, sessions, cfg, save=False)
    print(path.read_text())
    return 0


def cmd_sessions(args: argparse.Namespace, cfg: Config) -> int:
    for s in _sessions(cfg):
        clubs = sorted({x["club_code"] for x in s.counted})
        print(f"{s.id}  {len(s.counted):3d} shots  {', '.join(clubs)}" + (f"  ({s.player})" if s.player else ""))
    return 0


def cmd_dashboard(args: argparse.Namespace, cfg: Config) -> int:
    sessions = _sessions(cfg)
    print(f"Dashboard: {_rel(write_dashboard(sessions, cfg))} ({len(sessions)} sessions)")
    return 0


def cmd_demo(args: argparse.Namespace, cfg: Config) -> int:
    demo = load_config(data_dir=cfg.data_dir / "demo")
    if demo.data_dir.exists():
        for p in [demo.db_path, *demo.plans.glob("*.json"), *demo.reports.glob("*.md"), *demo.archive.glob("*.csv")]:
            p.unlink(missing_ok=True)
    demo.inbox.mkdir(parents=True, exist_ok=True)
    for name, text in demo_exports():
        (demo.inbox / name).write_text(text)
        cmd_ingest(argparse.Namespace(files=[], replace=False), demo)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golf", description=__doc__)
    parser.add_argument("--config", type=Path, help="config file (default: config.toml in the repo)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="import TrackMan CSV exports (default: everything in data/inbox)")
    p.add_argument("files", nargs="*")
    p.add_argument("--replace", action="store_true",
                   help="overwrite stored shots whose values differ from this file's")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("report", help="print the report for a session (default: latest)")
    p.add_argument("session", nargs="?")
    p.set_defaults(func=cmd_report)
    sub.add_parser("sessions", help="list sessions").set_defaults(func=cmd_sessions)
    sub.add_parser("dashboard", help="rebuild data/dashboard.html").set_defaults(func=cmd_dashboard)
    sub.add_parser("demo", help="build a dashboard from synthetic sessions in data/demo").set_defaults(func=cmd_demo)
    args = parser.parse_args(argv)
    return args.func(args, load_config(args.config))


if __name__ == "__main__":
    sys.exit(main())
