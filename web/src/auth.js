import { HttpError, bad, empty, json, nowIso, readJson } from "./http.js";
import { digestsEqual, equalBytes, fromHex, isHex64, randomHex, sha256, sha256Hex } from "./crypto.js";
import { parsePlayers } from "./db.js";

export const COOKIE = "__Host-gs";
const COOKIE_ATTRS = "Path=/; Secure; HttpOnly; SameSite=Lax";
const SHORT_MS = 12 * 60 * 60 * 1000;
const LONG_MS = 30 * 24 * 60 * 60 * 1000;
const WINDOW_MS = 15 * 60 * 1000;
const KEEP_MS = 24 * 60 * 60 * 1000;
const MAX_FAILURES = 10;
const DUMMY_HASH = "0".repeat(64);
const LOGIN_BODY_LIMIT = 4096;

export function originAllowed(request) {
  const origin = request.headers.get("Origin");
  return origin !== null && origin === new URL(request.url).origin;
}

function cookieToken(request) {
  const header = request.headers.get("Cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === COOKIE) {
      const value = part.slice(eq + 1).trim();
      return isHex64(value) ? value : null;
    }
  }
  return null;
}

async function tokenHash(token) {
  return sha256Hex(fromHex(token));
}

export async function currentUser(request, env) {
  const token = cookieToken(request);
  if (!token) return null;
  const row = await env.DB.prepare(
    `SELECT u.id, u.username, u.display_name, u.players
     FROM web_sessions s JOIN users u ON u.id = s.user_id
     WHERE s.token_hash = ? AND s.expires_at > ?`,
  )
    .bind(await tokenHash(token), nowIso())
    .first();
  if (!row) return null;
  return { id: row.id, username: row.username, display_name: row.display_name, players: parsePlayers(row.players) };
}

export async function requireUser(request, env) {
  const user = await currentUser(request, env);
  if (!user) throw new HttpError(401, "Sign in first.");
  return user;
}

export async function login(request, env) {
  const body = await readJson(request, LOGIN_BODY_LIMIT);
  const { username, key, remember } = body;
  if (typeof username !== "string" || username.trim().length < 1 || username.trim().length > 64) throw bad("Enter a username.");
  if (!isHex64(key)) throw bad("The login key must be 64 lowercase hex characters.");
  if (remember !== undefined && remember !== null && typeof remember !== "boolean") throw bad("remember must be true or false.");
  const name = username.trim().toLowerCase();
  const ip = request.headers.get("CF-Connecting-IP") || "local";
  const db = env.DB;
  const now = Date.now();

  const failures = await db
    .prepare("SELECT COUNT(*) AS n FROM login_failures WHERE at > ? AND (username = ? OR ip = ?)")
    .bind(new Date(now - WINDOW_MS).toISOString(), name, ip)
    .first("n");
  if (failures >= MAX_FAILURES) throw new HttpError(429, "Too many failed sign-ins. Try again in 15 minutes.");

  const user = await db.prepare("SELECT id, username, display_name, key_hash FROM users WHERE username = ?").bind(name).first();
  const presented = await sha256(fromHex(key));
  const stored = fromHex(user && isHex64(user.key_hash) ? user.key_hash : DUMMY_HASH);
  const match = equalBytes(presented, stored) && user !== null;

  if (!match) {
    await db.batch([
      db.prepare("INSERT INTO login_failures (username, ip, at) VALUES (?, ?, ?)").bind(name, ip, new Date(now).toISOString()),
      db.prepare("DELETE FROM login_failures WHERE at < ?").bind(new Date(now - KEEP_MS).toISOString()),
    ]);
    throw new HttpError(401, "The username or password is wrong.");
  }

  const token = randomHex(32);
  const created = new Date(now).toISOString();
  const expires = new Date(now + (remember === true ? LONG_MS : SHORT_MS)).toISOString();
  await db.batch([
    db.prepare("DELETE FROM login_failures WHERE username = ?").bind(name),
    db.prepare("DELETE FROM web_sessions WHERE user_id = ? AND expires_at <= ?").bind(user.id, created),
    db.prepare("INSERT INTO web_sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)").bind(await tokenHash(token), user.id, created, expires),
  ]);
  const cookie = `${COOKIE}=${token}; ${COOKIE_ATTRS}${remember === true ? `; Max-Age=${LONG_MS / 1000}` : ""}`;
  return json({ username: user.username, display_name: user.display_name }, 200, { "Set-Cookie": cookie });
}

export async function logout(request, env) {
  const token = cookieToken(request);
  if (token) await env.DB.prepare("DELETE FROM web_sessions WHERE token_hash = ?").bind(await tokenHash(token)).run();
  return empty(204, { "Set-Cookie": `${COOKIE}=; ${COOKIE_ATTRS}; Max-Age=0` });
}

export async function me(request, env) {
  const user = await requireUser(request, env);
  return json({ username: user.username, display_name: user.display_name, players: user.players });
}

export async function checkSyncToken(request, env) {
  const secret = env.SYNC_TOKEN;
  if (typeof secret !== "string" || secret.length < 32) throw new HttpError(503, "Sync is not configured on this site.");
  const header = request.headers.get("Authorization") || "";
  const match = /^Bearer (.+)$/.exec(header);
  const presented = match ? match[1] : "";
  if (!(await digestsEqual(presented, secret))) throw new HttpError(401, "The sync token is wrong.");
}
