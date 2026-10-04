from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .config import Config, load_config
from .dashboard import write_dashboards
from .focus import grade_plan, make_plan, pick_focus, plan_for, save_plan
from .report import bound_text, render_report, unit_suffix
from .stats import Session, split_sessions
from .store import connect, ingest_file, list_uploads, load_shots, merge_aliases
from .synth import demo_exports
from .sync import SiteError, key_hash, load_site, login_key, save_site, sync


def _sessions(cfg: Config) -> list[Session]:
    conn = connect(cfg.db_path)
    shots = load_shots(conn)
    conn.close()
    shots, conflicts = merge_aliases(shots, cfg.aliases)
    if conflicts:
        print(f"{len(conflicts)} shot(s) are stored under two names that [player.aliases] merges, with different "
              f"values. Kept the copy imported first. First: {conflicts[0]}")
    if cfg.player:
        shots = [s for s in shots if (s.get("player") or "").lower() == cfg.player.lower()]
    return split_sessions(shots, cfg.gap_minutes)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _write_report(session: Session, sessions: list[Session], cfg: Config, save: bool) -> tuple[Path, dict | None]:
    focus = pick_focus(session, cfg, sessions)
    plan = plan_for(session, sessions, cfg)
    grades = grade_plan(plan, session) if plan else None
    plan_path = save_plan(make_plan(session, focus, cfg), cfg.plans) if (focus and save) else None
    text = render_report(session, sessions, focus, grades, _rel(plan_path) if plan_path else None, cfg)
    cfg.reports.mkdir(parents=True, exist_ok=True)
    path = cfg.reports / f"{session.id}.md"
    path.write_text(text + "\n")
    return path, focus


def _focus_line(focus: dict | None) -> str:
    if not focus:
        return "Next focus: none yet (not enough shots with one club)."
    lo, hi = focus["window"]
    t = focus["today"]
    unit = unit_suffix(focus["unit"])
    window = f"at least {bound_text(lo)}{unit}" if hi >= 200 else f"{lo:+g}{unit} to {hi:+g}{unit}"
    return (f"Next focus: {focus['club']} {focus['label'].lower()} {window}"
            f" (today {t['hits']} of {t['n']} in window).")


def _print_dashboards(sessions: list[Session], cfg: Config) -> None:
    for player, path in write_dashboards(sessions, cfg):
        print(f"Dashboard{f' ({player})' if player else ''}: {_rel(path)}")


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
    new_uploads: set[int] = set()
    for path in paths:
        try:
            res = ingest_file(conn, path, replace=args.replace)
        except OSError as exc:
            print(f"{path.name}: not imported. {exc}")
            failed += 1
            continue
        if res.error is not None:
            print(f"{path.name}: not imported. {res.error}")
            failed += 1
            continue
        if res.already_imported:
            print(f"{path.name}: already imported, skipped.")
        else:
            print(f"{path.name}: {res.shots_in_file} shots, {res.shots_added} new. Stored as upload {res.upload_id}.")
            units = ", ".join(f"{k} [{v}]" for k, v in res.source_units.items()
                              if k in ("club_speed", "carry", "curve", "low_point", "impact_offset"))
            print(f"  units: {units}")
            if res.unmapped:
                print(f"  columns not used yet: {', '.join(res.unmapped)}")
            for w in res.warnings[:10]:
                print(f"  warning: {w}")
            if res.conflicts and args.replace:
                print(f"  replaced {res.replaced} stored shot(s) with this file's values.")
            if (res.shots_added or res.replaced) and res.upload_id is not None:
                new_uploads.add(res.upload_id)
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
    touched = [s for s in sessions if any(sh["upload_id"] in new_uploads for sh in s.shots)]
    for sess in touched:
        path, focus = _write_report(sess, sessions, cfg, save=True)
        who = f" ({sess.player})" if sess.player else ""
        print(f"Session {sess.id}{who}: {len(sess.counted)} counted shots. Report {_rel(path)}")
        print(f"  {_focus_line(focus)}")
    touched_ids = {s.id for s in touched}
    later = [s for s in sessions
             if s.id not in touched_ids and any(s.player == t.player and s.start > t.start for t in touched)]
    for sess in later:
        _write_report(sess, sessions, cfg, save=False)
    if later:
        print(f"Refreshed {len(later)} later report(s) whose comparisons or plan grades depend on these sessions.")
    _print_dashboards(sessions, cfg)
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
    if not sessions:
        print("No sessions yet. Run `bin/golf ingest` first.")
        return 1
    _print_dashboards(sessions, cfg)
    return 0


def cmd_demo(args: argparse.Namespace, cfg: Config) -> int:
    demo = load_config(data_dir=cfg.data_dir / "demo")
    if demo.data_dir.exists():
        for p in [demo.db_path, demo.db_path.with_name("golf.db-wal"), demo.db_path.with_name("golf.db-shm"),
                  *demo.plans.glob("*.json"), *demo.reports.glob("*.md")]:
            p.unlink(missing_ok=True)
    demo.inbox.mkdir(parents=True, exist_ok=True)
    for name, text in demo_exports():
        (demo.inbox / name).write_text(text)
        cmd_ingest(argparse.Namespace(files=[], replace=False), demo)
    return 0


