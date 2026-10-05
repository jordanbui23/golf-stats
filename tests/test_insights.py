import json
import subprocess
from pathlib import Path
from datetime import timedelta

import pytest

from golfstats import insights as ins
from golfstats.__main__ import main
from golfstats.config import Config, load_config
from golfstats.store import connect, latest_insight, unpublished_insights
from golfstats.sync import save_site, sync
from test_store_cli import START, write_export
from test_sync import FakeSite

REPORT = ("DR carry 217 ±26, smash 1.215, start -0.6 ±5.7. 6 of 16 shots. 7i and 3W and 52° wedge. 85% of best. "
          "Spin 3755 rpm. Window between -2° and +2°. Session 2026-10-02-1318.")


def drill(name="Start-line gate", setup="Two tees about four feet ahead on the target line.",
          reps="Driver, three sets of five.", pass_="Four of five start through the gate."):
    return {"name": name, "setup": setup, "reps": reps, "pass": pass_}


def item(title="Fix the start line", why="Most drives started outside the window.", target="More drives inside the "
         "window.", **kw):
    return {"title": title, "why": why, "drill": drill(**kw), "target": target}


def reply(*items, summary="Driver start line first.", **extra) -> str:
    return json.dumps({"summary": summary, "items": list(items) or [item(), item(), item()], **extra})


def test_a_number_copied_or_rounded_from_the_report_is_allowed():
    allowed = ins.allowed_numbers(REPORT)
    body = {"summary": "Carry 217, smash 1.2 and 1.22, start 0.6 left or -0.6, spread 6, 85% of best.",
            "items": [item(why="7i and 3W and 52° wedge."), item(why="Spin 3,755 rpm, from -2° to +2° or 2°.")]}
    assert ins.invented_numbers(body, allowed) == []


def test_an_invented_or_computed_number_is_caught():
    allowed = ins.allowed_numbers(REPORT)
    body = {"summary": "Only 38% started in the window, 11 shots out, carry 220.",
            "items": [item(target="Hit 12 of 16."), item(why="Smash 1.21 is low.")]}
    assert ins.invented_numbers(body, allowed) == ["38", "11", "220", "12", "1.21"]


def test_a_flipped_sign_an_exponent_or_a_hyphenated_number_is_caught():
    allowed = ins.allowed_numbers(REPORT)
    body = {"summary": "Start +0.6, spread -5.7, carry 2.17e2, gap 6-12 balls, and −26.",
            "items": [item(why="Smash 1.215 (from 1.215), carry +217.")]}
    assert ins.invented_numbers(body, allowed) == ["+0.6", "-5.7", "2.17e2", "12", "−26", "+217"]


def test_numbers_in_the_drill_and_the_setup_fix_are_checked_too():
    allowed = ins.allowed_numbers(REPORT)
    body = {"summary": "Start line.", "before": "Turn OERT on 30 times.",
            "items": [item(setup="Tees 9 feet ahead."), item(reps="Driver, 4 sets of 6."), item(name="The 50 ball drill")]}
    assert ins.invented_numbers(body, allowed) == ["30", "9", "4", "50"]


def test_number_words_are_refused_outside_the_drill():
    body = {"summary": "Six of sixteen in the window.", "before": "Hit two warm-up balls.",
            "items": [item(why="Twenty-five yds short, half of them left."), item(target="One more DR in window."),
                      item(title="Someone fix this", why="Twice as wide as a quarter of the 7i."),
                      item(why="A third of your drives, two fifths of the 9i. The third shot was fine.")]}
    assert ins.number_words(body) == ["six", "sixteen", "two", "twenty", "five", "half", "one", "twice", "quarter",
                                      "third", "fifths"]
    assert ins.number_words({"summary": "s", "items": [item(), item(), item()]}) == []


def test_a_number_word_gets_the_retry_and_a_clean_reply_is_kept(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path / "data")
    sessions = sessions_for(tmp_path, cfg)
    calls = []

    def ask(_cfg, _system, messages):
        calls.append(messages)
        return reply(item(why="Six of them."), item(), item()) if len(calls) == 1 else reply()

    rec = ins.generate(sessions[-1], sessions, cfg, ask=ask)
    assert len(calls) == 2 and "number words: six" in calls[1][-1]["content"][0]["text"]
    assert rec["body"]["items"][0]["drill"]["reps"] == "Driver, three sets of five."


def test_a_number_glued_to_a_unit_or_a_club_is_still_checked():
    allowed = ins.allowed_numbers("7i carry 140")
    body = {"summary": "7i carry 140.", "items": [item(why="It flew 150yds and the 9i was fine.")]}
    assert ins.invented_numbers(body, allowed) == ["150", "9"]


