import { test } from "node:test";
import assert from "node:assert/strict";
import { fixture, gzip, makeSite, sha256Hex } from "./helpers.mjs";

const PW = "pass phrase one";

async function twoUsers() {
  const site = makeSite();
  await site.addUser("jordan", PW, ["Jordan"]);
  await site.addUser("christian", PW, ["Christian"]);
  return { site, jordan: await site.session("jordan", PW), christian: await site.session("christian", PW) };
}

function row(site, id) {
  return site.db.prepare("SELECT * FROM uploads WHERE id = ?").get(id);
}

test("a new upload answers 201 with its summary and bumps the ledger", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = fixture("jordan.csv");
  const res = await site.upload(jordan, bytes, { players: ["Jordan"], shots: 20, filename: "jordan.csv" });
  assert.equal(res.status, 201);
  const body = await res.json();
  assert.equal(body.duplicate, false);
  const u = body.upload;
  assert.deepEqual(Object.keys(u).sort(), [
    "changed_version", "filename", "first_shot", "id", "last_shot", "pending", "players", "replace_stored",
    "result", "reverted_at", "reverted_by", "shots", "size", "uploaded_at", "uploaded_by",
  ]);
  assert.equal(u.filename, "jordan.csv");
  assert.equal(u.size, bytes.byteLength);
  assert.equal(u.uploaded_by, "jordan");
  assert.deepEqual(u.players, ["Jordan"]);
  assert.equal(u.shots, 20);
  assert.equal(u.replace_stored, 0);
  assert.equal(u.result, null);
  assert.equal(u.pending, true);
  assert.equal(u.changed_version, 1);
  assert.equal(site.ledgerVersion(), 1);
  assert.equal(row(site, u.id).sha256, await sha256Hex(bytes));
});

test("a duplicate of an active upload answers 200 with the existing upload and changes nothing", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = fixture("jordan.csv");
  const first = await (await site.upload(jordan, bytes, { players: ["Jordan"] })).json();
  const res = await site.upload(jordan, bytes, { players: ["Jordan"], filename: "again.csv" });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.duplicate, true);
  assert.equal(body.upload.id, first.upload.id);
  assert.equal(body.upload.filename, "session.csv");
  assert.equal(site.ledgerVersion(), 1);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM uploads").get().n, 1);
});

test("a duplicate of an upload the user cannot see answers upload null", async () => {
  const { site, jordan, christian } = await twoUsers();
  const bytes = fixture("christian.csv");
  await site.upload(christian, bytes, { players: ["Christian"] });
  const res = await site.upload(jordan, bytes, { players: ["Christian"] });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { upload: null, duplicate: true });
});

test("a reverted upload does not block a fresh upload of the same file", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = fixture("jordan.csv");
  const first = (await (await site.upload(jordan, bytes)).json()).upload;
  await site.request("POST", `/api/uploads/${first.id}/revert`, { cookie: jordan });
  const res = await site.upload(jordan, bytes);
  assert.equal(res.status, 201);
  assert.equal(site.ledgerVersion(), 3);
});

test("upload and revert answer 403 when Origin is missing or wrong", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = fixture("jordan.csv");
  const id = (await (await site.upload(jordan, bytes)).json()).upload.id;
  for (const origin of [null, "https://evil.example"]) {
    const form = new FormData();
    form.set("file", new Blob([bytes]));
    assert.equal((await site.request("POST", "/api/uploads", { body: form, cookie: jordan, origin })).status, 403);
    assert.equal((await site.request("POST", `/api/uploads/${id}/revert`, { cookie: jordan, origin })).status, 403);
  }
  assert.equal(row(site, id).reverted_at, null);
});

test("upload without a session answers 401", async () => {
  const site = makeSite();
  assert.equal((await site.upload(null, fixture("jordan.csv"))).status, 401);
});

