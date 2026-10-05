import { test } from "node:test";
import assert from "node:assert/strict";
import { TOKEN, fixture, gzip, makeSite, sha256Hex } from "./helpers.mjs";

const PW = "pass phrase two";
const b64 = (bytes) => Buffer.from(bytes).toString("base64");

async function pushBody(bytes, extra = {}) {
  return {
    filename: "box.csv",
    sha256: await sha256Hex(bytes),
    size: bytes.byteLength,
    encoding: "identity",
    data_b64: b64(bytes),
    uploaded_at: "2026-10-01T19:00:00-07:00",
    players: ["Jordan"],
    shots: 20,
    first_shot: "10/1/2026 6:00:00 PM",
    last_shot: "10/1/2026 6:20:00 PM",
    ...extra,
  };
}

async function siteWithJordan() {
  const site = makeSite();
  await site.addUser("jordan", PW, ["Jordan"]);
  return { site, jordan: await site.session("jordan", PW) };
}

test("sync routes answer 503 when the token is unset or shorter than 32 characters", async () => {
  for (const token of [null, "short-token"]) {
    const site = makeSite({ token });
    const res = await site.request("GET", "/api/sync/state", { bearer: token ?? "x" });
    assert.equal(res.status, 503);
    assert.equal(typeof (await res.json()).error, "string");
    assert.equal((await site.request("PUT", "/api/sync/users/a", { bearer: token ?? "x", json: {} })).status, 503);
  }
});

test("sync routes answer 401 with a wrong or missing token", async () => {
  const site = makeSite();
  assert.equal((await site.request("GET", "/api/sync/state", { bearer: TOKEN + "x" })).status, 401);
  assert.equal((await site.request("GET", "/api/sync/state")).status, 401);
  assert.equal((await site.request("GET", "/api/sync/state", { headers: { Authorization: TOKEN } })).status, 401);
  assert.equal((await site.request("GET", "/api/sync/nothing", { bearer: TOKEN })).status, 404);
});

test("sync routes do not need an Origin header", async () => {
  const site = makeSite();
  const res = await site.request("PUT", "/api/sync/users/jordan", { bearer: TOKEN, origin: null, json: { key_hash: "a".repeat(64) } });
  assert.equal(res.status, 200);
});

test("state returns the ledger version, uploads and users in their documented shapes", async () => {
  const { site, jordan } = await siteWithJordan();
  await site.upload(jordan, fixture("jordan.csv"), { players: ["Jordan"] });
  const res = await site.sync("GET", "/api/sync/state");
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ledger_version, 1);
  assert.deepEqual(Object.keys(body.uploads[0]).sort(), [
    "changed_version", "encoding", "filename", "id", "replace_stored", "reverted_at", "sha256", "size", "uid", "uploaded_at", "uploaded_by",
  ]);
  assert.equal(body.uploads[0].encoding, "identity");
  assert.deepEqual(body.users, [{ username: "jordan", display_name: "jordan", players: ["Jordan"] }]);
});

test("sync push creates a box upload with a normalised time", async () => {
  const site = makeSite();
  const bytes = fixture("jordan.csv");
  const res = await site.sync("POST", "/api/sync/uploads", await pushBody(bytes));
  assert.equal(res.status, 201);
  const body = await res.json();
  assert.equal(body.duplicate, false);
  assert.equal(body.upload.uploaded_by, "box");
  assert.equal(body.upload.uploaded_at, "2026-10-02T02:00:00.000Z");
  assert.equal(body.upload.changed_version, 1);
  const raw = await site.sync("GET", `/api/sync/uploads/${body.upload.id}/raw`);
  assert.deepEqual(new Uint8Array(await raw.arrayBuffer()), bytes);
});

test("sync push dedupes against reverted uploads too and returns the newest", async () => {
  const { site, jordan } = await siteWithJordan();
  const bytes = fixture("jordan.csv");
  const first = (await (await site.upload(jordan, bytes)).json()).upload.id;
  await site.request("POST", `/api/uploads/${first}/revert`, { cookie: jordan });
  const second = (await (await site.upload(jordan, bytes)).json()).upload.id;
  await site.request("POST", `/api/uploads/${second}/revert`, { cookie: jordan });
  const version = site.ledgerVersion();
  const res = await site.sync("POST", "/api/sync/uploads", await pushBody(bytes));
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.duplicate, true);
  assert.equal(body.upload.id, second);
  assert.equal(site.ledgerVersion(), version);
});

