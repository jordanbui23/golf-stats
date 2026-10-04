import { test } from "node:test";
import assert from "node:assert/strict";
import { D1Database } from "../dev/d1.mjs";

function db() {
  const d = new D1Database(":memory:");
  d.db.exec("CREATE TABLE p (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE) STRICT; CREATE TABLE c (pid INTEGER NOT NULL REFERENCES p(id), data BLOB) STRICT;");
  return d;
}

test("first returns a row, a column, or null", async () => {
  const d = db();
  await d.prepare("INSERT INTO p (name) VALUES (?)").bind("a").run();
  assert.deepEqual(await d.prepare("SELECT id, name FROM p").first(), { id: 1, name: "a" });
  assert.equal(await d.prepare("SELECT name FROM p").first("name"), "a");
  assert.equal(await d.prepare("SELECT name FROM p WHERE id = 9").first(), null);
});

test("run reports changes and last_row_id", async () => {
  const d = db();
  const r = await d.prepare("INSERT INTO p (name) VALUES (?)").bind("a").run();
  assert.equal(r.success, true);
  assert.equal(r.meta.changes, 1);
  assert.equal(r.meta.last_row_id, 1);
});

test("blobs come back as plain arrays and booleans bind as integers", async () => {
  const d = db();
  await d.prepare("INSERT INTO p (name) VALUES ('a')").run();
  await d.prepare("INSERT INTO c VALUES (?, ?)").bind(true, new Uint8Array([1, 2, 255])).run();
  const row = await d.prepare("SELECT pid, data FROM c").first();
  assert.equal(row.pid, 1);
  assert.ok(Array.isArray(row.data));
  assert.deepEqual(row.data, [1, 2, 255]);
});

test("binding undefined throws", () => {
  const d = db();
  assert.throws(() => d.prepare("SELECT ?").bind(undefined), /undefined/);
});

test("foreign keys are enforced", async () => {
  const d = db();
  await assert.rejects(d.prepare("INSERT INTO c VALUES (5, NULL)").run(), /FOREIGN KEY/);
});

test("batch rolls back every statement when one fails", async () => {
  const d = db();
  await assert.rejects(d.batch([
    d.prepare("INSERT INTO p (name) VALUES ('a')"),
    d.prepare("INSERT INTO p (name) VALUES ('a')"),
  ]));
  assert.equal(await d.prepare("SELECT COUNT(*) AS n FROM p").first("n"), 0);
});

test("batch returns one result per statement", async () => {
  const d = db();
  const res = await d.batch([
    d.prepare("INSERT INTO p (name) VALUES ('a')"),
    d.prepare("SELECT name FROM p"),
  ]);
  assert.equal(res.length, 2);
  assert.equal(res[0].meta.changes, 1);
  assert.deepEqual(res[1].results, [{ name: "a" }]);
});

test("raw returns arrays with optional column names", async () => {
  const d = db();
  await d.prepare("INSERT INTO p (name) VALUES ('a')").run();
  assert.deepEqual(await d.prepare("SELECT id, name FROM p").raw(), [[1, "a"]]);
  assert.deepEqual(await d.prepare("SELECT id, name FROM p").raw({ columnNames: true }), [["id", "name"], [1, "a"]]);
});