test("upload validation answers 400", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = fixture("jordan.csv");
  const cases = [
    { size: bytes.byteLength + 1 },
    { encoding: "brotli" },
    { encoding: "gzip" },
    { sha256: "ABC" },
    { filename: "x".repeat(201) },
    { players: Array.from({ length: 51 }, (_, i) => `p${i}`) },
    { players: ["x".repeat(101)] },
    { size: 64_000_001 },
  ];
  for (const opts of cases) {
    const res = await site.upload(jordan, bytes, opts);
    assert.equal(res.status, 400, JSON.stringify(opts).slice(0, 80));
    assert.equal(typeof (await res.json()).error, "string");
  }
  assert.equal((await site.upload(jordan, new Uint8Array(0), { size: 1 })).status, 400);
  const plain = await site.request("POST", "/api/uploads", { body: "x", cookie: jordan, headers: { "Content-Type": "text/plain" } });
  assert.equal(plain.status, 400);
  const huge = await site.request("POST", "/api/uploads", {
    body: "x",
    cookie: jordan,
    headers: { "Content-Type": "multipart/form-data; boundary=x", "Content-Length": "99999999" },
  });
  assert.equal(huge.status, 400);
  assert.equal(site.ledgerVersion(), 0);
});

test("uploads are visible to the uploader and to users sharing a player, and 404 to others", async () => {
  const { site, jordan, christian } = await twoUsers();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"), { players: ["JORDAN"] })).json()).upload.id;
  const theirs = await (await site.request("GET", "/api/uploads", { cookie: christian })).json();
  assert.deepEqual(theirs.uploads, []);
  for (const action of ["revert", "restore"]) {
    assert.equal((await site.request("POST", `/api/uploads/${id}/${action}`, { cookie: christian })).status, 404);
  }
  assert.equal((await site.request("POST", `/api/uploads/${id}/replace`, { cookie: christian, json: { on: true } })).status, 404);
  assert.equal((await site.request("GET", `/api/uploads/${id}/raw`, { cookie: christian })).status, 404);
  assert.equal((await site.request("GET", `/api/uploads/999/raw`, { cookie: jordan })).status, 404);
  assert.equal((await site.request("GET", `/api/uploads/abc/raw`, { cookie: jordan })).status, 404);

  await site.addUser("christian", PW, ["Christian", "jordan"]);
  const now = await (await site.request("GET", "/api/uploads", { cookie: christian })).json();
  assert.equal(now.uploads.length, 1);
});

test("the uploads list is newest first", async () => {
  const { site, jordan } = await twoUsers();
  await site.upload(jordan, fixture("jordan.csv"));
  await site.upload(jordan, fixture("christian.csv"));
  const body = await (await site.request("GET", "/api/uploads", { cookie: jordan })).json();
  assert.deepEqual(body.uploads.map((u) => u.id), [2, 1]);
});

test("revert marks the upload, stamps the version, and is idempotent", async () => {
  const { site, jordan } = await twoUsers();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload.id;
  const res = await site.request("POST", `/api/uploads/${id}/revert`, { cookie: jordan });
  assert.equal(res.status, 200);
  const u = (await res.json()).upload;
  assert.equal(u.reverted_by, "jordan");
  assert.ok(u.reverted_at);
  assert.equal(u.changed_version, 2);
  assert.equal(site.ledgerVersion(), 2);
  const again = await (await site.request("POST", `/api/uploads/${id}/revert`, { cookie: jordan })).json();
  assert.equal(again.upload.reverted_at, u.reverted_at);
  assert.equal(again.upload.changed_version, 2);
  assert.equal(site.ledgerVersion(), 2);
});

test("restore clears the revert, and answers 409 when an identical upload is active", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = fixture("jordan.csv");
  const first = (await (await site.upload(jordan, bytes)).json()).upload.id;
  await site.request("POST", `/api/uploads/${first}/revert`, { cookie: jordan });
  const restored = await site.request("POST", `/api/uploads/${first}/restore`, { cookie: jordan });
  assert.equal(restored.status, 200);
  const u = (await restored.json()).upload;
  assert.equal(u.reverted_at, null);
  assert.equal(u.reverted_by, null);
  assert.equal(u.changed_version, 3);
  const noop = await (await site.request("POST", `/api/uploads/${first}/restore`, { cookie: jordan })).json();
  assert.equal(noop.upload.changed_version, 3);
  assert.equal(site.ledgerVersion(), 3);

  await site.request("POST", `/api/uploads/${first}/revert`, { cookie: jordan });
  const second = (await (await site.upload(jordan, bytes)).json()).upload.id;
  const conflict = await site.request("POST", `/api/uploads/${first}/restore`, { cookie: jordan });
  assert.equal(conflict.status, 409);
  assert.notEqual(row(site, first).reverted_at, null);
  assert.equal(row(site, second).reverted_at, null);
  assert.equal(site.ledgerVersion(), 5);
});

