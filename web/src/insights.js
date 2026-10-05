import { HttpError, bad, json, notFound, parseJsonColumn, readJson } from "./http.js";

const BODY_LIMIT = 64 * 1024;
const LIST_LIMIT = 20;
const MAX_ITEMS = 10;
const ITEM_FIELDS = { title: 200, why: 600, drill: 600, target: 600 };
const SUMMARY_MAX = 600;
const CONTROL = /[\u0000-\u001f\u007f]/;
const COLUMNS = "session_id, session_label, created_at, model, body";

function textField(value, name, max) {
  if (typeof value !== "string") throw bad(`${name} must be text.`);
  const trimmed = value.trim();
  if (trimmed.length < 1 || trimmed.length > max || CONTROL.test(trimmed)) {
    throw bad(`${name} must be 1 to ${max} characters without control characters.`);
  }
  return trimmed;
}

function createdAtField(value) {
  if (typeof value !== "string" || value.length > 64 || !/Z$/.test(value)) throw bad("created_at must be a UTC time ending in Z.");
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) throw bad("created_at must be a UTC time ending in Z.");
  return new Date(ms).toISOString();
}

function bodyField(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw bad("body must be an object.");
  const summary = textField(value.summary, "body.summary", SUMMARY_MAX);
  const items = value.items;
  if (!Array.isArray(items) || items.length < 1 || items.length > MAX_ITEMS) throw bad(`body.items must be a list of 1 to ${MAX_ITEMS} items.`);
  return {
    summary,
    items: items.map((item, i) => {
      if (item === null || typeof item !== "object" || Array.isArray(item)) throw bad(`body.items[${i}] must be an object.`);
      const out = {};
      for (const [key, max] of Object.entries(ITEM_FIELDS)) out[key] = textField(item[key], `body.items[${i}].${key}`, max);
      return out;
    }),
  };
}

function insightItem(row) {
  const body = parseJsonColumn(row.body, { summary: "", items: [] });
  return {
    session_id: row.session_id,
    session_label: row.session_label,
    created_at: row.created_at,
    model: row.model,
    summary: body.summary,
    items: body.items,
  };
}

export async function listInsights(user, env) {
  const { results } = await env.DB.prepare(`SELECT ${COLUMNS} FROM insights WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ${LIST_LIMIT}`)
    .bind(user.id)
    .all();
  return json({ insights: results.map(insightItem) });
}

export async function syncInsight(request, env) {
  const db = env.DB;
  const body = await readJson(request, BODY_LIMIT);
  if (typeof body.uid !== "string" || !/^[0-9a-f]{32}$/.test(body.uid)) throw bad("uid must be 32 lowercase hex characters.");
  if (typeof body.username !== "string" || body.username.length < 1 || body.username.length > 64) throw bad("username must be 1 to 64 characters.");
  if (!Number.isSafeInteger(body.user_id) || body.user_id < 1) throw bad("user_id must be the user's id from the sync state.");
  if (!Number.isSafeInteger(body.site_instance)) throw bad("site_instance must be the site_instance from the sync state.");
  if (typeof body.session_id !== "string" || !/^[0-9a-z-]{1,80}$/.test(body.session_id)) throw bad("session_id must be 1 to 80 lowercase letters, digits or dashes.");
  const fields = [
    body.uid,
    body.session_id,
    textField(body.session_label, "session_label", 100),
    createdAtField(body.created_at),
    textField(body.model, "model", 100),
    JSON.stringify(bodyField(body.body)),
  ];
  const instance = await db.prepare("SELECT value FROM meta WHERE key = 'site_instance'").first("value");
  if (instance !== body.site_instance) throw new HttpError(409, "This insight was made for another site database.");
  const user = await db.prepare("SELECT id FROM users WHERE id = ? AND username = ?").bind(body.user_id, body.username).first();
  if (!user) throw notFound("No such user. A user made again under the same name is a different user.");
  const out = await db
    .prepare(
      `INSERT INTO insights (uid, user_id, session_id, session_label, created_at, model, body)
       VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (uid) DO NOTHING`,
    )
    .bind(fields[0], user.id, ...fields.slice(1))
    .run();
  const row = await db.prepare(`SELECT ${COLUMNS} FROM insights WHERE uid = ?`).bind(body.uid).first();
  const created = (out.meta?.changes ?? 0) > 0;
  return json({ insight: insightItem(row), duplicate: !created }, created ? 201 : 200);
}
