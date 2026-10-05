import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { TOKEN, fixture, gzip, loginKey, sha256Hex } from "./helpers.mjs";

const SERVER = fileURLToPath(new URL("../dev/server.mjs", import.meta.url));

function launch(t, db, extra = []) {
  const child = spawn(process.execPath, [SERVER, "--port", "0", "--db", db, ...extra], { stdio: ["ignore", "pipe", "pipe"] });
  t.after(() => child.kill("SIGKILL"));
  return new Promise((resolve, reject) => {
    let out = "";
    child.stdout.on("data", (chunk) => {
      out += chunk;
      const m = /^listening on (http:\/\/127\.0\.0\.1:\d+)\n/.exec(out);
      if (m) resolve({ child, base: m[1] });
    });
    child.stderr.on("data", (chunk) => (out += chunk));
    child.on("exit", (code) => reject(new Error(`server exited ${code}: ${out}`)));
  });
}

function scratch(t) {
  const dir = mkdtempSync(join(tmpdir(), "gsweb-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return join(dir, "dev.db");
}

test("the dev server serves pages with the static headers and routes the API", async (t) => {
  const { base } = await launch(t, scratch(t));
  const page = await fetch(`${base}/login`);
  assert.equal(page.status, 200);
  assert.match(page.headers.get("Content-Type"), /^text\/html/);
  assert.match(page.headers.get("Content-Security-Policy"), /^default-src 'self'; script-src 'self';.*object-src 'none'$/);
  assert.equal(page.headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(page.headers.get("Referrer-Policy"), "same-origin");
  assert.match(await page.text(), /Keep me signed in on this device/);
  const css = await fetch(`${base}/style.css`);
  assert.match(css.headers.get("Content-Type"), /^text\/css/);
  assert.equal((await fetch(`${base}/`)).status, 200);
  assert.equal((await fetch(`${base}/_headers`)).status, 404);
  assert.equal((await fetch(`${base}/api/me`)).status, 401);
  assert.equal((await fetch(`${base}/api/sync/state`)).status, 503);
});

test("the dev server applies migrations once, keeps data across restarts and passes gzip bytes through", async (t) => {
  const db = scratch(t);
  const first = await launch(t, db, ["--token", TOKEN]);
  const auth = { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" };
  const { key, keyHash } = await loginKey("jordan", "server pass");
  const put = await fetch(`${first.base}/api/sync/users/jordan`, { method: "PUT", headers: auth, body: JSON.stringify({ key_hash: keyHash, players: ["Jordan"] }) });
  assert.equal(put.status, 200);
  first.child.kill("SIGKILL");

  const { base } = await launch(t, db, ["--token", TOKEN]);
  const login = await fetch(`${base}/api/login`, {
    method: "POST",
    headers: { Origin: base, "Content-Type": "application/json" },
    body: JSON.stringify({ username: "jordan", key, remember: false }),
  });
  assert.equal(login.status, 200);
  const cookie = login.headers.getSetCookie()[0].split(";")[0];

  const original = fixture("jordan.csv");
  const form = new FormData();
  form.set("file", new Blob([original]), "jordan.csv");
  form.set("encoding", "identity");
  form.set("sha256", await sha256Hex(original));
  form.set("size", String(original.byteLength));
  form.set("filename", "jordan.csv");
  form.set("players", JSON.stringify(["Jordan"]));
  const up = await fetch(`${base}/api/uploads`, { method: "POST", headers: { Origin: base, Cookie: cookie }, body: form });
  assert.equal(up.status, 201);
  const mine = (await up.json()).upload.id;
  const raw = await fetch(`${base}/api/uploads/${mine}/raw?inline=1`, { headers: { Cookie: cookie } });
  assert.equal(raw.headers.get("Content-Encoding"), null);
  assert.deepEqual(new Uint8Array(await raw.arrayBuffer()), original);

  const other = fixture("christian.csv");
  const stored = await gzip(other);
  const push = await fetch(`${base}/api/sync/uploads`, {
    method: "POST",
    headers: auth,
    body: JSON.stringify({
      filename: "c.csv", sha256: await sha256Hex(other), size: other.byteLength, encoding: "gzip",
      data_b64: Buffer.from(stored).toString("base64"), uploaded_at: "2026-10-02T19:00:00Z", players: ["Jordan"], shots: 14,
    }),
  });
  assert.equal(push.status, 201);
  const id = (await push.json()).upload.id;
  const zipped = await fetch(`${base}/api/uploads/${id}/raw?inline=1`, { headers: { Cookie: cookie } });
  assert.equal(zipped.headers.get("Content-Encoding"), "gzip");
  assert.deepEqual(new Uint8Array(await zipped.arrayBuffer()), other);
  const box = await fetch(`${base}/api/sync/uploads/${id}/raw`, { headers: auth });
  assert.equal(box.headers.get("X-Encoding"), "gzip");
  assert.deepEqual(new Uint8Array(await box.arrayBuffer()), stored);
});