test("sync push validation answers 400", async () => {
  const site = makeSite();
  const bytes = fixture("jordan.csv");
  const cases = [
    { uploaded_at: "2026-10-01T19:00:00" },
    { uploaded_at: "yesterday" },
    { uploaded_at: undefined },
    { data_b64: "!!!" },
    { size: 5 },
    { encoding: "gzip" },
    { players: "Jordan" },
    { sha256: undefined },
  ];
  for (const extra of cases) {
    const res = await site.sync("POST", "/api/sync/uploads", await pushBody(bytes, extra));
    assert.equal(res.status, 400, JSON.stringify(extra));
  }
  assert.equal(site.ledgerVersion(), 0);
});

test("sync push keeps replace_stored for a new upload but never for a duplicate", async () => {
  const site = makeSite();
  const bytes = fixture("jordan.csv");
  const created = await (await site.sync("POST", "/api/sync/uploads", await pushBody(bytes, { replace_stored: true }))).json();
  assert.equal(created.upload.replace_stored, 1);
  const other = fixture("christian.csv");
  const plain = await (await site.sync("POST", "/api/sync/uploads", await pushBody(other))).json();
  assert.equal(plain.upload.replace_stored, 0);
  const again = await (await site.sync("POST", "/api/sync/uploads", await pushBody(other, { replace_stored: true }))).json();
  assert.equal(again.duplicate, true);
  assert.equal(again.upload.replace_stored, 0);
  const res = await site.sync("POST", "/api/sync/uploads", await pushBody(fixture("jordan.csv"), { replace_stored: "yes", sha256: "a".repeat(64) }));
  assert.equal(res.status, 400);
});

test("sync push accepts gzip storage", async () => {
  const site = makeSite();
  const original = fixture("jordan.csv");
  const stored = await gzip(original);
  const res = await site.sync("POST", "/api/sync/uploads", await pushBody(original, { encoding: "gzip", data_b64: b64(stored) }));
  assert.equal(res.status, 201);
  assert.equal((await res.json()).upload.encoding, "gzip");
});

async function dashboardOf(site, cookie) {
  return (await site.request("GET", "/api/dashboard", { cookie })).json();
}

test("publish writes results and analyses without bumping the ledger", async () => {
  const { site, jordan } = await siteWithJordan();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"), { players: ["Jordan"], shots: 3 })).json()).upload.id;
  const html = await gzip(new TextEncoder().encode("<!doctype html><p>stats</p>"));
  const result = { ok: true, error: null, shots_in_file: 20, shots_used: 20, conflicts: 0, players: ["Jordan", "Guest"], warnings: [] };
  const res = await site.sync("POST", "/api/sync/publish", {
    ledger_version: 1,
    results: [{ id, result }, { id: 999, result }],
    dashboards: [{ username: "jordan", sessions: 2, html_gz_b64: b64(html) }, { username: "ghost", sessions: 0, html_gz_b64: null }],
  });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { published: 1 });
  assert.equal(site.ledgerVersion(), 1);
  const list = await (await site.request("GET", "/api/uploads", { cookie: jordan })).json();
  const u = list.uploads[0];
  assert.deepEqual(u.result, result);
  assert.deepEqual(u.players, ["Jordan", "Guest"]);
  assert.equal(u.shots, 20);
  assert.equal(u.changed_version, 1);
  assert.equal(u.pending, false);
  const dash = await dashboardOf(site, jordan);
  assert.equal(dash.ledger_version, 1);
  assert.equal(dash.analysis.based_on_version, 1);
  assert.equal(dash.analysis.sessions, 2);
  assert.ok(dash.analysis.published_at);
  assert.deepEqual(dash.pending, []);
});

test("publish refuses a ledger_version above the current one and skips a lower one", async () => {
  const { site, jordan } = await siteWithJordan();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload.id;
  await site.request("POST", `/api/uploads/${id}/revert`, { cookie: jordan });
  const html = b64(await gzip(new TextEncoder().encode("<p>v2</p>")));
  const ahead = await site.sync("POST", "/api/sync/publish", { ledger_version: 3, results: [], dashboards: [] });
  assert.equal(ahead.status, 400);
  assert.deepEqual(await (await site.sync("POST", "/api/sync/publish", { ledger_version: 2, results: [], dashboards: [{ username: "jordan", sessions: 1, html_gz_b64: html }] })).json(), { published: 1 });
  const lower = await site.sync("POST", "/api/sync/publish", { ledger_version: 1, results: [], dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: null }] });
  assert.deepEqual(await lower.json(), { published: 0 });
  const dash = await dashboardOf(site, jordan);
  assert.equal(dash.analysis.based_on_version, 2);
  assert.equal(dash.analysis.sessions, 1);
  const same = await site.sync("POST", "/api/sync/publish", { ledger_version: 2, results: [], dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: null }] });
  assert.deepEqual(await same.json(), { published: 1 });
});

