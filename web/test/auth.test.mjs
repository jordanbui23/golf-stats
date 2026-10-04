import { test } from "node:test";
import assert from "node:assert/strict";
import { ORIGIN, cookieFrom, loginKey, makeSite } from "./helpers.mjs";

const PASSWORD = "correct horse battery";

async function siteWithJordan() {
  const site = makeSite();
  await site.addUser("jordan", PASSWORD, ["Jordan"], { display_name: "Jordan B" });
  return site;
}

test("login sets a session cookie with the required attributes and no Max-Age by default", async () => {
  const site = await siteWithJordan();
  const res = await site.login("jordan", PASSWORD);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { username: "jordan", display_name: "Jordan B" });
  const cookie = res.headers.get("Set-Cookie");
  assert.match(cookie, /^__Host-gs=[0-9a-f]{64}; Path=\/; Secure; HttpOnly; SameSite=Lax$/);
  const row = site.db.prepare("SELECT created_at, expires_at FROM web_sessions").get();
  assert.equal(Date.parse(row.expires_at) - Date.parse(row.created_at), 12 * 3600 * 1000);
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal(res.headers.get("X-Content-Type-Options"), "nosniff");
});

test("login with remember lasts 30 days and sets Max-Age", async () => {
  const site = await siteWithJordan();
  const res = await site.login("Jordan ", PASSWORD, true);
  assert.equal(res.status, 200);
  assert.match(res.headers.get("Set-Cookie"), /; Max-Age=2592000$/);
  const row = site.db.prepare("SELECT created_at, expires_at FROM web_sessions").get();
  assert.equal(Date.parse(row.expires_at) - Date.parse(row.created_at), 30 * 24 * 3600 * 1000);
});

test("the stored session is a hash, not the cookie token", async () => {
  const site = await siteWithJordan();
  const token = cookieFrom(await site.login("jordan", PASSWORD)).split("=")[1];
  const row = site.db.prepare("SELECT token_hash FROM web_sessions").get();
  assert.notEqual(row.token_hash, token);
});

test("a wrong password and an unknown user both answer 401", async () => {
  const site = await siteWithJordan();
  const { key } = await loginKey("jordan", "wrong password");
  const wrong = await site.request("POST", "/api/login", { json: { username: "jordan", key } });
  assert.equal(wrong.status, 401);
  assert.equal(wrong.headers.get("Set-Cookie"), null);
  const unknown = await site.request("POST", "/api/login", { json: { username: "nobody", key } });
  assert.equal(unknown.status, 401);
  assert.equal(typeof (await unknown.json()).error, "string");
});

test("a malformed login body answers 400", async () => {
  const site = await siteWithJordan();
  assert.equal((await site.request("POST", "/api/login", { json: { username: "jordan", key: "abc" } })).status, 400);
  assert.equal((await site.request("POST", "/api/login", { body: "not json", headers: { "Content-Type": "application/json" } })).status, 400);
  assert.equal((await site.request("POST", "/api/login", { json: { username: 5, key: "0".repeat(64) } })).status, 400);
});

test("ten failures for one username throttle that username with 429, even with the right key", async () => {
  const site = await siteWithJordan();
  const bad = "f".repeat(64);
  for (let i = 0; i < 10; i++) {
    const res = await site.request("POST", "/api/login", { json: { username: "jordan", key: bad }, headers: { "CF-Connecting-IP": `10.0.0.${i}` } });
    assert.equal(res.status, 401);
  }
  const res = await site.login("jordan", PASSWORD);
  assert.equal(res.status, 429);
});

test("ten failures from one IP throttle that IP for any username", async () => {
  const site = await siteWithJordan();
  const bad = "f".repeat(64);
  for (let i = 0; i < 10; i++) {
    await site.request("POST", "/api/login", { json: { username: `user${i}`, key: bad }, headers: { "CF-Connecting-IP": "203.0.113.9" } });
  }
  const { key } = await loginKey("jordan", PASSWORD);
  const blocked = await site.request("POST", "/api/login", { json: { username: "jordan", key }, headers: { "CF-Connecting-IP": "203.0.113.9" } });
  assert.equal(blocked.status, 429);
  const other = await site.request("POST", "/api/login", { json: { username: "jordan", key }, headers: { "CF-Connecting-IP": "203.0.113.10" } });
  assert.equal(other.status, 200);
});

