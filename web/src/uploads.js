import { HttpError, bad, json, notFound, nowIso, readBody, readJson } from "./http.js";
import { randomHex } from "./crypto.js";
import {
  LEDGER_VERSION,
  UPLOAD_COLUMNS,
  analysisFor,
  bumpWhere,
  canSee,
  insertUploadStatements,
  storedBytes,
  summary,
  uploadById,
  uploadByUid,
} from "./db.js";
import {
  LIMITS,
  encodingField,
  filenameField,
  playersJsonField,
  sha256Field,
  shotTimeField,
  shotsField,
  sizeField,
  storedBytesField,
} from "./validate.js";

const MULTIPART_LIMIT = LIMITS.storedMax + 64 * 1024;
const SMALL_JSON_LIMIT = 4096;

export async function listUploads(user, env) {
  const db = env.DB;
  const analysis = await analysisFor(db, user.id);
  const { results } = await db.prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads ORDER BY id DESC`).all();
  return json({ uploads: results.filter((r) => canSee(user, r)).map((r) => summary(r, analysis)) });
}

function formText(form, name) {
  const value = form.get(name);
  if (value === null) return null;
  if (typeof value !== "string") throw bad(`${name} must be a text field.`);
  return value;
}

async function parseUploadForm(request) {
  const type = request.headers.get("Content-Type") || "";
  if (!/^multipart\/form-data\s*;/i.test(type)) throw bad("Send the upload as multipart/form-data.");
  const bytes = await readBody(request, MULTIPART_LIMIT);
  let form;
  try {
    form = await new Response(bytes, { headers: { "Content-Type": type } }).formData();
  } catch {
    throw bad("The upload form could not be read.");
  }
  const file = form.get("file");
  if (file === null || typeof file === "string") throw bad("The upload needs a file.");
  const encoding = encodingField(formText(form, "encoding"));
  const size = sizeField(formText(form, "size"));
  return {
    bytes: storedBytesField(new Uint8Array(await file.arrayBuffer()), encoding, size),
    encoding,
    size,
    sha256: sha256Field(formText(form, "sha256")),
    filename: filenameField(formText(form, "filename")),
    players: playersJsonField(formText(form, "players")),
    shots: shotsField(formText(form, "shots")),
    first_shot: shotTimeField(formText(form, "first_shot"), "first_shot"),
    last_shot: shotTimeField(formText(form, "last_shot"), "last_shot"),
  };
}

export async function createUpload(request, user, env) {
  const db = env.DB;
  const fields = await parseUploadForm(request);
  fields.uid = randomHex(16);
  fields.uploaded_at = nowIso();
  fields.uploaded_by = user.username;
  const guard = "NOT EXISTS (SELECT 1 FROM uploads WHERE sha256 = ? AND reverted_at IS NULL)";
  await db.batch(insertUploadStatements(db, fields, guard, [fields.sha256]));
  const analysis = await analysisFor(db, user.id);
  const created = await uploadByUid(db, fields.uid);
  if (created) return json({ upload: summary(created, analysis), duplicate: false }, 201);
  const existing = await db
    .prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads WHERE sha256 = ? AND reverted_at IS NULL ORDER BY id LIMIT 1`)
    .bind(fields.sha256)
    .first();
  const visible = existing && canSee(user, existing);
  return json({ upload: visible ? summary(existing, analysis) : null, duplicate: true }, 200);
}

async function visibleUpload(db, user, id) {
  const row = id === null ? null : await uploadById(db, id);
  if (!row || !canSee(user, row)) throw notFound("No such upload.");
  return row;
}

function asciiFilename(name) {
  return name.replace(/[^\x20-\x7e]/g, "_").replace(/["\\]/g, "_");
}

export async function rawUpload(request, user, env, id) {
  const db = env.DB;
  const row = await visibleUpload(db, user, id);
  const bytes = await storedBytes(db, row.uid);
  const inline = new URL(request.url).searchParams.get("inline") === "1";
  const headers = {
    "Content-Type": "text/csv; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Length": String(bytes.byteLength),
  };
  if (!inline) {
    headers["Content-Disposition"] = `attachment; filename="${asciiFilename(row.filename)}"; filename*=UTF-8''${encodeURIComponent(row.filename)}`;
  }
  if (row.encoding === "gzip") {
    headers["Content-Encoding"] = "gzip";
    return new Response(bytes, { status: 200, headers, encodeBody: "manual" });
  }
  return new Response(bytes, { status: 200, headers });
}

async function respondWithUpload(db, user, id) {
  const analysis = await analysisFor(db, user.id);
  return json({ upload: summary(await uploadById(db, id), analysis) });
}

export async function revertUpload(user, env, id) {
  const db = env.DB;
  await visibleUpload(db, user, id);
  const active = "EXISTS (SELECT 1 FROM uploads WHERE id = ? AND reverted_at IS NULL)";
  await db.batch([
    bumpWhere(db, active, id),
    db
      .prepare(`UPDATE uploads SET reverted_at = ?, reverted_by = ?, changed_version = ${LEDGER_VERSION} WHERE id = ? AND reverted_at IS NULL`)
      .bind(nowIso(), user.username, id),
  ]);
  return respondWithUpload(db, user, id);
}

export async function restoreUpload(user, env, id) {
  const db = env.DB;
  await visibleUpload(db, user, id);
  const free = "NOT EXISTS (SELECT 1 FROM uploads o WHERE o.sha256 = u.sha256 AND o.reverted_at IS NULL)";
  await db.batch([
    bumpWhere(db, `EXISTS (SELECT 1 FROM uploads u WHERE u.id = ? AND u.reverted_at IS NOT NULL AND ${free})`, id),
    db
      .prepare(
        `UPDATE uploads AS u SET reverted_at = NULL, reverted_by = NULL, changed_version = ${LEDGER_VERSION}
         WHERE u.id = ? AND u.reverted_at IS NOT NULL AND ${free}`,
      )
      .bind(id),
  ]);
  const row = await uploadById(db, id);
  if (row.reverted_at !== null) throw new HttpError(409, "Another active upload has the same file. Revert it first.");
  return respondWithUpload(db, user, id);
}

export async function replaceUpload(request, user, env, id) {
  const db = env.DB;
  const body = await readJson(request, SMALL_JSON_LIMIT);
  if (typeof body.on !== "boolean") throw bad("on must be true or false.");
  await visibleUpload(db, user, id);
  const value = body.on ? 1 : 0;
  await db.batch([
    bumpWhere(db, "EXISTS (SELECT 1 FROM uploads WHERE id = ? AND replace_stored != ?)", id, value),
    db
      .prepare(`UPDATE uploads SET replace_stored = ?, changed_version = ${LEDGER_VERSION} WHERE id = ? AND replace_stored != ?`)
      .bind(value, id, value),
  ]);
  return respondWithUpload(db, user, id);
}
