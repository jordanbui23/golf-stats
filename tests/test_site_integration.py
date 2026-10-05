import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from golfstats.__main__ import main
from golfstats.config import load_config
from golfstats.sync import login_key
from test_store_cli import START, write_export

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")


def _node_has_sqlite() -> bool:
    if not NODE:
        return False
    out = subprocess.run([NODE, "-e", "require('node:sqlite')"], capture_output=True)
    return out.returncode == 0


LOCAL_SITE = re.compile(r"http://127\.0\.0\.1:\d+")
EXTERNAL = os.environ.get("GOLF_TEST_SITE_URL")

pytestmark = pytest.mark.skipif(not EXTERNAL and not _node_has_sqlite(),
                                reason="needs node with node:sqlite for web/dev/server.mjs")


@pytest.fixture
def site(tmp_path):
    if EXTERNAL:
        if not LOCAL_SITE.fullmatch(EXTERNAL):
            pytest.fail("GOLF_TEST_SITE_URL must be http://127.0.0.1:<port>: this test creates users and uploads")
        yield EXTERNAL, os.environ["GOLF_TEST_SITE_TOKEN"]
        return
    token = secrets.token_hex(24)
    proc = subprocess.Popen([NODE, str(WEB / "dev" / "server.mjs"), "--port", "0", "--db", str(tmp_path / "site.db"),
                             "--token", token], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline() if proc.stdout else ""
    match = re.search(r"listening on (http://127\.0\.0\.1:\d+)", line)
    if not match:
        proc.kill()
        pytest.fail(f"dev server did not start: {line!r} {proc.stderr.read() if proc.stderr else ''}")
    yield match.group(1), token
    proc.terminate()
    proc.wait(timeout=10)


class Browser:
    def __init__(self, url: str):
        self.url = url
        self.cookie = ""

    def call(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
        req = urllib.request.Request(self.url + path, data=body, method=method)
        req.add_header("Origin", self.url)
        if content_type:
            req.add_header("Content-Type", content_type)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, headers, raw = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, headers, raw = exc.code, exc.headers, exc.read()
        if headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        cookie = headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        return status, headers, raw

    def json(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode()
        status, _, raw = self.call(method, path, body, "application/json" if body is not None else None)
        return status, json.loads(raw) if raw else None

    def login(self, username: str, password: str, remember: bool = False):
        return self.json("POST", "/api/login", {"username": username, "key": login_key(username, password),
                                                 "remember": remember})

    def upload(self, path: Path, players: list[str]):
        original = path.read_bytes()
        fields = {"encoding": "identity", "sha256": hashlib.sha256(original).hexdigest(), "size": str(len(original)),
                  "filename": path.name, "players": json.dumps(players), "shots": "20"}
        boundary = "----golf" + secrets.token_hex(8)
        buf = io.BytesIO()
        for name, value in fields.items():
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                  "Content-Type: text/csv\r\n\r\n".encode())
        buf.write(original)
        buf.write(f"\r\n--{boundary}--\r\n".encode())
        status, _, raw = self.call("POST", "/api/uploads", buf.getvalue(), f"multipart/form-data; boundary={boundary}")
        return status, json.loads(raw)


def dashboard_data(html: bytes) -> dict:
    return json.loads(html.decode().split("const DATA = ", 1)[1].split(";</script>", 1)[0])


def run(cfg_file: Path, monkeypatch, capsys, *argv: str, stdin: str = "") -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(["--config", str(cfg_file), *argv])
    return code, capsys.readouterr().out


def test_upload_revert_and_sync_round_trip_through_the_real_site(site, cfg_file, tmp_path, monkeypatch, capsys):
    url, token = site
    cfg = load_config(cfg_file)
    assert run(cfg_file, monkeypatch, capsys, "site", "--url", url, "--token-stdin", stdin=token + "\n")[0] == 0
    code, out = run(cfg_file, monkeypatch, capsys, "user", "add", "jordan", "--player", "Jordan", "--player",
                    "JordanBui", "--display", "Jordan", "--password-stdin", stdin="hunter2 but longer\n")
    assert code == 0 and "players: Jordan, JordanBui" in out

    web = Browser(url)
    assert web.login("jordan", "wrong password")[0] == 401
    status, me = web.login("Jordan ", "hunter2 but longer")
    assert status == 200 and me["display_name"] == "Jordan" and web.cookie.startswith("__Host-gs=")

    first = write_export(tmp_path / "exports", "10-01.csv", player="Jordan", plan=[("7 Iron", 24)])
    status, body = web.upload(first, ["Jordan"])
    assert status == 201 and body["duplicate"] is False
    assert web.upload(first, ["Jordan"])[1]["duplicate"] is True
    status, dash = web.json("GET", "/api/dashboard")
    assert dash["analysis"] is None and [p["filename"] for p in dash["pending"]] == ["10-01.csv"]

    code, out = run(cfg_file, monkeypatch, capsys, "sync")
    assert code == 0 and "pulled 1" in out and "jordan: 1 session(s) published." in out
    status, dash = web.json("GET", "/api/dashboard")
    assert dash["analysis"]["sessions"] == 1 and dash["pending"] == []
    status, headers, html = web.call("GET", "/api/dashboard/html")
    assert status == 200 and "script-src 'unsafe-inline'" in headers["Content-Security-Policy"]
    data = dashboard_data(html)
    assert data["player"] == "Jordan" and len(data["sessions"]) == 1 and data["sessions"][0]["focus"]

    wrong = write_export(tmp_path / "exports", "christian.csv", player="Christian", seed=2,
                         start=START + timedelta(days=1))
    status, body = web.upload(wrong, ["Christian"])
    assert status == 201
    wrong_id = body["upload"]["id"]
    assert web.json("POST", f"/api/uploads/{wrong_id}/revert")[1]["upload"]["reverted_at"]

    later = write_export(cfg.inbox, "10-08.csv", player="JordanBui", seed=3, start=START + timedelta(days=7),
                         plan=[("7 Iron", 24)])
    assert run(cfg_file, monkeypatch, capsys, "ingest")[0] == 0 and not later.exists()
    code, out = run(cfg_file, monkeypatch, capsys, "sync")
    assert code == 0 and "Pushed 1" in out and "jordan: 2 session(s) published." in out

    status, listing = web.json("GET", "/api/uploads")
    by_name = {u["filename"]: u for u in listing["uploads"]}
    assert set(by_name) == {"10-01.csv", "christian.csv", "10-08.csv"}
    assert by_name["10-08.csv"]["uploaded_by"] == "box" and by_name["10-08.csv"]["players"] == ["JordanBui"]
    assert by_name["christian.csv"]["result"]["shots_used"] == 0
    assert by_name["10-01.csv"]["result"] == {"ok": True, "error": None, "shots_in_file": 24, "shots_used": 24,
                                              "conflicts": 0, "players": ["Jordan"], "warnings": []}
    data = dashboard_data(web.call("GET", "/api/dashboard/html")[2])
    assert data["player"] == "Jordan" and [s["player"] for s in data["sessions"]] == ["Jordan", "Jordan"]
    assert data["sessions"][1]["grades"] and b"Christian" not in json.dumps(data).encode()

    status, _, raw = web.call("GET", f"/api/uploads/{by_name['10-01.csv']['id']}/raw")
    assert status == 200 and raw == first.read_bytes()

    assert web.json("POST", f"/api/uploads/{by_name['10-01.csv']['id']}/revert")[0] == 200
    code, out = run(cfg_file, monkeypatch, capsys, "sync")
    assert "jordan: 1 session(s) published." in out
    assert web.json("POST", "/api/logout")[0] == 204
    assert web.json("GET", "/api/me")[0] == 401


def test_insights_reach_the_signed_in_golfer_through_the_real_site(site, cfg_file, tmp_path, monkeypatch, capsys):
    from golfstats import __main__ as cli
    from golfstats import insights as ins

    url, token = site
    assert run(cfg_file, monkeypatch, capsys, "site", "--url", url, "--token-stdin", stdin=token + "\n")[0] == 0
    assert run(cfg_file, monkeypatch, capsys, "user", "add", "ana", "--player", "Ana", "--password-stdin",
               stdin="a long enough password\n")[0] == 0
    web = Browser(url)
    assert web.login("ana", "a long enough password")[0] == 200
    assert web.upload(write_export(tmp_path / "exports", "s.csv", player="Ana", plan=[("7 Iron", 24)]),
                      ["Ana"])[0] == 201
    item = {"title": "Find the centre", "why": "Most strikes were thin.",
            "drill": {"name": "Face spray", "setup": "Spray the face.", "reps": "7-iron, two sets of five.",
                      "pass": "Four of five marks sit in the middle of the face."},
            "target": "More solid strikes."}
    answer = json.dumps({"summary": "Strike first.", "items": [item, item, item]})
    monkeypatch.setattr(cli, "generate", lambda s, ss, c: ins.generate(s, ss, c, ask=lambda *_: answer))

    code, out = run(cfg_file, monkeypatch, capsys, "insights")
    assert code == 0 and "pulled 1" in out and "Published to the site." in out
    status, body = web.json("GET", "/api/insights")
    assert status == 200 and [i["summary"] for i in body["insights"]] == ["Strike first."]
    assert body["insights"][0]["items"][0] == item and body["insights"][0]["session_id"] == "2026-10-01-1800"
    code, out = run(cfg_file, monkeypatch, capsys, "insights", "--again")
    assert code == 0 and len(web.json("GET", "/api/insights")[1]["insights"]) == 2
