import { bad, empty, json, notFound, nowIso, readJson } from "./http.js";
import { decodeBase64, isHex64, randomHex } from "./crypto.js";
import { insertUploadStatements, ledgerVersion, parsePlayers, playerRowsStatement, storedBytes, syncItem, uploadById, uploadByUid, UPLOAD_COLUMNS } from "./db.js";
import {
  LIMITS,
  encodingField,
  filenameField,
  playersField,
  sha256Field,
  shotTimeField,
  shotsField,
  sizeField,
  storedBytesField,
} from "./validate.js";

const PUSH_LIMIT = Math.ceil(LIMITS.storedMax / 3) * 4 + 64 * 1024;
const PUBLISH_LIMIT = 64_000_000;
const USER_LIMIT = 16 * 1024;
const RESULT_MAX = 64 * 1024;
const HTML_GZ_MAX = 2_000_000;
const USERNAME = /^[a-z0-9][a-z0-9._-]{0,63}$/i;

function userItem(row) {
  return { username: row.username, display_name: row.display_name, players: parsePlayers(row.players) };
}

export async function syncState(env) {
  const db = env.DB;
  const [version, uploads, users] = await db.batch([
    db.prepare("SELECT value FROM meta WHERE key = 'ledger_version'"),
    db.prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads ORDER BY id`),
    db.prepare("SELECT username, display_name, players FROM users ORDER BY id"),
  ]);
  return json({
    ledger_version: version.results[0].value,
    uploads: uploads.results.map(syncItem),
    users: users.results.map(userItem),
  });
}

export async function syncRaw(env, id) {
  const db = env.DB;
  const row = id === null ? null : await uploadById(db, id);
  if (!row) throw notFound("No such upload.");
  const bytes = await storedBytes(db, row.uid);
  return new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Length": String(bytes.byteLength),
      "X-Encoding": row.encoding,
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function uploadedAtField(value) {
  if (typeof value !== "string" || value.length > 64 || !/(Z|[+-]\d{2}(:?\d{2})?)$/i.test(value.trim())) {
    throw bad("uploaded_at must be a date and time with a time zone.");
  }
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) throw bad("uploaded_at must be a date and time with a time zone.");
  return new Date(ms).toISOString();
}

function replaceStoredField(value) {
  if (value === undefined || value === null) return false;
  if (typeof value !== "boolean") throw bad("replace_stored must be true or false.");
  return value;
}

export async function syncPush(request, env) {
  const db = env.DB;
  const body = await readJson(request, PUSH_LIMIT);
  const encoding = encodingField(body.encoding);
  const size = sizeField(body.size);
  const bytes = decodeBase64(body.data_b64);
  if (bytes === null) throw bad("data_b64 must be base64.");
  const fields = {
    uid: randomHex(16),
    sha256: sha256Field(body.sha256),
    filename: filenameField(body.filename),
    size,
    encoding,
    bytes: storedBytesField(bytes, encoding, size),
    uploaded_at: uploadedAtField(body.uploaded_at),
    uploaded_by: "box",
    players: body.players === undefined || body.players === null ? [] : playersField(body.players, "players", LIMITS.boxPlayers),
    shots: shotsField(body.shots),
    first_shot: shotTimeField(body.first_shot, "first_shot"),
    last_shot: shotTimeField(body.last_shot, "last_shot"),
    replace_stored: replaceStoredField(body.replace_stored),
  };
  await db.batch(insertUploadStatements(db, fields, "NOT EXISTS (SELECT 1 FROM uploads WHERE sha256 = ?)", [fields.sha256]));
  const created = await uploadByUid(db, fields.uid);
  if (created) return json({ upload: syncItem(created), duplicate: false }, 201);
  const newest = await db.prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads WHERE sha256 = ? ORDER BY id DESC LIMIT 1`).bind(fields.sha256).first();
  return json({ upload: newest ? syncItem(newest) : null, duplicate: true }, 200);
}

function resultStatement(db, item, version) {
  if (item === null || typeof item !== "object" || !Number.isSafeInteger(item.id)) throw bad("Each result needs a numeric id.");
  const result = item.result;
  if (result === null || typeof result !== "object" || Array.isArray(result)) throw bad("Each result needs a result object.");
  const text = JSON.stringify(result);
  if (text.length > RESULT_MAX) throw bad("A result is too large.");
  const guard = "(u.result_version IS NULL OR u.result_version <= ?)";
  const statements = [];
  const sets = ["result = ?", "result_version = ?"];
  const params = [text, version];
  if (result.players !== undefined) {
    const players = playersField(result.players, "result.players", LIMITS.boxPlayers);
    sets.push("players = ?");
    params.push(JSON.stringify(players));
    statements.push(
      db
        .prepare(`DELETE FROM upload_players WHERE upload_id = ? AND EXISTS (SELECT 1 FROM uploads u WHERE u.id = ? AND ${guard})`)
        .bind(item.id, item.id, version),
      playerRowsStatement(db, players, `u.id = ? AND ${guard}`, item.id, version),
    );
  }
  if (result.shots_in_file !== undefined) {
    sets.push("shots = ?");
    params.push(shotsField(result.shots_in_file));
  }
  statements.push(db.prepare(`UPDATE uploads AS u SET ${sets.join(", ")} WHERE u.id = ? AND ${guard}`).bind(...params, item.id, version));
  return statements;
}

function dashboardStatement(db, item, version, publishedAt) {
  if (item === null || typeof item !== "object" || typeof item.username !== "string") throw bad("Each dashboard needs a username.");
  if (!Number.isSafeInteger(item.sessions) || item.sessions < 0) throw bad("sessions must be a whole number.");
  let html = null;
  if (item.sessions > 0) {
    html = decodeBase64(item.html_gz_b64);
    if (html === null || html.byteLength < 2) throw bad("html_gz_b64 must be base64 gzip data when sessions is above 0.");
    if (html[0] !== 0x1f || html[1] !== 0x8b) throw bad("html_gz_b64 is not gzip data.");
    if (html.byteLength > HTML_GZ_MAX) throw bad("A dashboard is too large.");
  } else if (item.html_gz_b64 !== null && item.html_gz_b64 !== undefined) {
    throw bad("html_gz_b64 must be null when sessions is 0.");
  }
  return db
    .prepare(
      `INSERT INTO analyses (user_id, based_on_version, published_at, sessions, html_gz)
       SELECT id, ?, ?, ?, ? FROM users WHERE username = ?
       ON CONFLICT (user_id) DO UPDATE SET
         based_on_version = excluded.based_on_version,
         published_at = excluded.published_at,
         sessions = excluded.sessions,
         html_gz = excluded.html_gz
       WHERE excluded.based_on_version >= analyses.based_on_version`,
    )
    .bind(version, publishedAt, item.sessions, html === null ? null : html.buffer, item.username);
}

export async function syncPublish(request, env) {
  const db = env.DB;
  const body = await readJson(request, PUBLISH_LIMIT);
  const version = body.ledger_version;
  if (!Number.isSafeInteger(version) || version < 0) throw bad("ledger_version must be a whole number.");
  const results = body.results ?? [];
  const dashboards = body.dashboards ?? [];
  if (!Array.isArray(results)) throw bad("results must be a list.");
  if (!Array.isArray(dashboards)) throw bad("dashboards must be a list.");
  if (version > (await ledgerVersion(db))) throw bad("ledger_version is newer than the site's.");
  const publishedAt = nowIso();
  const resultStatements = results.flatMap((item) => resultStatement(db, item, version));
  const dashboardStatements = dashboards.map((item) => dashboardStatement(db, item, version, publishedAt));
  if (resultStatements.length + dashboardStatements.length === 0) return json({ published: 0 });
  const out = await db.batch([...resultStatements, ...dashboardStatements]);
  const published = out.slice(resultStatements.length).reduce((n, r) => n + (r.meta?.changes ?? 0), 0);
  return json({ published });
}

export async function syncUsers(env) {
  const { results } = await env.DB.prepare("SELECT username, display_name, players, created_at FROM users ORDER BY id").all();
  return json({ users: results.map((r) => ({ ...userItem(r), created_at: r.created_at })) });
}

function usernameParam(raw) {
  let name;
  try {
    name = decodeURIComponent(raw);
  } catch {
    throw bad("The username is not valid.");
  }
  if (!USERNAME.test(name)) throw bad("A username is 1 to 64 letters, digits, dots, dashes or underscores, starting with a letter or digit.");
  if (name.toLowerCase() === "box") throw bad("The username box is reserved.");
  return name;
}

function displayNameField(value) {
  if (typeof value !== "string") throw bad("display_name must be text.");
  const trimmed = value.trim();
  if (trimmed.length < 1 || trimmed.length > LIMITS.displayName || /[\u0000-\u001f\u007f]/.test(trimmed)) {
    throw bad(`display_name must be 1 to ${LIMITS.displayName} characters.`);
  }
  return trimmed;
}

export async function putUser(request, env, rawName) {
  const db = env.DB;
  const name = usernameParam(rawName);
  const body = await readJson(request, USER_LIMIT);
  const displayName = body.display_name === undefined ? undefined : displayNameField(body.display_name);
  const players = body.players === undefined ? undefined : playersField(body.players);
  if (body.key_hash !== undefined && !isHex64(body.key_hash)) throw bad("key_hash must be 64 lowercase hex characters.");
  const keyHash = body.key_hash;
  const existing = await db.prepare("SELECT id FROM users WHERE username = ?").bind(name).first();
  if (!existing) {
    if (keyHash === undefined) throw bad("Creating a user needs key_hash.");
    await db
      .prepare("INSERT INTO users (username, display_name, players, key_hash, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (username) DO NOTHING")
      .bind(name, displayName ?? name, JSON.stringify(players ?? []), keyHash, nowIso())
      .run();
  } else {
    const statements = [];
    if (keyHash !== undefined) {
      statements.push(
        db.prepare("DELETE FROM web_sessions WHERE user_id = ? AND EXISTS (SELECT 1 FROM users WHERE id = ? AND key_hash != ?)").bind(existing.id, existing.id, keyHash),
        db.prepare("UPDATE users SET key_hash = ? WHERE id = ?").bind(keyHash, existing.id),
      );
    }
    if (displayName !== undefined) statements.push(db.prepare("UPDATE users SET display_name = ? WHERE id = ?").bind(displayName, existing.id));
    if (players !== undefined) statements.push(db.prepare("UPDATE users SET players = ? WHERE id = ?").bind(JSON.stringify(players), existing.id));
    if (statements.length) await db.batch(statements);
  }
  const row = await db.prepare("SELECT username, display_name, players, created_at FROM users WHERE username = ?").bind(name).first();
  return json({ user: { ...userItem(row), created_at: row.created_at } });
}

export async function deleteUser(env, rawName) {
  const db = env.DB;
  const name = usernameParam(rawName);
  const row = await db.prepare("SELECT id FROM users WHERE username = ?").bind(name).first();
  if (!row) throw notFound("No such user.");
  await db.batch([
    db.prepare("DELETE FROM web_sessions WHERE user_id = ?").bind(row.id),
    db.prepare("DELETE FROM analyses WHERE user_id = ?").bind(row.id),
    db.prepare("DELETE FROM users WHERE id = ?").bind(row.id),
  ]);
  return empty(204);
}