def cmd_uploads(args: argparse.Namespace, cfg: Config) -> int:
    conn = connect(cfg.db_path)
    uploads = list_uploads(conn)
    conn.close()
    if not uploads:
        print("No uploads yet.")
        return 0
    for u in uploads:
        state = "reverted" if u["reverted_at"] else "active"
        if u["replace_stored"]:
            state += ", replaces stored"
        site = f"site {u['site_id']}" if u["site_id"] is not None else "not on site"
        line = (f"{u['id']:4d}  {u['uploaded_at']}  {u['filename']}  by {u['uploaded_by']}  {site}  {state}  "
                f"{u['shots_used']} of {u['shots_in_file']} shots used")
        print(line + (f"  error: {u['error']}" if u["error"] else ""))
    return 0


def _read_secret(args: argparse.Namespace, flag: str, prompt: str, confirm: bool) -> str:
    if getattr(args, flag):
        value = sys.stdin.readline().rstrip("\r\n")
    else:
        value = getpass.getpass(prompt)
        if confirm and getpass.getpass("Again: ") != value:
            raise ValueError("the two entries do not match")
    if not value:
        raise ValueError("it cannot be empty")
    return value


def cmd_site(args: argparse.Namespace, cfg: Config) -> int:
    try:
        token = _read_secret(args, "token_stdin", "Sync token: ", confirm=False)
        save_site(cfg.site_path, args.url, token)
    except ValueError as exc:
        print(f"Site settings not saved: {exc}.")
        return 1
    print(f"Saved site settings to {_rel(cfg.site_path)} (mode 0600).")
    return 0


def cmd_sync(args: argparse.Namespace, cfg: Config) -> int:
    try:
        report = sync(cfg, load_site(cfg.site_path))
    except (SiteError, FileNotFoundError, ValueError) as exc:
        print(f"Sync failed: {exc}")
        return 1
    print(f"Pushed {report.pushed}, merged {report.merged}, pulled {report.pulled}"
          + (f", downloaded again {report.redownloaded}" if report.redownloaded else "")
          + f", state changes {report.state_changes}.")
    for user, n in report.published.items():
        print(f"  {user}: {n} session(s) published.")
    print(f"Ledger version {report.ledger_version}.")
    for err in report.errors:
        print(f"  upload error: {err}")
    return 1 if report.errors else 0


def _password_hash(args: argparse.Namespace) -> str:
    password = _read_secret(args, "password_stdin", f"Password for {args.name}: ", confirm=True)
    return key_hash(login_key(args.name, password))


def cmd_user(args: argparse.Namespace, cfg: Config) -> int:
    try:
        client = load_site(cfg.site_path)
        if args.action == "list":
            for u in client.users():
                print(f"{u['username']}  {u['display_name']}  players: {', '.join(u['players']) or '(none)'}")
            return 0
        if args.action == "remove":
            client.delete_user(args.name)
            print(f"Removed {args.name}.")
            return 0
        if args.action == "add":
            fields = {"display_name": args.display or args.name, "players": args.player,
                      "key_hash": _password_hash(args)}
        elif args.action == "passwd":
            fields = {"key_hash": _password_hash(args)}
        else:
            fields = {"players": args.players}
        user = client.put_user(args.name, fields)
    except (SiteError, FileNotFoundError, ValueError) as exc:
        print(f"User not changed: {exc}")
        return 1
    print(f"Saved {user['username']} ({user['display_name']}), players: {', '.join(user['players']) or '(none)'}.")
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
    sub.add_parser("dashboard", help="rebuild data/dashboard-<player>.html, one per player").set_defaults(
        func=cmd_dashboard)
    sub.add_parser("demo", help="build a dashboard from synthetic sessions in data/demo").set_defaults(func=cmd_demo)
    sub.add_parser("uploads", help="list stored uploads").set_defaults(func=cmd_uploads)
    p = sub.add_parser("site", help="save the upload site's URL and sync token in data/site.json")
    p.add_argument("--url", required=True)
    p.add_argument("--token-stdin", action="store_true", help="read the token from one line of stdin")
    p.set_defaults(func=cmd_site)
    sub.add_parser("sync", help="push local uploads, pull the site's, publish dashboards").set_defaults(
        func=cmd_sync)
    p = sub.add_parser("user", help="manage site users")
    users = p.add_subparsers(dest="action", required=True)
    u = users.add_parser("add", help="create a user")
    u.add_argument("name")
    u.add_argument("--player", action="append", required=True, help="a player name in the exports (repeatable)")
    u.add_argument("--display", help="display name (default: the username)")
    u.add_argument("--password-stdin", action="store_true", help="read the password from one line of stdin")
    u = users.add_parser("passwd", help="set a new password")
    u.add_argument("name")
    u.add_argument("--password-stdin", action="store_true", help="read the password from one line of stdin")
    u = users.add_parser("players", help="set the players a user sees")
    u.add_argument("name")
    u.add_argument("players", nargs="+")
    users.add_parser("list", help="list users")
    u = users.add_parser("remove", help="delete a user")
    u.add_argument("name")
    p.set_defaults(func=cmd_user)
    args = parser.parse_args(argv)
    return args.func(args, load_config(args.config))


if __name__ == "__main__":
    sys.exit(main())