test("replace toggles replace_stored and only a real change bumps the version", async () => {
  const { site, jordan } = await twoUsers();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"))).json()).upload.id;
  const on = await (await site.request("POST", `/api/uploads/${id}/replace`, { cookie: jordan, json: { on: true } })).json();
  assert.equal(on.upload.replace_stored, 1);
  assert.equal(on.upload.changed_version, 2);
  const same = await (await site.request("POST", `/api/uploads/${id}/replace`, { cookie: jordan, json: { on: true } })).json();
  assert.equal(same.upload.changed_version, 2);
  const off = await (await site.request("POST", `/api/uploads/${id}/replace`, { cookie: jordan, json: { on: false } })).json();
  assert.equal(off.upload.replace_stored, 0);
  assert.equal(off.upload.changed_version, 3);
  assert.equal(site.ledgerVersion(), 3);
  assert.equal((await site.request("POST", `/api/uploads/${id}/replace`, { cookie: jordan, json: { on: "yes" } })).status, 400);
});

function bigCsv(target) {
  const header = "sep=,\r\nDate,Player,Club,Ball Speed\r\n,,,[mph]\r\n";
  const lines = [header];
  let n = header.length;
  for (let i = 0; n < target; i++) {
    const line = `10/1/2026 6:${String(i % 60).padStart(2, "0")}:00 PM,Jordan,7 Iron,${(100 + (i % 37) / 7).toFixed(6)}\r\n`;
    lines.push(line);
    n += line.length;
  }
  return new TextEncoder().encode(lines.join(""));
}

test("a stored body above 1 MB round-trips byte for byte through both raw routes", async () => {
  const { site, jordan } = await twoUsers();
  const bytes = bigCsv(2_500_000);
  const id = (await (await site.upload(jordan, bytes, { players: ["Jordan"] })).json()).upload.id;
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM upload_chunks").get().n, 3);
  const user = await site.request("GET", `/api/uploads/${id}/raw`, { cookie: jordan });
  assert.equal(user.status, 200);
  assert.equal(user.headers.get("Content-Type"), "text/csv; charset=utf-8");
  assert.match(user.headers.get("Content-Disposition"), /^attachment; filename="session.csv"/);
  assert.equal(user.headers.get("Content-Encoding"), null);
  assert.deepEqual(new Uint8Array(await user.arrayBuffer()), bytes);
  const box = await site.sync("GET", `/api/sync/uploads/${id}/raw`);
  assert.equal(box.status, 200);
  assert.equal(box.headers.get("Content-Type"), "application/octet-stream");
  assert.equal(box.headers.get("X-Encoding"), "identity");
  assert.deepEqual(new Uint8Array(await box.arrayBuffer()), bytes);
});

test("a browser upload must be uncompressed and match its sha256", async () => {
  const { site, jordan, christian } = await twoUsers();
  const original = fixture("jordan.csv");
  const zipped = await site.upload(jordan, await gzip(original), { encoding: "gzip", original });
  assert.equal(zipped.status, 400);
  assert.match((await zipped.json()).error, /uncompressed/);
  const junk = fixture("christian.csv");
  const claimed = await site.upload(christian, junk, { sha256: await sha256Hex(original), players: ["Jordan"] });
  assert.equal(claimed.status, 400);
  assert.match((await claimed.json()).error, /sha256/);
  assert.equal(site.ledgerVersion(), 0);
  assert.equal((await site.upload(jordan, original, { players: ["Jordan"] })).status, 201);
});