def test_a_reply_in_a_code_fence_parses_and_is_trimmed():
    body, problems = ins.parse_reply("```json\n" + reply(item(title="  Fix it  "), item(), item()) + "\n```")
    assert problems == [] and body is not None
    assert body["items"][0]["title"] == "Fix it" and len(body["items"]) == 3 and "before" not in body
    assert body["items"][0]["drill"] == drill()


def test_a_setup_fix_is_kept_when_given():
    body, problems = ins.parse_reply(reply(item(), item(), item(), before="  Turn OERT on.  "))
    assert problems == [] and body is not None and body["before"] == "Turn OERT on."


@pytest.mark.parametrize("text,problem", [
    ("no json here", "holds no JSON object"),
    ("{not json}", "not valid JSON"),
    (reply(item(), item()), "3 to 5 objects"),
    (reply(item(), item(), item(title="x" * 51)), "item 3 title is longer than 50"),
    (reply(item(), item(), {**item(), "drill": "Hit ten balls."}), "item 3 drill must be an object"),
    (reply(item(), item(), item(pass_="")), "item 3 drill.pass must be a sentence"),
    (reply(item(), item(), {**item(), "drill": {k: v for k, v in drill().items() if k != "reps"}}),
     "item 3 drill.reps must be a sentence"),
    (reply(item(), item(), item(name="n" * 41)), "item 3 drill.name is longer than 40"),
    (reply(item(), item(), item(), before="b" * 161), "before is longer than 160"),
    (reply(item(), item(), item(), before=3), "before must be text"),
    (reply(item(), item(), item(), summary="s" * 141), "summary is longer than 140"),
    (reply(item(), item(), item(why="two\nlines")), "item 3 why holds a line break"),
    (reply(item(), item(), item(), summary="tab\there"), "summary holds a line break"),
])
def test_a_reply_with_the_wrong_shape_is_refused(text, problem):
    body, problems = ins.parse_reply(text)
    assert body is None and any(problem in p for p in problems)


def sessions_for(tmp_path, cfg: Config):
    write_export(cfg.inbox, "a.csv", player="Jordan", plan=[("7 Iron", 24)])
    write_export(cfg.inbox, "b.csv", START + timedelta(days=1), seed=2, player="Jordan", plan=[("7 Iron", 24)])
    from golfstats.__main__ import _sessions, cmd_ingest
    import argparse
    cmd_ingest(argparse.Namespace(files=[], replace=False), cfg)
    return _sessions(cfg)


def test_the_facts_are_the_report_plus_the_plan(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path / "data")
    sessions = sessions_for(tmp_path, cfg)
    text = ins.facts(sessions[-1], sessions, cfg)
    assert text.startswith(f"# Session {sessions[-1].id}") and "## Strike" in text
    assert f"## Plan for next session\n\n{cfg.plan_shots} shots with the 7i" in text


def test_an_invented_number_gets_one_retry_with_the_numbers_named(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path / "data")
    sessions = sessions_for(tmp_path, cfg)
    calls = []

    def ask(_cfg, system, messages):
        calls.append(messages)
        if len(calls) == 1:
            return reply(item(why="You hit 987654 of them."), item(), item())
        return reply(item(why="Most of them."), item(why="Most."), item(why="Most."))

    rec = ins.generate(sessions[-1], sessions, cfg, ask=ask)
    assert len(calls) == 2 and "987654" in calls[1][-1]["content"][0]["text"]
    assert calls[1][1]["role"] == "assistant" and rec["body"]["items"][0]["why"] == "Most of them."
    assert rec["session_id"] == sessions[-1].id and rec["player"] == "Jordan" and len(rec["uid"]) == 32
    assert rec["created_at"].endswith("Z") and rec["report"].startswith("# Session")


def test_two_bad_replies_store_nothing_and_raise(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path / "data")
    sessions = sessions_for(tmp_path, cfg)
    with pytest.raises(ins.InsightError, match="failed the checks twice.*987654"):
        ins.generate(sessions[-1], sessions, cfg, ask=lambda *_: reply(item(why="987654"), item(), item()))


class Proc:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def converse(text="hi", stop="end_turn"):
    return json.dumps({"stopReason": stop, "output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": "thinking"}}}, {"text": text}]}}})