test("failures older than 15 minutes do not count and a success clears the username's rows", async () => {
  const site = await siteWithJordan();
  const old = new Date(Date.now() - 16 * 60 * 1000).toISOString();
  const insert = site.db.prepare("INSERT INTO login_failures (username, ip, at) VALUES ('jordan', 'local', ?)");
  for (let i = 0; i < 12; i++) insert.run(old);
  const res = await site.login("jordan", PASSWORD);
  assert.equal(res.status, 200);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM login_failures WHERE username = 'jordan'").get().n, 0);
});

test("a failure deletes failure rows older than one day", async () => {
  const site = await siteWithJordan();
  site.db.prepare("INSERT INTO login_failures (username, ip, at) VALUES ('x', 'y', ?)").run(new Date(Date.now() - 25 * 3600 * 1000).toISOString());
  await site.request("POST", "/api/login", { json: { username: "jordan", key: "f".repeat(64) } });
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM login_failures").get().n, 1);
});

test("me answers with the user for a live session and 401 without one", async () => {
  const site = await siteWithJordan();
  const cookie = await site.session("jordan", PASSWORD);
  const res = await site.request("GET", "/api/me", { cookie });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { username: "jordan", display_name: "Jordan B", players: ["Jordan"] });
  assert.equal((await site.request("GET", "/api/me")).status, 401);
  assert.equal((await site.request("GET", "/api/me", { cookie: "__Host-gs=" + "a".repeat(64) })).status, 401);
});

test("an expired session is rejected and a login deletes the user's expired sessions", async () => {
  const site = await siteWithJordan();
  const cookie = await site.session("jordan", PASSWORD);
  site.db.prepare("UPDATE web_sessions SET expires_at = ?").run(new Date(Date.now() - 1000).toISOString());
  assert.equal((await site.request("GET", "/api/me", { cookie })).status, 401);
  await site.session("jordan", PASSWORD);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM web_sessions").get().n, 1);
});

test("logout deletes the session and clears the cookie", async () => {
  const site = await siteWithJordan();
  const cookie = await site.session("jordan", PASSWORD);
  const res = await site.request("POST", "/api/logout", { cookie });
  assert.equal(res.status, 204);
  assert.match(res.headers.get("Set-Cookie"), /^__Host-gs=; Path=\/; Secure; HttpOnly; SameSite=Lax; Max-Age=0$/);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM web_sessions").get().n, 0);
  assert.equal((await site.request("GET", "/api/me", { cookie })).status, 401);
});

test("login answers 403 when Origin is missing or belongs to another site", async () => {
  const site = await siteWithJordan();
  const { key } = await loginKey("jordan", PASSWORD);
  const missing = await site.request("POST", "/api/login", { json: { username: "jordan", key }, origin: null });
  assert.equal(missing.status, 403);
  const wrong = await site.request("POST", "/api/login", { json: { username: "jordan", key }, origin: "https://evil.example" });
  assert.equal(wrong.status, 403);
  const right = await site.request("POST", "/api/login", { json: { username: "jordan", key }, origin: ORIGIN });
  assert.equal(right.status, 200);
});

test("unknown API routes answer 404 JSON and wrong methods answer 405", async () => {
  const site = await siteWithJordan();
  const res = await site.request("GET", "/api/nothing");
  assert.equal(res.status, 404);
  assert.equal(typeof (await res.json()).error, "string");
  assert.equal(res.headers.get("Cache-Control"), "no-store");
  assert.equal((await site.request("GET", "/api/login")).status, 405);
});

test("an unexpected exception answers a generic 500 without details", async () => {
  const site = await siteWithJordan();
  const cookie = await site.session("jordan", PASSWORD);
  site.env.DB.db.exec("DROP TABLE analyses");
  const original = console.error;
  console.error = () => {};
  try {
    const res = await site.request("GET", "/api/dashboard", { cookie });
    assert.equal(res.status, 500);
    const body = await res.json();
    assert.deepEqual(Object.keys(body), ["error"]);
    assert.doesNotMatch(body.error, /analyses|SQL|table/i);
  } finally {
    console.error = original;
  }
});
