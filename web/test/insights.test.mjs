import { test } from "node:test";
import assert from "node:assert/strict";
import { makeSite } from "./helpers.mjs";

const PW = "pass phrase two";
const drill = { name: "Start-line gate", setup: "Two tees four feet ahead.", reps: "Driver, three sets of five.", pass: "Four of five start through the gate." };
const item = (title = "Start the driver inside the window") => ({ title, why: "6 of 16 inside.", drill, target: "More than 6 of 16." });

const ids = {};
let instance = 0;

function insight(extra = {}) {
  const username = extra.username ?? "jordan";
  return {
    uid: "a".repeat(32),
    username,
    user_id: ids[username] ?? 999,
    site_instance: instance,
    session_id: "2026-10-02-1318",
    session_label: "Fri Oct 2 2026, 1:18 PM",
    created_at: "2026-10-05T22:23:45.298Z",
    model: "us.anthropic.claude-sonnet-5-5",
    body: { summary: "Driver start line first.", items: [item(), item("Find the centre"), item("Turn on OERT")] },
    ...extra,
  };
}

async function siteWithUsers() {
  const site = makeSite();
  await site.addUser("jordan", PW, ["Jordan"]);
  await site.addUser("chris", PW, ["Christian"]);
  for (const r of site.db.prepare("SELECT id, username FROM users").all()) ids[r.username] = r.id;
  instance = (await (await site.sync("GET", "/api/sync/state")).json()).site_instance;
  return { site, jordan: await site.session("jordan", PW), chris: await site.session("chris", PW) };
}

test("the box publishes an insight once per uid and only with the token", async () => {
  const { site } = await siteWithUsers();
  assert.equal((await site.request("POST", "/api/sync/insights", { json: insight(), bearer: "x".repeat(40), origin: null })).status, 401);
  const first = await site.sync("POST", "/api/sync/insights", insight());
  assert.equal(first.status, 201);
  const body = await first.json();
  assert.equal(body.duplicate, false);
  assert.equal(body.insight.items.length, 3);
  assert.deepEqual(body.insight.items[0].drill, drill);
  assert.equal(body.insight.before, null);
  const withBefore = await site.sync("POST", "/api/sync/insights", insight({ uid: "f".repeat(32), body: { summary: "s", before: " Turn OERT on. ", items: [item()] } }));
  assert.equal((await withBefore.json()).insight.before, "Turn OERT on.");
  site.db.prepare("DELETE FROM insights WHERE uid = ?").run("f".repeat(32));
  const again = await site.sync("POST", "/api/sync/insights", insight({ body: { summary: "other", items: [item()] } }));
  assert.equal(again.status, 200);
  const dup = await again.json();
  assert.equal(dup.duplicate, true);
  assert.equal(dup.insight.summary, "Driver start line first.");
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM insights").get().n, 1);
});

test("an insight for an unknown user, or for a user made again under that name, is refused", async () => {
  const { site } = await siteWithUsers();
  assert.equal((await site.sync("POST", "/api/sync/insights", insight({ username: "nobody" }))).status, 404);
  const old = ids.jordan;
  assert.equal((await site.sync("DELETE", "/api/sync/users/jordan")).status, 204);
  await site.addUser("jordan", PW, ["Someone else"]);
  const res = await site.sync("POST", "/api/sync/insights", insight({ user_id: old }));
  assert.equal(res.status, 404);
  assert.match((await res.json()).error, /different user/);
  assert.equal((await site.sync("POST", "/api/sync/insights", insight({ user_id: ids.chris }))).status, 404);
  assert.equal((await site.sync("POST", "/api/sync/insights", insight({ user_id: "1" }))).status, 400);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM insights").get().n, 0);
  const state = await (await site.sync("GET", "/api/sync/state")).json();
  const now = state.users.find((u) => u.username === "jordan");
  assert.notEqual(now.id, old);
  assert.equal((await site.sync("POST", "/api/sync/insights", insight({ user_id: now.id }))).status, 201);
});