test("an older publish does not overwrite a newer result or its players", async () => {
  const { site, jordan } = await siteWithJordan();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload.id;
  await site.request("POST", `/api/uploads/${id}/revert`, { cookie: jordan });
  const newer = { ok: true, shots_in_file: 20, players: ["Jordan", "JordanBui"] };
  await site.sync("POST", "/api/sync/publish", { ledger_version: 2, results: [{ id, result: newer }], dashboards: [] });
  const older = { ok: true, shots_in_file: 3, players: ["Someone"] };
  await site.sync("POST", "/api/sync/publish", { ledger_version: 1, results: [{ id, result: older }], dashboards: [] });
  const r = site.db.prepare("SELECT result, players, shots, result_version FROM uploads WHERE id = ?").get(id);
  const names = site.db.prepare("SELECT name_lc FROM upload_players WHERE upload_id = ? ORDER BY name_lc").all(id).map((p) => p.name_lc);
  assert.deepEqual(JSON.parse(r.result), newer);
  assert.deepEqual([JSON.parse(r.players), names, r.shots, r.result_version], [["Jordan", "JordanBui"], ["jordan", "jordanbui"], 20, 2]);
  await site.sync("POST", "/api/sync/publish", { ledger_version: 2, results: [{ id, result: { ...newer, shots_in_file: 19, players: ["Jordan"] } }], dashboards: [] });
  assert.equal(site.db.prepare("SELECT shots FROM uploads WHERE id = ?").get(id).shots, 19);
  assert.deepEqual(site.db.prepare("SELECT name_lc FROM upload_players WHERE upload_id = ?").all(id).map((p) => p.name_lc), ["jordan"]);
});

test("publish validation answers 400 and writes nothing", async () => {
  const { site, jordan } = await siteWithJordan();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload.id;
  const cases = [
    { ledger_version: "1" },
    { ledger_version: 1, results: [{ id, result: "ok" }] },
    { ledger_version: 1, results: [{ id, result: { players: "x" } }] },
    { ledger_version: 1, dashboards: [{ username: "jordan", sessions: 1, html_gz_b64: null }] },
    { ledger_version: 1, dashboards: [{ username: "jordan", sessions: 1, html_gz_b64: b64(new TextEncoder().encode("plain")) }] },
    { ledger_version: 1, dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: "aGk=" }] },
  ];
  for (const body of cases) {
    assert.equal((await site.sync("POST", "/api/sync/publish", body)).status, 400, JSON.stringify(body));
  }
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM analyses").get().n, 0);
  assert.equal(site.db.prepare("SELECT result FROM uploads").get().result, null);
});

test("pending follows the ledger: everything before an analysis, then only later changes", async () => {
  const { site, jordan } = await siteWithJordan();
  const a = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload.id;
  const before = await dashboardOf(site, jordan);
  assert.equal(before.analysis, null);
  assert.deepEqual(before.pending.map((u) => u.id), [a]);
  await site.sync("POST", "/api/sync/publish", { ledger_version: 1, results: [], dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: null }] });
  assert.deepEqual((await dashboardOf(site, jordan)).pending, []);
  const b = (await (await site.upload(jordan, fixture("christian.csv"), { players: ["Christian"] })).json()).upload.id;
  await site.request("POST", `/api/uploads/${a}/revert`, { cookie: jordan });
  const after = await dashboardOf(site, jordan);
  assert.deepEqual(after.pending.map((u) => u.id), [b, a]);
  assert.ok(after.pending.every((u) => u.pending === true));
  await site.request("POST", `/api/uploads/${a}/replace`, { cookie: jordan, json: { on: true } });
  await site.sync("POST", "/api/sync/publish", { ledger_version: site.ledgerVersion(), results: [], dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: null }] });
  assert.deepEqual((await dashboardOf(site, jordan)).pending, []);
});