def test_bedrock_goes_through_the_aws_cli_with_the_configured_profile(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return Proc(out=converse("the reply"))

    monkeypatch.setattr(ins.shutil, "which", lambda _name: "/usr/bin/aws")
    monkeypatch.setattr(ins.subprocess, "run", run)
    cfg = Config(data_dir=Path("data"), insights_model="m-1", insights_profile="golf", insights_region="us-west-2")
    assert ins.bedrock(cfg, "sys", [{"role": "user", "content": [{"text": "q"}]}]) == "the reply"
    cmd = seen["cmd"]
    assert cmd[:3] == ["/usr/bin/aws", "bedrock-runtime", "converse"] and "--profile" in cmd and "golf" in cmd
    payload = json.loads(cmd[cmd.index("--cli-input-json") + 1])
    assert payload["modelId"] == "m-1" and payload["system"] == [{"text": "sys"}]
    assert "shell" not in seen["kw"] and seen["kw"]["timeout"] == ins.TIMEOUT


@pytest.mark.parametrize("proc,match", [
    (Proc(code=255, err="An error occurred (ExpiredTokenException)\n"), "ExpiredTokenException"),
    (Proc(out=converse(stop="max_tokens")), "ran out of its"),
    (Proc(out="not json"), "other than a Converse reply"),
])
def test_a_failed_bedrock_call_is_an_insight_error(monkeypatch, proc, match):
    monkeypatch.setattr(ins.shutil, "which", lambda _name: "/usr/bin/aws")
    monkeypatch.setattr(ins.subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(ins.InsightError, match=match):
        ins.bedrock(Config(data_dir=Path("data")), "s", [])


def test_a_bedrock_timeout_is_an_insight_error(monkeypatch):
    def run(*_a, **_k):
        raise subprocess.TimeoutExpired("aws", 1)

    monkeypatch.setattr(ins.shutil, "which", lambda _name: "/usr/bin/aws")
    monkeypatch.setattr(ins.subprocess, "run", run)
    with pytest.raises(ins.InsightError, match="did not answer"):
        ins.bedrock(Config(data_dir=Path("data")), "s", [])


@pytest.fixture
def model(monkeypatch):
    from golfstats import __main__ as cli
    calls = []

    def fake_generate(session, sessions, cfg):
        def ask(_cfg, _system, messages):
            calls.append(session.id)
            return reply(item(title=f"Work on {session.player}"), item(), item())
        return ins.generate(session, sessions, cfg, ask=ask)

    monkeypatch.setattr(cli, "generate", fake_generate)
    return calls


def test_cli_without_a_site_analyses_each_player_once(cfg_file, model, capsys):
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "a.csv", player="Jordan")
    write_export(cfg.inbox, "b.csv", START + timedelta(hours=1), seed=3, player="Christian")
    assert main(["--config", str(cfg_file), "ingest"]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfg_file), "insights", "--user", "jordan"]) == 0
    out = capsys.readouterr().out
    assert "Insights for Jordan" in out and "1. Work on Jordan" in out and "Christian" not in out
    assert main(["--config", str(cfg_file), "insights"]) == 0
    out = capsys.readouterr().out
    assert "Jordan: session 2026-10-01-1800 already has insights" in out and "1. Work on Christian" in out
    assert main(["--config", str(cfg_file), "insights", "--user", "Jordan", "--again"]) == 0
    assert model == ["2026-10-01-1800", "2026-10-01-1900", "2026-10-01-1800"]
    conn = connect(cfg.db_path)
    stored = latest_insight(conn, None, None, "2026-10-01-1800")
    assert stored is not None and stored["body"]["items"][0]["title"] == "Work on Jordan"
    assert stored["site_instance"] is None and unpublished_insights(conn, None) == []
    conn.close()


def test_cli_names_an_unknown_player_or_session(cfg_file, model, capsys):
    cfg = load_config(cfg_file)
    write_export(cfg.inbox, "a.csv", player="Jordan")
    main(["--config", str(cfg_file), "ingest"])
    capsys.readouterr()
    assert main(["--config", str(cfg_file), "insights", "--user", "nobody"]) == 1
    assert "No player named nobody." in capsys.readouterr().out
    assert main(["--config", str(cfg_file), "insights", "--session", "2020-01-01-0000"]) == 1
    assert "No session '2020-01-01-0000'" in capsys.readouterr().out and model == []


def test_cli_with_nothing_to_analyse_fails(cfg_file, model, capsys):
    assert main(["--config", str(cfg_file), "insights"]) == 1
    assert "Nothing to analyse" in capsys.readouterr().out and model == []


class InsightSite(FakeSite):
    def __init__(self, users):
        super().__init__(users)
        self.insights: list[dict] = []
        self.fail = False

    def push_insight(self, payload: dict) -> dict:
        self.calls.append("insight")
        if self.fail:
            from golfstats.sync import SiteError
            raise SiteError(500, "Something went wrong on the server.")
        self.insights.append(payload)
        return {"insight": payload, "duplicate": False}