test("every site database gets its own instance, and an insight for another one is refused", async () => {
  const { site } = await siteWithUsers();
  assert.ok(Number.isSafeInteger(instance) && instance > 0);
  const other = makeSite();
  assert.notEqual((await (await other.sync("GET", "/api/sync/state")).json()).site_instance, instance);
  const res = await site.sync("POST", "/api/sync/insights", insight({ site_instance: instance + 1 }));
  assert.equal(res.status, 409);
  assert.equal((await site.sync("POST", "/api/sync/insights", insight({ site_instance: undefined }))).status, 400);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM insights").get().n, 0);
});

test("a malformed insight is refused with a sentence", async () => {
  const { site } = await siteWithUsers();
  const bad = [
    { uid: "A".repeat(32) },
    { session_id: "2026/10/02" },
    { created_at: "2026-10-05T22:23:45" },
    { model: "" },
    { session_label: "x".repeat(101) },
    { body: null },
    { body: { summary: "s", items: [] } },
    { body: { summary: "s", items: Array.from({ length: 11 }, () => item()) } },
    { body: { summary: "s", items: [{ ...item(), drill: 3 }] } },
    { body: { summary: "s", items: [{ ...item(), drill: "Hit ten balls." }] } },
    { body: { summary: "s", items: [{ ...item(), drill: { ...drill, pass: "" } }] } },
    { body: { summary: "s", items: [{ ...item(), drill: { name: "n", setup: "s", reps: "r" } }] } },
    { body: { summary: "s", before: 3, items: [item()] } },
    { body: { summary: "s", items: [{ ...item(), why: "line\nbreak" }] } },
    { body: { summary: "x".repeat(601), items: [item()] } },
  ];
  for (const extra of bad) {
    const res = await site.sync("POST", "/api/sync/insights", insight(extra));
    assert.equal(res.status, 400, JSON.stringify(extra));
    assert.equal(typeof (await res.json()).error, "string");
  }
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM insights").get().n, 0);
});

test("a user sees only their own insights, newest first", async () => {
  const { site, jordan, chris } = await siteWithUsers();
  assert.equal((await site.request("GET", "/api/insights")).status, 401);
  await site.sync("POST", "/api/sync/insights", insight({ uid: "1".repeat(32), created_at: "2026-10-03T10:00:00.000Z", session_id: "2026-10-02-1318" }));
  await site.sync("POST", "/api/sync/insights", insight({ uid: "2".repeat(32), created_at: "2026-10-05T10:00:00.000Z", session_id: "2026-10-04-1700" }));
  await site.sync("POST", "/api/sync/insights", insight({ uid: "3".repeat(32), username: "chris", body: { summary: "Chris only.", items: [item()] } }));
  const mine = await (await site.request("GET", "/api/insights", { cookie: jordan })).json();
  assert.deepEqual(mine.insights.map((i) => i.session_id), ["2026-10-04-1700", "2026-10-02-1318"]);
  assert.deepEqual(Object.keys(mine.insights[0]).sort(), ["before", "created_at", "items", "model", "session_id", "session_label", "summary"]);
  const theirs = await (await site.request("GET", "/api/insights", { cookie: chris })).json();
  assert.deepEqual(theirs.insights.map((i) => i.summary), ["Chris only."]);
});

test("the list holds the newest twenty", async () => {
  const { site, jordan } = await siteWithUsers();
  for (let i = 0; i < 22; i++) {
    const day = String(i + 1).padStart(2, "0");
    await site.sync("POST", "/api/sync/insights", insight({ uid: i.toString(16).padStart(32, "0"), created_at: `2026-09-${day}T10:00:00.000Z` }));
  }
  const { insights } = await (await site.request("GET", "/api/insights", { cookie: jordan })).json();
  assert.equal(insights.length, 20);
  assert.equal(insights[0].created_at, "2026-09-22T10:00:00.000Z");
});

test("deleting a user deletes their insights", async () => {
  const { site } = await siteWithUsers();
  await site.sync("POST", "/api/sync/insights", insight());
  assert.equal((await site.sync("DELETE", "/api/sync/users/jordan")).status, 204);
  assert.equal(site.db.prepare("SELECT COUNT(*) AS n FROM insights").get().n, 0);
});