test("dashboard html is served gzipped with its own CSP, and 404 without one or with no sessions", async () => {
  const { site, jordan } = await siteWithJordan();
  assert.equal((await site.request("GET", "/api/dashboard/html", { cookie: jordan })).status, 404);
  assert.equal((await site.request("GET", "/api/dashboard/html")).status, 401);
  await site.sync("POST", "/api/sync/publish", { ledger_version: 0, results: [], dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: null }] });
  assert.equal((await site.request("GET", "/api/dashboard/html", { cookie: jordan })).status, 404);
  const html = await gzip(new TextEncoder().encode("<!doctype html><title>d</title>"));
  await site.sync("POST", "/api/sync/publish", { ledger_version: 0, results: [], dashboards: [{ username: "jordan", sessions: 3, html_gz_b64: b64(html) }] });
  const res = await site.request("GET", "/api/dashboard/html", { cookie: jordan });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Content-Type"), "text/html; charset=utf-8");
  assert.equal(res.headers.get("Content-Encoding"), "gzip");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal(
    res.headers.get("Content-Security-Policy"),
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
  );
  assert.deepEqual(new Uint8Array(await res.arrayBuffer()), html);
});

test("users PUT creates with key_hash only, updates fields, and a new key kills sessions", async () => {
  const site = makeSite();
  assert.equal((await site.sync("PUT", "/api/sync/users/jordan", { display_name: "Jordan" })).status, 400);
  assert.equal((await site.sync("PUT", "/api/sync/users/box", { key_hash: "a".repeat(64) })).status, 400);
  assert.equal((await site.sync("PUT", "/api/sync/users/-bad", { key_hash: "a".repeat(64) })).status, 400);
  assert.equal((await site.sync("PUT", "/api/sync/users/jordan", { key_hash: "A".repeat(64) })).status, 400);
  await site.addUser("jordan", PW, ["Jordan"]);
  const cookie = await site.session("jordan", PW);
  const rename = await site.sync("PUT", "/api/sync/users/JORDAN", { display_name: "Jordan B", players: ["Jordan", "JB"] });
  assert.equal(rename.status, 200);
  const user = (await rename.json()).user;
  assert.deepEqual({ ...user, created_at: undefined }, { username: "jordan", display_name: "Jordan B", players: ["Jordan", "JB"], created_at: undefined });
  assert.equal((await site.request("GET", "/api/me", { cookie })).status, 200);
  const sameKey = site.db.prepare("SELECT key_hash FROM users").get().key_hash;
  await site.sync("PUT", "/api/sync/users/jordan", { key_hash: sameKey });
  assert.equal((await site.request("GET", "/api/me", { cookie })).status, 200);
  await site.sync("PUT", "/api/sync/users/jordan", { key_hash: "b".repeat(64) });
  assert.equal((await site.request("GET", "/api/me", { cookie })).status, 401);
  assert.equal((await site.login("jordan", PW)).status, 401);
});

test("users list returns every user with created_at", async () => {
  const site = makeSite();
  await site.addUser("jordan", PW, ["Jordan"]);
  const body = await (await site.sync("GET", "/api/sync/users")).json();
  assert.equal(body.users.length, 1);
  assert.deepEqual(Object.keys(body.users[0]).sort(), ["created_at", "display_name", "players", "username"]);
});

test("users DELETE removes the user, their sessions and their analysis", async () => {
  const { site, jordan } = await siteWithJordan();
  await site.sync("POST", "/api/sync/publish", { ledger_version: 0, results: [], dashboards: [{ username: "jordan", sessions: 0, html_gz_b64: null }] });
  const res = await site.sync("DELETE", "/api/sync/users/jordan");
  assert.equal(res.status, 204);
  for (const table of ["users", "web_sessions", "analyses"]) {
    assert.equal(site.db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n, 0, table);
  }
  assert.equal((await site.request("GET", "/api/me", { cookie: jordan })).status, 401);
  assert.equal((await site.sync("DELETE", "/api/sync/users/jordan")).status, 404);
});

test("every ledger change stamps changed_version with the bumped counter", async () => {
  const { site, jordan } = await siteWithJordan();
  const a = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload;
  const b = (await (await site.sync("POST", "/api/sync/uploads", await pushBody(fixture("christian.csv"))).then((r) => r.json()))).upload;
  const c = (await (await site.request("POST", `/api/uploads/${a.id}/revert`, { cookie: jordan })).json()).upload;
  const d = (await (await site.request("POST", `/api/uploads/${a.id}/restore`, { cookie: jordan })).json()).upload;
  const e = (await (await site.request("POST", `/api/uploads/${a.id}/replace`, { cookie: jordan, json: { on: true } })).json()).upload;
  assert.deepEqual([a.changed_version, b.changed_version, c.changed_version, d.changed_version, e.changed_version], [1, 2, 3, 4, 5]);
  assert.equal(site.ledgerVersion(), 5);
});