USERS = [{"id": 1, "username": "jordan", "display_name": "Jordan", "players": ["Jordan"]},
         {"id": 2, "username": "chris", "display_name": "Chris", "players": ["Christian"]}]


@pytest.fixture
def site(cfg_file, monkeypatch):
    from golfstats import __main__ as cli
    cfg = load_config(cfg_file)
    save_site(cfg.site_path, "https://golf.example", "t" * 40)
    fake = InsightSite([dict(u) for u in USERS])
    monkeypatch.setattr(cli, "load_site", lambda _path: fake)
    for name, player, start in [("a.csv", "Jordan", START), ("b.csv", "Christian", START + timedelta(hours=2))]:
        fake.add(write_export(cfg.data_dir / "src", name, start, player=player).read_bytes(), name=name)
    return fake


def test_cli_syncs_first_then_publishes_one_insight_per_site_user(cfg_file, site, model, capsys):
    assert main(["--config", str(cfg_file), "insights"]) == 0
    out = capsys.readouterr().out
    assert out.index("Pushed 0, merged 0, pulled 2") < out.index("Insights for Jordan")
    assert out.count("Published to the site.") == 2
    assert site.calls.index("state") < site.calls.index("insight")
    by_user = {p["username"]: p for p in site.insights}
    assert set(by_user) == {"jordan", "chris"}
    assert by_user["chris"]["session_id"] == "2026-10-01-2000" and by_user["chris"]["body"]["items"][0]["title"] == \
        "Work on Chris"
    assert set(by_user["jordan"]) == {"uid", "username", "user_id", "site_instance", "session_id",
                                      "session_label", "created_at", "model", "body"}
    assert by_user["jordan"]["user_id"] == 1 and by_user["chris"]["user_id"] == 2
    assert main(["--config", str(cfg_file), "insights"]) == 0
    assert "already has insights" in capsys.readouterr().out and len(site.insights) == 2


def test_an_earlier_insight_that_still_fails_to_publish_fails_the_command(cfg_file, site, model, capsys):
    site.fail = True
    assert main(["--config", str(cfg_file), "insights", "--user", "jordan"]) == 1
    capsys.readouterr()
    assert main(["--config", str(cfg_file), "insights", "--user", "jordan"]) == 1
    out = capsys.readouterr().out
    assert "upload error: insights for jordan" in out and "already has insights" in out


def test_an_insight_that_fails_to_publish_is_sent_by_the_next_sync(cfg_file, site, model, capsys):
    site.fail = True
    assert main(["--config", str(cfg_file), "insights", "--user", "jordan"]) == 1
    assert "Not published: site answered 500" in capsys.readouterr().out and site.insights == []
    cfg = load_config(cfg_file)
    conn = connect(cfg.db_path)
    [pending] = unpublished_insights(conn, site.instance)
    conn.close()
    site.fail = False
    report = sync(cfg, site)
    assert report.insights == 1 and [p["uid"] for p in site.insights] == [pending["uid"]]
    conn = connect(cfg.db_path)
    assert unpublished_insights(conn, site.instance) == []
    conn.close()
    assert sync(cfg, site).insights == 0


def test_sync_holds_an_insight_made_for_another_site_database(cfg_file, site, model, capsys):
    site.fail = True
    main(["--config", str(cfg_file), "insights", "--user", "jordan"])
    site.fail = False
    site.instance = 8
    assert sync(load_config(cfg_file), site).insights == 0 and site.insights == []
    site.instance = 7
    assert sync(load_config(cfg_file), site).insights == 1
    assert site.insights[0]["site_instance"] == 7


def test_a_site_without_an_instance_is_refused_before_the_model_runs(cfg_file, site, model, capsys):
    site.instance = None
    assert main(["--config", str(cfg_file), "insights"]) == 1
    assert "Apply its D1 migrations" in capsys.readouterr().out and model == []


def test_sync_holds_an_insight_for_a_user_the_site_no_longer_has(cfg_file, site, model, capsys):
    site.fail = True
    main(["--config", str(cfg_file), "insights"])
    site.fail = False
    site.users_list = [{**USERS[0], "id": 3}]
    cfg = load_config(cfg_file)
    assert sync(cfg, site).insights == 0 and site.insights == []
    capsys.readouterr()
    assert main(["--config", str(cfg_file), "insights"]) == 0
    assert "Published to the site." in capsys.readouterr().out
    [sent] = site.insights
    assert sent["user_id"] == 3 and model.count("2026-10-01-1800") == 2
