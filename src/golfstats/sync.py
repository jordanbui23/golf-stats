from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import tempfile
import zlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .dashboard import render_dashboard
from .stats import split_sessions
from .store import (
    connect, delete_upload, link_site_id, list_uploads, load_shots, merge_aliases, mirror_upload, set_replace,
    set_reverted, shot_span, upload_raw, upload_result,
)

TIMEOUT = 30
ITERATIONS = 600_000
MIN_TOKEN = 32
RESULTS_PER_PUBLISH = 100


class SiteError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"site answered {status}: {message}" if status else message)
        self.status = status
        self.message = message


def login_key(username: str, password: str) -> str:
    salt = ("golf-stats:" + username.strip().lower()).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, 32).hex()


def key_hash(key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(key_hex)).hexdigest()


def check_site_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    ok = parts.hostname and port != -1 and not parts.username and not parts.password and (
        parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")))
    if not ok or parts.query or parts.fragment:
        raise ValueError(f"site URL must be https://, or http:// to localhost or 127.0.0.1, got {url!r}")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"refused redirect to {newurl}", headers, fp)


class SiteClient:
    def __init__(self, url: str, token: str):
        self.url = check_site_url(url)
        self.token = token
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[bytes, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                return resp.read(), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if exc.fp else ""
            try:
                message = json.loads(text)["error"]
            except (ValueError, KeyError, TypeError):
                message = text.strip()[:200] or str(exc.reason)
            raise SiteError(exc.code, message) from None
        except (urllib.error.URLError, OSError) as exc:
            raise SiteError(0, f"cannot reach {self.url}: {getattr(exc, 'reason', exc)}") from None

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        raw, _ = self._request(method, path, body)
        return json.loads(raw) if raw else {}

    def state(self) -> dict:
        return self._json("GET", "/api/sync/state")

    def download(self, site_id: int) -> tuple[bytes, str]:
        raw, headers = self._request("GET", f"/api/sync/uploads/{int(site_id)}/raw")
        return raw, headers.get("x-encoding", "identity")

    def push_upload(self, payload: dict) -> dict:
        return self._json("POST", "/api/sync/uploads", payload)

    def publish(self, payload: dict) -> dict:
        return self._json("POST", "/api/sync/publish", payload)

    def users(self) -> list[dict]:
        return self._json("GET", "/api/sync/users")["users"]

    def put_user(self, username: str, fields: dict) -> dict:
        return self._json("PUT", f"/api/sync/users/{urllib.parse.quote(username, safe='')}", fields)["user"]

    def delete_user(self, username: str) -> None:
        self._request("DELETE", f"/api/sync/users/{urllib.parse.quote(username, safe='')}")


def save_site(path: Path, url: str, token: str) -> None:
    url = check_site_url(url)
    if len(token) < MIN_TOKEN:
        raise ValueError(f"the sync token must be at least {MIN_TOKEN} characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"url": url, "token": token}) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_site(path: Path) -> SiteClient:
    if not path.exists():
        raise FileNotFoundError(f"no site settings in {path}. Run `bin/golf site --url URL` first.")
    site = json.loads(path.read_text())
    return SiteClient(site["url"], site["token"])


def utc_iso(local: str) -> str:
    dt = datetime.fromisoformat(local).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def gzip_b64(data: bytes) -> str:
    return base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii")


@dataclass
class SyncReport:
    ledger_version: int = 0
    pushed: int = 0
    merged: int = 0
    pulled: int = 0
    redownloaded: int = 0
    state_changes: int = 0
    published: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def visible_names(players: list[str], aliases: dict[str, str]) -> list[str]:
    return sorted({*players, *(aliases.get(p, p) for p in players)})


def _has_verified_bytes(conn, upload: dict, sha256: str) -> bool:
    return upload["error"] is None and hashlib.sha256(upload_raw(conn, upload["id"])).hexdigest() == sha256


def _push(conn, client, cfg: Config, report: SyncReport) -> None:
    linked = {u["site_id"]: u for u in list_uploads(conn) if u["site_id"] is not None}
    for u in list_uploads(conn):
        if u["site_id"] is not None or u["error"] is not None or u["reverted_at"] is not None:
            continue
        raw = upload_raw(conn, u["id"])
        first, last = shot_span(conn, u["id"])
        result = upload_result(conn, u["id"])
        answer = client.push_upload({
            "filename": u["filename"], "sha256": u["sha256"], "size": len(raw), "encoding": "gzip",
            "data_b64": gzip_b64(raw), "uploaded_at": utc_iso(u["uploaded_at"]),
            "players": visible_names(result["players"], cfg.aliases),
            "shots": u["shots_in_file"], "first_shot": first, "last_shot": last,
            "replace_stored": bool(u["replace_stored"]),
        })
        site = answer["upload"]
        other = linked.get(site["id"])
        if answer.get("duplicate") and other is not None:
            if other["sha256"] != u["sha256"] or not _has_verified_bytes(conn, other, u["sha256"]):
                report.errors.append(f"{u['filename']}: the site matched it to upload {site['id']}, whose copy "
                                     "here does not verify. Kept this copy and did not push it.")
                continue
            delete_upload(conn, u["id"])
            report.merged += 1
            continue
        link_site_id(conn, u["id"], site["id"])
        set_replace(conn, u["id"], bool(site.get("replace_stored")))
        set_reverted(conn, u["id"], site.get("reverted_at"))
        linked[site["id"]] = {**u, "site_id": site["id"]}
        report.pushed += 1


def _fetch(client, site_upload: dict) -> bytes:
    data, encoding = client.download(site_upload["id"])
    if encoding != "gzip":
        return data
    unzip = zlib.decompressobj(wbits=31)
    try:
        return unzip.decompress(data, int(site_upload["size"]) + 1)
    except zlib.error:
        return data


def _mirror(conn, client, site_uploads: list[dict], report: SyncReport) -> tuple[dict[int, int], list[dict]]:
    local = {u["site_id"]: u for u in list_uploads(conn) if u["site_id"] is not None}
    ids: dict[int, int] = {}
    unverified: list[dict] = []
    for s in site_uploads:
        known = local.get(s["id"])
        fresh = known is None or known["sha256"] != s["sha256"] or \
            hashlib.sha256(upload_raw(conn, known["id"])).hexdigest() != s["sha256"]
        if fresh:
            data = _fetch(client, s)
            ids[s["id"]] = mirror_upload(conn, data, site_id=s["id"], filename=s["filename"],
                                         uploaded_by=s["uploaded_by"], uploaded_at=s["uploaded_at"],
                                         replace=bool(s["replace_stored"]), reverted_at=s.get("reverted_at"),
                                         sha256=s["sha256"])
            if hashlib.sha256(data).hexdigest() != s["sha256"] and s.get("reverted_at") is None:
                unverified.append(s)
            report.pulled += known is None
            report.redownloaded += known is not None
            continue
        ids[s["id"]] = known["id"]
        if bool(s["replace_stored"]) != known["replace_stored"] or s.get("reverted_at") != known["reverted_at"]:
            set_replace(conn, known["id"], bool(s["replace_stored"]))
            set_reverted(conn, known["id"], s.get("reverted_at"))
            report.state_changes += 1
    return ids, unverified


def build_dashboard(shots: list[dict], user: dict, cfg: Config) -> dict:
    players = {p.lower() for p in user.get("players", [])}
    name = user.get("display_name") or user["username"]
    mine = [{**s, "player": name} for s in shots if (s.get("player") or "").lower() in players]
    sessions = split_sessions(mine, cfg.gap_minutes)
    html = gzip_b64(render_dashboard(sessions, cfg, name).encode("utf-8")) if sessions else None
    return {"username": user["username"], "sessions": len(sessions), "html_gz_b64": html}


def sync(cfg: Config, client: SiteClient) -> SyncReport:
    report = SyncReport()
    conn = connect(cfg.db_path)
    try:
        _push(conn, client, cfg, report)
        state = client.state()
        report.ledger_version = state["ledger_version"]
        ids, unverified = _mirror(conn, client, state["uploads"], report)
        results = []
        for s in state["uploads"]:
            result = upload_result(conn, ids[s["id"]])
            result["players"] = visible_names(result["players"], cfg.aliases)
            results.append({"id": s["id"], "result": result})
            if not result["ok"]:
                report.errors.append(f"{s['filename']} (site upload {s['id']}): {result['error']}")
        for i in range(0, len(results), RESULTS_PER_PUBLISH):
            client.publish({"ledger_version": report.ledger_version,
                            "results": results[i:i + RESULTS_PER_PUBLISH], "dashboards": []})
        if unverified:
            report.errors.append(f"Dashboards not published: {len(unverified)} active upload(s) did not download intact, "
                                 f"first {unverified[0]['filename']} (site upload {unverified[0]['id']}). Sync again, "
                                 "or revert it on the site.")
            return report
        shots, _ = merge_aliases(load_shots(conn), cfg.aliases)
        for user in state["users"]:
            board = build_dashboard(shots, user, cfg)
            client.publish({"ledger_version": report.ledger_version, "results": [], "dashboards": [board]})
            report.published[board["username"]] = board["sessions"]
    finally:
        conn.close()
    return report
