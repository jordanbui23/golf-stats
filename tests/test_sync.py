import base64
import gzip
import hashlib
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from golfstats.config import Config
from golfstats.store import connect, ingest_file, list_uploads, load_shots, upload_raw
from golfstats.tps_csv import parse_tps_csv
from golfstats.sync import SiteClient, SiteError, check_site_url, key_hash, login_key, save_site, sync, utc_iso
from test_store_cli import START, write_conflicting, write_export


class FakeSite:
    def __init__(self, users: list[dict] | None = None):
        self.version = 0
        self.uploads: list[dict] = []
        self.blobs: dict[int, tuple[bytes, str]] = {}
        self.users_list = users or []
        self.pushed: list[dict] = []
        self.published: list[dict] = []
        self.calls: list[str] = []

    def add(self, data: bytes, *, by: str = "jordan", name: str = "web.csv", gz: bool = True, sha: str | None = None,
            stored: bytes | None = None) -> dict:
        self.version += 1
        upload = {"id": len(self.uploads) + 1, "uid": f"u{len(self.uploads) + 1}",
                  "sha256": sha or hashlib.sha256(data).hexdigest(), "filename": name, "size": len(data),
                  "encoding": "gzip" if gz else "identity", "uploaded_at": "2026-10-02T01:00:00.000Z",
                  "uploaded_by": by, "replace_stored": 0, "reverted_at": None, "changed_version": self.version}
        self.uploads.append(upload)
        body = stored if stored is not None else data
        self.blobs[upload["id"]] = (gzip.compress(body) if gz else body, upload["encoding"])
        return upload

    def state(self) -> dict:
        self.calls.append("state")
        return {"ledger_version": self.version, "uploads": [dict(u) for u in self.uploads],
                "users": [dict(u) for u in self.users_list]}

    def download(self, site_id: int) -> tuple[bytes, str]:
        self.calls.append(f"download {site_id}")
        return self.blobs[site_id]

    def push_upload(self, payload: dict) -> dict:
        self.calls.append("push")
        self.pushed.append(payload)
        same = [u for u in self.uploads if u["sha256"] == payload["sha256"]]
        if same:
            return {"upload": dict(same[-1]), "duplicate": True}
        data = gzip.decompress(base64.b64decode(payload["data_b64"]))
        upload = self.add(data, by="box", name=payload["filename"])
        upload["uploaded_at"] = payload["uploaded_at"]
        upload["replace_stored"] = int(payload.get("replace_stored", False))
        return {"upload": dict(upload), "duplicate": False}

    def publish(self, payload: dict) -> dict:
        self.calls.append("publish")
        self.published.append(payload)
        return {"published": len(payload["dashboards"])}


