import { createServer } from "node:http";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { D1Database } from "./d1.mjs";
import { handle } from "../src/app.js";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const PUBLIC = join(WEB, "public");
const MIGRATIONS = join(WEB, "migrations");

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

function parseArgs(argv) {
  const out = { port: 8788, db: null, token: undefined };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--port") out.port = Number(argv[++i]);
    else if (arg === "--db") out.db = argv[++i];
    else if (arg === "--token") out.token = argv[++i];
    else throw new Error(`unknown argument ${arg}`);
  }
  if (!Number.isInteger(out.port) || out.port < 0 || out.port > 65535) throw new Error("--port must be a port number");
  if (!out.db) throw new Error("--db is required");
  return out;
}

function migrate(db) {
  db.db.exec("CREATE TABLE IF NOT EXISTS d1_migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)");
  const done = new Set(db.db.prepare("SELECT name FROM d1_migrations").all().map((r) => r.name));
  for (const name of readdirSync(MIGRATIONS).filter((n) => n.endsWith(".sql")).sort()) {
    if (done.has(name)) continue;
    const sql = readFileSync(join(MIGRATIONS, name), "utf8");
    db.db.exec("BEGIN");
    try {
      db.db.exec(sql);
      db.db.prepare("INSERT INTO d1_migrations (name) VALUES (?)").run(name);
      db.db.exec("COMMIT");
    } catch (err) {
      db.db.exec("ROLLBACK");
      throw err;
    }
  }
}

function globToRegExp(pattern) {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${escaped}$`);
}

function loadHeaderRules() {
  let text;
  try {
    text = readFileSync(join(PUBLIC, "_headers"), "utf8");
  } catch {
    return [];
  }
  const rules = [];
  let current = null;
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith("#")) continue;
    if (!/^\s/.test(line)) {
      current = { match: globToRegExp(line.trim()), headers: [] };
      rules.push(current);
    } else if (current) {
      const idx = line.indexOf(":");
      if (idx > 0) current.headers.push([line.slice(0, idx).trim(), line.slice(idx + 1).trim()]);
    }
  }
  return rules;
}

function resolveStatic(pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  if (decoded.includes("\0")) return null;
  const rel = normalize(decoded).replace(/^([/\\])+/, "");
  if (rel.split(/[/\\]/).some((p) => p === ".." || p.startsWith("_"))) return null;
  const base = join(PUBLIC, rel);
  if (base !== PUBLIC && !base.startsWith(PUBLIC + sep)) return null;
  const candidates = pathname.endsWith("/") ? [join(base, "index.html")] : [base, `${base}.html`, join(base, "index.html")];
  for (const file of candidates) {
    try {
      if (statSync(file).isFile()) return file;
    } catch {}
  }
  return null;
}

function staticHeaders(rules, pathname) {
  const headers = {};
  for (const rule of rules) {
    if (rule.match.test(pathname)) for (const [k, v] of rule.headers) headers[k] = v;
  }
  return headers;
}

async function toRequest(req, port) {
  const host = req.headers.host || `127.0.0.1:${port}`;
  const url = new URL(req.url, `http://${host}`);
  const headers = new Headers();
  for (const [k, v] of Object.entries(req.headers)) {
    if (Array.isArray(v)) for (const item of v) headers.append(k, item);
    else if (v !== undefined) headers.set(k, v);
  }
  let body;
  if (req.method !== "GET" && req.method !== "HEAD") {
    const parts = [];
    for await (const chunk of req) parts.push(chunk);
    body = Buffer.concat(parts);
  }
  return new Request(url, { method: req.method, headers, body });
}

async function writeResponse(res, response, head) {
  const headers = {};
  response.headers.forEach((value, key) => {
    if (key === "set-cookie") return;
    headers[key] = value;
  });
  const cookies = response.headers.getSetCookie();
  if (cookies.length) headers["set-cookie"] = cookies;
  const bytes = response.body && !head ? Buffer.from(await response.arrayBuffer()) : null;
  res.writeHead(response.status, headers);
  res.end(bytes);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const DB = new D1Database(args.db);
  migrate(DB);
  const env = { DB };
  if (args.token !== undefined) env.SYNC_TOKEN = args.token;
  const rules = loadHeaderRules();

  const server = createServer(async (req, res) => {
    try {
      const pathname = new URL(req.url, "http://x").pathname;
      if (pathname === "/api" || pathname.startsWith("/api/")) {
        await writeResponse(res, await handle(await toRequest(req, args.port), env), req.method === "HEAD");
        return;
      }
      if (req.method !== "GET" && req.method !== "HEAD") {
        res.writeHead(405, { "content-type": "text/plain; charset=utf-8", allow: "GET, HEAD" });
        res.end("Method not allowed\n");
        return;
      }
      const file = resolveStatic(pathname);
      const headers = staticHeaders(rules, pathname);
      if (!file) {
        res.writeHead(404, { ...headers, "content-type": "text/plain; charset=utf-8" });
        res.end("Not found\n");
        return;
      }
      if (pathname.endsWith(".html")) {
        const pretty = pathname.slice(0, -5).replace(/\/index$/, "/");
        res.writeHead(308, { ...headers, location: pretty || "/" });
        res.end();
        return;
      }
      const bytes = readFileSync(file);
      res.writeHead(200, { ...headers, "content-type": TYPES[extname(file)] || "application/octet-stream", "content-length": bytes.byteLength });
      res.end(req.method === "HEAD" ? null : bytes);
    } catch (err) {
      console.error(err);
      if (!res.headersSent) res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end("Internal error\n");
    }
  });

  server.listen(args.port, "127.0.0.1", () => {
    console.log(`listening on http://127.0.0.1:${server.address().port}`);
  });

  const stop = () => server.close(() => process.exit(0));
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}

main();