test("box gzip storage is passed through with Content-Encoding on the user route and raw on the box route", async () => {
  const { site, jordan } = await twoUsers();
  const original = fixture("jordan.csv");
  const stored = await gzip(original);
  const res = await site.sync("POST", "/api/sync/uploads", {
    filename: "jordan.csv", sha256: await sha256Hex(original), size: original.byteLength, encoding: "gzip",
    data_b64: Buffer.from(stored).toString("base64"), uploaded_at: "2026-10-01T19:00:00Z", players: ["Jordan"], shots: 20,
  });
  assert.equal(res.status, 201);
  const id = (await res.json()).upload.id;
  const inline = await site.request("GET", `/api/uploads/${id}/raw?inline=1`, { cookie: jordan });
  assert.equal(inline.headers.get("Content-Encoding"), "gzip");
  assert.equal(inline.headers.get("Content-Disposition"), null);
  assert.equal(inline.headers.get("Cache-Control"), "no-store");
  assert.deepEqual(new Uint8Array(await inline.arrayBuffer()), stored);
  const box = await site.sync("GET", `/api/sync/uploads/${id}/raw`);
  assert.equal(box.headers.get("X-Encoding"), "gzip");
  assert.equal(box.headers.get("Content-Encoding"), null);
  assert.deepEqual(new Uint8Array(await box.arrayBuffer()), stored);
});

test("a new user with a deleted user's name cannot see the old user's uploads", async () => {
  const { site, jordan } = await twoUsers();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"), { players: ["Somebody"] })).json()).upload.id;
  assert.equal((await site.request("GET", `/api/uploads/${id}/raw`, { cookie: jordan })).status, 200);
  assert.equal((await site.sync("DELETE", "/api/sync/users/jordan")).status, 204);
  await site.addUser("jordan", "another pass phrase", ["Jordan"]);
  const again = await site.session("jordan", "another pass phrase");
  assert.deepEqual((await (await site.request("GET", "/api/uploads", { cookie: again })).json()).uploads, []);
  assert.equal((await site.request("GET", `/api/uploads/${id}/raw`, { cookie: again })).status, 404);
  assert.equal((await site.request("POST", `/api/uploads/${id}/revert`, { cookie: again })).status, 404);
  assert.equal(row(site, id).uploaded_by, "jordan");
});

test("player names match without regard to case, beyond ASCII too", async () => {
  const { site, christian } = await twoUsers();
  await site.addUser("jorg", PW, ["jörg"]);
  const jorg = await site.session("jorg", PW);
  await site.upload(christian, fixture("christian.csv"), { players: ["JÖRG"] });
  assert.equal((await (await site.request("GET", "/api/uploads", { cookie: jorg })).json()).uploads.length, 1);
});

test("the uploads list stops at 500 rows", async () => {
  const { site, jordan } = await twoUsers();
  const insert = site.db.prepare(
    `INSERT INTO uploads (uid, sha256, filename, size, encoding, uploaded_at, uploaded_by, uploaded_by_id, changed_version)
     VALUES (?, ?, 'x.csv', 1, 'identity', '2026-10-01T00:00:00.000Z', 'jordan', 1, 1)`,
  );
  for (let i = 0; i < 501; i++) insert.run(`u${i}`, String(i).padStart(64, "0"));
  const body = await (await site.request("GET", "/api/uploads", { cookie: jordan })).json();
  assert.equal(body.uploads.length, 500);
  assert.equal(body.uploads[0].id, 501);
});

test("a filename with non-ASCII characters is quoted safely in Content-Disposition", async () => {
  const { site, jordan } = await twoUsers();
  const id = (await (await site.upload(jordan, fixture("jordan.csv"), { filename: 'Ses"sión.csv' })).json()).upload.id;
  const res = await site.request("GET", `/api/uploads/${id}/raw`, { cookie: jordan });
  assert.equal(res.headers.get("Content-Disposition"), `attachment; filename="Ses_si_n.csv"; filename*=UTF-8''Ses%22si%C3%B3n.csv`);
});
