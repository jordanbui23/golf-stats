import { parseJsonColumn } from "./http.js";

export const CHUNK_BYTES = 1_000_000;

export const UPLOAD_COLUMNS =
  "id, uid, sha256, filename, size, encoding, uploaded_at, uploaded_by, players, shots, first_shot, last_shot, replace_stored, reverted_at, reverted_by, changed_version, result";

export const LEDGER_VERSION = "(SELECT value FROM meta WHERE key = 'ledger_version')";

export function bumpWhere(db, condition, ...params) {
  return db.prepare(`UPDATE meta SET value = value + 1 WHERE key = 'ledger_version' AND ${condition}`).bind(...params);
}

export async function ledgerVersion(db) {
  return db.prepare("SELECT value FROM meta WHERE key = 'ledger_version'").first("value");
}

export function parsePlayers(text) {
  const value = parseJsonColumn(text, []);
  return Array.isArray(value) ? value.filter((p) => typeof p === "string") : [];
}

export function canSee(user, row) {
  if (String(row.uploaded_by).toLowerCase() === user.username.toLowerCase()) return true;
  const mine = new Set(user.players.map((p) => p.toLowerCase()));
  return parsePlayers(row.players).some((p) => mine.has(p.toLowerCase()));
}

export function isPending(row, analysis) {
  return analysis === null || row.changed_version > analysis.based_on_version;
}

export function summary(row, analysis) {
  return {
    id: row.id,
    filename: row.filename,
    size: row.size,
    uploaded_at: row.uploaded_at,
    uploaded_by: row.uploaded_by,
    players: parsePlayers(row.players),
    shots: row.shots,
    first_shot: row.first_shot,
    last_shot: row.last_shot,
    replace_stored: row.replace_stored,
    reverted_at: row.reverted_at,
    reverted_by: row.reverted_by,
    changed_version: row.changed_version,
    result: parseJsonColumn(row.result, null),
    pending: isPending(row, analysis),
  };
}

export function syncItem(row) {
  return {
    id: row.id,
    uid: row.uid,
    sha256: row.sha256,
    filename: row.filename,
    size: row.size,
    encoding: row.encoding,
    uploaded_at: row.uploaded_at,
    uploaded_by: row.uploaded_by,
    replace_stored: row.replace_stored,
    reverted_at: row.reverted_at,
    changed_version: row.changed_version,
  };
}

export async function analysisFor(db, userId) {
  return db.prepare("SELECT based_on_version, published_at, sessions FROM analyses WHERE user_id = ?").bind(userId).first();
}

export async function uploadById(db, id) {
  return db.prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads WHERE id = ?`).bind(id).first();
}

export async function uploadByUid(db, uid) {
  return db.prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads WHERE uid = ?`).bind(uid).first();
}

export async function storedBytes(db, uid) {
  const { results } = await db.prepare("SELECT data FROM upload_chunks WHERE upload_uid = ? ORDER BY seq").bind(uid).all();
  const parts = results.map((r) => new Uint8Array(r.data));
  const total = parts.reduce((n, p) => n + p.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.byteLength;
  }
  return out;
}

export function insertUploadStatements(db, fields, guard, guardParams) {
  const statements = [
    bumpWhere(db, guard, ...guardParams),
    db
      .prepare(
        `INSERT INTO uploads (uid, sha256, filename, size, encoding, uploaded_at, uploaded_by, players, shots, first_shot, last_shot, replace_stored, changed_version)
         SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ${LEDGER_VERSION} WHERE ${guard}`,
      )
      .bind(
        fields.uid,
        fields.sha256,
        fields.filename,
        fields.size,
        fields.encoding,
        fields.uploaded_at,
        fields.uploaded_by,
        JSON.stringify(fields.players),
        fields.shots,
        fields.first_shot,
        fields.last_shot,
        fields.replace_stored ? 1 : 0,
        ...guardParams,
      ),
  ];
  const bytes = fields.bytes;
  for (let seq = 0, offset = 0; offset < bytes.byteLength; seq++, offset += CHUNK_BYTES) {
    statements.push(
      db
        .prepare("INSERT INTO upload_chunks (upload_uid, seq, data) SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM uploads WHERE uid = ?)")
        .bind(fields.uid, seq, bytes.slice(offset, offset + CHUNK_BYTES).buffer, fields.uid),
    );
  }
  return statements;
}