def cfg_at(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


def html_of(dashboard: dict) -> str:
    return gzip.decompress(base64.b64decode(dashboard["html_gz_b64"])).decode()


def test_login_key_matches_an_independent_pbkdf2():
    expected = hashlib.pbkdf2_hmac("sha256", "pässwörd 1".encode("utf-8"), b"golf-stats:jordan", 600000, 32)
    key = login_key("  Jordan \t", "pässwörd 1")
    assert key == expected.hex() and len(key) == 64 and key == key.lower()
    assert key_hash(key) == hashlib.sha256(expected).hexdigest()
    assert login_key("jordan", "pässwörd 1") == key and login_key("jordan", "pässwörd 2") != key


@pytest.mark.parametrize("url", [
    "http://example.com", "http://localhost.example.com", "http://127.0.0.1.nip.io", "http://user@localhost",
    "http://localhost:8788@evil.example", "ftp://localhost", "file:///etc/passwd", "localhost:8788", "https://",
    "javascript:alert(1)", "http://[::1]:8788", "http://localhost:99999", "https://golf.example?x=1",
])
def test_a_site_url_that_is_not_https_or_local_http_is_refused(url):
    with pytest.raises(ValueError, match="must be https://"):
        SiteClient(url, "t" * 40)


@pytest.mark.parametrize("url,clean", [
    ("https://golf.example.pages.dev/", "https://golf.example.pages.dev"),
    ("http://localhost:8788", "http://localhost:8788"), ("http://127.0.0.1:1", "http://127.0.0.1:1"),
    ("http://LOCALHOST", "http://LOCALHOST"),
])
def test_https_and_local_http_are_accepted(url, clean):
    assert check_site_url(url) == clean


class _Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def log_message(self, *_a):
        pass

    def _send(self, status: int, body: dict, headers: dict | None = None):
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/api/sync/state":
            self._send(200, {"ledger_version": 3, "uploads": [], "users": []})
        elif self.path == "/api/sync/users":
            self._send(302, {}, {"Location": "/elsewhere"})
        else:
            self._send(503, {"error": "Sync is not configured."})


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_the_client_sends_the_bearer_token_and_never_follows_a_redirect(server):
    client = SiteClient(server, "s" * 40)
    assert client.state()["ledger_version"] == 3
    with pytest.raises(SiteError) as redirect:
        client.users()
    with pytest.raises(SiteError) as down:
        client.download(1)
    assert redirect.value.status == 302 and (down.value.status, down.value.message) == (503, "Sync is not configured.")
    assert _Handler.seen == [("/api/sync/state", "Bearer " + "s" * 40), ("/api/sync/users", "Bearer " + "s" * 40),
                             ("/api/sync/uploads/1/raw", "Bearer " + "s" * 40)]


def test_site_settings_are_written_private(tmp_path):
    path = tmp_path / "data" / "site.json"
    save_site(path, "https://golf.example/", "k" * 40)
    assert json.loads(path.read_text()) == {"url": "https://golf.example", "token": "k" * 40}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        save_site(path, "http://golf.example", "k" * 40)
    with pytest.raises(ValueError, match="at least 32"):
        save_site(path, "https://golf.example", "short")


@pytest.fixture
def pacific(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def test_local_naive_times_become_utc_iso(pacific):
    assert utc_iso("2026-10-01T18:00:00") == "2026-10-02T01:00:00.000Z"
    assert utc_iso("2026-10-02T01:00:00.123456+00:00") == "2026-10-02T01:00:00.123Z"


def test_sync_pushes_local_uploads_and_skips_errored_ones(tmp_path, pacific):
    cfg = cfg_at(tmp_path)
    conn = connect(cfg.db_path)
    a = write_export(tmp_path / "in", "a.csv", player="Jordan")
    ingest_file(conn, a)
    bad = tmp_path / "in" / "bad.csv"
    bad.write_text("not,a,trackman,file\n")
    ingest_file(conn, bad)
    conn.execute("UPDATE uploads SET uploaded_at = '2026-10-01T18:30:00' WHERE filename = 'a.csv'")
    conn.close()
    site = FakeSite()
    report = sync(cfg, site)
    assert (report.pushed, report.merged, report.pulled, report.ledger_version) == (1, 0, 0, 1)
    assert site.calls[:2] == ["push", "state"] and site.calls[-1] == "publish"
    [payload] = site.pushed
    assert gzip.decompress(base64.b64decode(payload["data_b64"])) == a.read_bytes()
    assert (payload["filename"], payload["encoding"], payload["size"], payload["shots"]) == \
        ("a.csv", "gzip", len(a.read_bytes()), 20)
    assert payload["uploaded_at"] == "2026-10-02T01:30:00.000Z" and payload["players"] == ["Jordan"]
    assert payload["first_shot"] == START.isoformat() and payload["replace_stored"] is False
    conn = connect(cfg.db_path)
    linked = {u["filename"]: u["site_id"] for u in list_uploads(conn)}
    assert linked == {"a.csv": 1, "bad.csv": None}
    assert site.published[0]["results"] == [{"id": 1, "result": {
        "ok": True, "error": None, "shots_in_file": 20, "shots_used": 20, "conflicts": 0, "players": ["Jordan"],
        "warnings": []}}]
    assert sync(cfg, site).pushed == 0 and len(site.pushed) == 1


def test_sync_mirrors_site_uploads_and_copies_their_state(tmp_path):
    cfg = cfg_at(tmp_path)
    data = write_export(tmp_path / "in", "a.csv", player="Jordan").read_bytes()
    plain = write_export(tmp_path / "in", "b.csv", player="Jordan", seed=2, start=START + timedelta(days=1))
    site = FakeSite()
    site.add(data, name="phone.csv")
    site.add(plain.read_bytes(), by="christian", gz=False)
    report = sync(cfg, site)
    assert (report.pulled, report.state_changes, report.errors) == (2, 0, [])
    conn = connect(cfg.db_path)
    uploads = list_uploads(conn)
    assert [(u["site_id"], u["uploaded_by"], u["filename"]) for u in uploads] == \
        [(1, "jordan", "phone.csv"), (2, "christian", "web.csv")]
    assert upload_raw(conn, uploads[0]["id"]) == data and len(load_shots(conn)) == 40
    conn.close()

    site.uploads[0]["reverted_at"] = "2026-10-03T00:00:00.000Z"
    site.version += 1
    report = sync(cfg, site)
    assert (report.pulled, report.state_changes, report.ledger_version) == (0, 1, 3)
    assert [c for c in site.calls if c.startswith("download")] == ["download 1", "download 2"]
    conn = connect(cfg.db_path)
    assert len(load_shots(conn)) == 20 and list_uploads(conn)[0]["reverted_at"] == "2026-10-03T00:00:00.000Z"
    assert site.published[-1]["ledger_version"] == 3
    assert site.published[-1]["results"][0]["result"]["shots_used"] == 0


def test_a_duplicate_of_an_upload_already_linked_here_is_merged(tmp_path):
    cfg = cfg_at(tmp_path)
    src = write_export(tmp_path / "in", "a.csv")
    site = FakeSite()
    site.add(src.read_bytes())
    sync(cfg, site)
    site.uploads[0]["reverted_at"] = "2026-10-03T00:00:00.000Z"
    sync(cfg, site)
    conn = connect(cfg.db_path)
    assert ingest_file(conn, src).already_imported is False and len(list_uploads(conn)) == 2
    conn.close()
    report = sync(cfg, site)
    assert (report.merged, report.pushed) == (1, 0)
    conn = connect(cfg.db_path)
    assert [u["site_id"] for u in list_uploads(conn)] == [1] and load_shots(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM upload_shots").fetchone()[0] == 20


def test_a_duplicate_the_site_already_holds_is_linked_and_takes_the_site_state(tmp_path):
    cfg = cfg_at(tmp_path)
    src = write_export(tmp_path / "in", "a.csv")
    conn = connect(cfg.db_path)
    ingest_file(conn, src)
    conn.close()
    site = FakeSite()
    site.add(src.read_bytes())
    site.uploads[0]["replace_stored"] = 1
    report = sync(cfg, site)
    assert (report.pushed, report.pulled) == (1, 0) and not [c for c in site.calls if c.startswith("download")]
    conn = connect(cfg.db_path)
    [u] = list_uploads(conn)
    assert (u["site_id"], u["replace_stored"]) == (1, True)


def test_a_bad_download_is_an_upload_error_and_is_fetched_again_next_time(tmp_path):
    cfg = cfg_at(tmp_path)
    data = write_export(tmp_path / "in", "a.csv").read_bytes()
    site = FakeSite()
    site.add(data, stored=data[:-10])
    report = sync(cfg, site)
    assert len(report.errors) == 1 and "SHA-256 mismatch" in report.errors[0]
    result = site.published[0]["results"][0]["result"]
    assert not result["ok"] and "SHA-256" in result["error"] and result["shots_used"] == 0
    site.blobs[1] = (gzip.compress(data), "gzip")
    report = sync(cfg, site)
    assert (report.redownloaded, report.errors) == (1, [])
    conn = connect(cfg.db_path)
    assert len(load_shots(conn)) == 20 and upload_raw(conn, 1) == data
    conn.execute("UPDATE uploads SET raw = ? WHERE id = 1", (b"corrupt",))
    conn.close()
    assert sync(cfg, site).redownloaded == 1
    assert upload_raw(connect(cfg.db_path), 1) == data


def test_sync_publishes_one_dashboard_per_user_at_the_state_version(tmp_path):
    cfg = cfg_at(tmp_path)
    site = FakeSite(users=[
        {"username": "jordan", "display_name": "Jordan B", "players": ["JORDAN", "jbui"]},
        {"username": "christian", "display_name": "Christian", "players": ["Christian"]},
        {"username": "guest", "display_name": "Guest", "players": []},
    ])
    site.add(write_export(tmp_path / "in", "a.csv", player="Jordan").read_bytes())
    site.add(write_export(tmp_path / "in", "b.csv", player="jbui", seed=2,
                          start=START + timedelta(days=2)).read_bytes())
    site.add(write_export(tmp_path / "in", "c.csv", player="Christian", seed=3).read_bytes(), by="christian")
    report = sync(cfg, site)
    assert report.published == {"jordan": 2, "christian": 1, "guest": 0}
    results, *boards = site.published
    assert all(p["ledger_version"] == 3 for p in site.published)
    assert [r["id"] for r in results["results"]] == [1, 2, 3] and results["dashboards"] == []
    assert all(p["results"] == [] and len(p["dashboards"]) == 1 for p in boards)
    jordan, christian, guest = [p["dashboards"][0] for p in boards]
    html = html_of(jordan)
    assert '"player":"Jordan B"' in html and '"player":"Christian"' not in html and '"player":"jbui"' not in html
    assert '"player":"Christian"' in html_of(christian) and '"player":"Jordan' not in html_of(christian)
    assert guest == {"username": "guest", "sessions": 0, "html_gz_b64": None}



def test_results_are_published_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr("golfstats.sync.RESULTS_PER_PUBLISH", 2)
    cfg = cfg_at(tmp_path)
    site = FakeSite()
    for i in range(3):
        site.add(write_export(tmp_path / "in", f"{i}.csv", seed=i, start=START + timedelta(days=i)).read_bytes())
    sync(cfg, site)
    assert [[r["id"] for r in p["results"]] for p in site.published] == [[1, 2], [3]]


def test_a_cli_replace_upload_is_pushed_with_replace_stored(tmp_path):
    cfg = cfg_at(tmp_path)
    a = write_export(tmp_path / "in", "a.csv", seed=1)
    b = write_conflicting(tmp_path / "in", "b.csv", a)
    conn = connect(cfg.db_path)
    ingest_file(conn, a)
    assert ingest_file(conn, b, replace=True).replaced == 20
    conn.close()
    site = FakeSite()
    sync(cfg, site)
    assert [p["replace_stored"] for p in site.pushed] == [False, True]
    conn = connect(cfg.db_path)
    assert [u["replace_stored"] for u in list_uploads(conn)] == [False, True]
    assert {s["club_speed"] for s in load_shots(conn)} == {s["club_speed"] for s in parse_tps_csv(b).shots}


class FakeUsers(FakeSite):
    def __init__(self):
        super().__init__()
        self.puts: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def users(self) -> list[dict]:
        return [{"username": "jordan", "display_name": "Jordan", "players": ["Jordan"], "created_at": "x"}]

    def put_user(self, username: str, fields: dict) -> dict:
        self.puts.append((username, fields))
        return {"username": username, "display_name": fields.get("display_name", username),
                "players": fields.get("players", [])}

    def delete_user(self, username: str) -> None:
        self.deleted.append(username)


@pytest.fixture
def fake_site(monkeypatch):
    from golfstats import __main__ as cli
    site = FakeUsers()
    monkeypatch.setattr(cli, "load_site", lambda _path: site)
    return site


def test_cli_user_add_sends_only_the_key_hash(cfg_file, fake_site, monkeypatch, capsys):
    import io
    from golfstats.__main__ import main
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse\nignored\n"))
    assert main(["--config", str(cfg_file), "user", "add", "Jordan", "--player", "Jordan", "--player", "jbui",
                 "--display", "Jordan B", "--password-stdin"]) == 0
    [(name, fields)] = fake_site.puts
    assert name == "Jordan" and fields == {"display_name": "Jordan B", "players": ["Jordan", "jbui"],
                                           "key_hash": key_hash(login_key("jordan", "correct horse"))}
    assert "correct horse" not in capsys.readouterr().out


def test_cli_password_prompts_must_match(cfg_file, fake_site, monkeypatch, capsys):
    from golfstats.__main__ import main
    answers = iter(["one", "two"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))
    assert main(["--config", str(cfg_file), "user", "passwd", "jordan"]) == 1
    assert fake_site.puts == [] and "do not match" in capsys.readouterr().out
    answers = iter(["same", "same"])
    assert main(["--config", str(cfg_file), "user", "passwd", "jordan"]) == 0
    assert fake_site.puts == [("jordan", {"key_hash": key_hash(login_key("jordan", "same"))})]


def test_cli_user_players_list_and_remove(cfg_file, fake_site, capsys):
    from golfstats.__main__ import main
    assert main(["--config", str(cfg_file), "user", "players", "jordan", "Jordan", "JB"]) == 0
    assert main(["--config", str(cfg_file), "user", "list"]) == 0
    assert main(["--config", str(cfg_file), "user", "remove", "jordan"]) == 0
    assert fake_site.puts == [("jordan", {"players": ["Jordan", "JB"]})] and fake_site.deleted == ["jordan"]
    assert "jordan  Jordan  players: Jordan" in capsys.readouterr().out


def test_cli_site_saves_a_private_file_and_refuses_plain_http(cfg_file, monkeypatch, capsys):
    import io
    from golfstats.__main__ import main
    cfg = load_cfg(cfg_file)
    monkeypatch.setattr("sys.stdin", io.StringIO("t" * 40 + "\n"))
    assert main(["--config", str(cfg_file), "site", "--url", "http://golf.example", "--token-stdin"]) == 1
    assert not cfg.site_path.exists() and "must be https://" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("t" * 40 + "\n"))
    assert main(["--config", str(cfg_file), "site", "--url", "http://localhost:8788", "--token-stdin"]) == 0
    assert cfg.site_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(cfg.site_path.read_text()) == {"url": "http://localhost:8788", "token": "t" * 40}


def test_cli_sync_and_uploads_report(cfg_file, fake_site, capsys):
    from golfstats.__main__ import main
    cfg = load_cfg(cfg_file)
    fake_site.add(write_export(cfg.data_dir / "src", "a.csv").read_bytes(), name="phone.csv")
    fake_site.users_list = [{"username": "jordan", "display_name": "Jordan", "players": ["Demo"]}]
    assert main(["--config", str(cfg_file), "sync"]) == 0
    out = capsys.readouterr().out
    assert "Pushed 0, merged 0, pulled 1, state changes 0." in out and "jordan: 1 session(s) published." in out
    assert "Ledger version 1." in out
    assert main(["--config", str(cfg_file), "uploads"]) == 0
    assert "phone.csv  by jordan  site 1  active  20 of 20 shots used" in capsys.readouterr().out


def load_cfg(cfg_file):
    from golfstats.config import load_config
    return load_config(cfg_file)
