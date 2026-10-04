import { readFileSync } from "node:fs";
import { D1Database } from "../dev/d1.mjs";
import { handle } from "../src/app.js";

export const ORIGIN = "https://golf.example";
export const TOKEN = "t".repeat(40);
const MIGRATION = readFileSync(new URL("../migrations/0001_init.sql", import.meta.url), "utf8");

export function fixture(name) {
  return new Uint8Array(readFileSync(new URL(`./fixtures/${name}`, import.meta.url)));
}

const keyCache = new Map();

export async function loginKey(username, password) {
  const id = `${username}\n${password}`;
  if (!keyCache.has(id)) {
    const enc = new TextEncoder();
    const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
    const bits = await crypto.subtle.deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt: enc.encode("golf-stats:" + username.trim().toLowerCase()), iterations: 600000 },
      base,
      256,
    );
    const key = new Uint8Array(bits);
    const keyHex = Buffer.from(key).toString("hex");
    const keyHash = Buffer.from(await crypto.subtle.digest("SHA-256", key)).toString("hex");
    keyCache.set(id, { key: keyHex, keyHash });
  }
  return keyCache.get(id);
}

export async function sha256Hex(bytes) {
  return Buffer.from(await crypto.subtle.digest("SHA-256", bytes)).toString("hex");
}

export async function gzip(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export function makeSite({ token = TOKEN } = {}) {
  const DB = new D1Database(":memory:");
  DB.db.exec(MIGRATION);
  const env = { DB };
  if (token !== null) env.SYNC_TOKEN = token;
  return new Site(env);
}

class Site {
  constructor(env) {
    this.env = env;
  }

  get db() {
    return this.env.DB.db;
  }

  async request(method, path, { json, body, headers = {}, cookie, origin = ORIGIN, bearer } = {}) {
    const h = new Headers(headers);
    if (origin && method !== "GET") h.set("Origin", origin);
    if (cookie) h.set("Cookie", cookie);
    if (bearer) h.set("Authorization", `Bearer ${bearer}`);
    let payload = body;
    if (json !== undefined) {
      payload = JSON.stringify(json);
      h.set("Content-Type", "application/json");
    }
    const req = new Request(ORIGIN + path, { method, headers: h, body: payload });
    return handle(req, this.env);
  }

  sync(method, path, json) {
    return this.request(method, path, { json, bearer: TOKEN, origin: null });
  }

  async addUser(username, password, players, extra = {}) {
    const { keyHash } = await loginKey(username, password);
    const res = await this.sync("PUT", `/api/sync/users/${username}`, { key_hash: keyHash, players, ...extra });
    if (res.status !== 200) throw new Error(`addUser ${res.status} ${await res.text()}`);
    return res.json();
  }

  async login(username, password, remember = false) {
    const { key } = await loginKey(username, password);
    return this.request("POST", "/api/login", { json: { username, key, remember } });
  }

  async session(username, password) {
    const res = await this.login(username, password);
    if (res.status !== 200) throw new Error(`login ${res.status} ${await res.text()}`);
    return cookieFrom(res);
  }

  async upload(cookie, bytes, { encoding = "identity", size, sha256, filename = "session.csv", players = [], shots = 3, original } = {}) {
    const form = new FormData();
    form.set("file", new Blob([bytes]), filename);
    form.set("encoding", encoding);
    form.set("sha256", sha256 ?? (await sha256Hex(original ?? bytes)));
    form.set("size", String(size ?? (original ?? bytes).byteLength));
    form.set("filename", filename);
    form.set("players", JSON.stringify(players));
    form.set("shots", String(shots));
    form.set("first_shot", "10/1/2026 6:00:00 PM");
    form.set("last_shot", "10/1/2026 6:20:00 PM");
    return this.request("POST", "/api/uploads", { body: form, cookie });
  }

  ledgerVersion() {
    return this.db.prepare("SELECT value FROM meta WHERE key = 'ledger_version'").get().value;
  }
}

export function cookieFrom(res) {
  const header = res.headers.get("Set-Cookie");
  return header.split(";")[0];
}
