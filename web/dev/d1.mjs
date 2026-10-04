import { DatabaseSync } from "node:sqlite";

function toParam(value, index) {
  if (value === undefined) throw new TypeError(`D1_TYPE_ERROR: Type 'undefined' not supported for value at index ${index}`);
  if (value === null) return null;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" || typeof value === "string" || typeof value === "bigint") return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  if (Array.isArray(value)) return Uint8Array.from(value);
  throw new TypeError(`D1_TYPE_ERROR: Type '${typeof value}' not supported for value at index ${index}`);
}

function toValue(value) {
  if (value instanceof Uint8Array) return Array.from(value);
  if (typeof value === "bigint") return Number(value);
  return value;
}

function toRow(row) {
  const out = {};
  for (const [k, v] of Object.entries(row)) out[k] = toValue(v);
  return out;
}

class D1PreparedStatement {
  constructor(db, sql, params = []) {
    this.db = db;
    this.sql = sql;
    this.params = params;
  }

  bind(...values) {
    return new D1PreparedStatement(this.db, this.sql, values.map(toParam));
  }

  _exec() {
    const stmt = this.db.db.prepare(this.sql);
    const started = performance.now();
    let rows = [];
    let changes = 0;
    let lastRowId = 0;
    if (stmt.columns().length > 0) {
      rows = stmt.all(...this.params).map(toRow);
      const info = this.db.db.prepare("SELECT changes() AS c, last_insert_rowid() AS r").get();
      changes = Number(info.c);
      lastRowId = Number(info.r);
    } else {
      const info = stmt.run(...this.params);
      changes = Number(info.changes);
      lastRowId = Number(info.lastInsertRowid);
    }
    return {
      results: rows,
      success: true,
      meta: { changes, last_row_id: lastRowId, duration: performance.now() - started, rows_read: rows.length, rows_written: changes, changed_db: changes > 0 },
    };
  }

  async first(column) {
    const { results } = this._exec();
    const row = results[0];
    if (row === undefined) return null;
    if (column === undefined) return row;
    if (!(column in row)) throw new Error(`D1_COLUMN_NOTFOUND: Column not found (${column})`);
    return row[column];
  }

  async all() {
    return this._exec();
  }

  async run() {
    return this._exec();
  }

  async raw(options = {}) {
    const stmt = this.db.db.prepare(this.sql);
    const names = stmt.columns().map((c) => c.name);
    if (names.length === 0) {
      stmt.run(...this.params);
      return options.columnNames ? [[]] : [];
    }
    const rows = stmt.all(...this.params).map((r) => names.map((n) => toValue(r[n])));
    return options.columnNames ? [names, ...rows] : rows;
  }
}

export class D1Database {
  constructor(path = ":memory:") {
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA foreign_keys = ON");
  }

  prepare(sql) {
    return new D1PreparedStatement(this, sql);
  }

  async batch(statements) {
    this.db.exec("BEGIN");
    try {
      const results = statements.map((s) => s._exec());
      this.db.exec("COMMIT");
      return results;
    } catch (err) {
      this.db.exec("ROLLBACK");
      throw err;
    }
  }

  async exec(sql) {
    this.db.exec(sql);
    return { count: 1, duration: 0 };
  }

  close() {
    this.db.close();
  }
}

export function openD1(path) {
  return new D1Database(path);
}
