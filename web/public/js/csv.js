export const PREVIEW_COLUMNS = [
  "Date", "Player", "Club", "Club Speed", "Ball Speed", "Carry Flat - Length", "Carry Flat - Side",
  "Launch Direction", "Face Angle", "Club Path", "Face To Path", "Spin Rate",
];

export function stripBom(text) {
  let i = 0;
  while (text.charCodeAt(i) === 0xfeff) i++;
  return text.slice(i);
}

function firstLine(text) {
  const m = /\r\n|\n|\r/.exec(text);
  return m ? text.slice(0, m.index) : text;
}

function count(text, ch) {
  let n = 0;
  for (const c of text) if (c === ch) n++;
  return n;
}

export function detectDelimiter(text) {
  const line = firstLine(text);
  if (line.startsWith("sep=")) {
    const named = line.slice(4);
    return { delimiter: named.length > 0 ? named[0] : ",", skipFirstLine: true };
  }
  return { delimiter: count(line, ";") > count(line, ",") ? ";" : ",", skipFirstLine: false };
}

export function parseRows(text, delimiter) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  let i = 0;
  const n = text.length;
  while (i < n) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i += 2;
          continue;
        }
        quoted = false;
        i++;
        continue;
      }
      cell += c;
      i++;
      continue;
    }
    if (c === '"' && cell === "") {
      quoted = true;
      i++;
    } else if (c === delimiter) {
      row.push(cell);
      cell = "";
      i++;
    } else if (c === "\r" || c === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      i += c === "\r" && text[i + 1] === "\n" ? 2 : 1;
    } else {
      cell += c;
      i++;
    }
  }
  if (cell !== "" || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export function headerName(cell) {
  return cell.trim().replace(/\s*\[[^\]]*\]$/, "").trim().toLowerCase();
}

function headerUnit(cell) {
  const m = /\[([^\]]*)\]\s*$/.exec(cell.trim());
  return m ? m[1] : "";
}

function isHeader(row) {
  const names = new Set(row.map(headerName));
  return names.has("club") && (names.has("ball speed") || names.has("club speed"));
}

function isUnitsRow(row) {
  const cells = row.map((c) => c.trim()).filter((c) => c !== "");
  return cells.length > 0 && cells.every((c) => /^\[.*\]$/.test(c));
}

export function parseExport(input) {
  const text = stripBom(input);
  const { delimiter, skipFirstLine } = detectDelimiter(text);
  const all = parseRows(text, delimiter);
  const rows = skipFirstLine ? all.slice(1) : all;
  const headerIndex = rows.slice(0, 10).findIndex(isHeader);
  if (headerIndex < 0) return { ok: false, error: "This does not look like a TrackMan export. No header row with Club and Ball Speed or Club Speed was found." };
  const header = rows[headerIndex].map((c) => c.trim());
  const keys = header.map(headerName);
  let units = header.map(headerUnit);
  let start = headerIndex + 1;
  if (rows[start] && isUnitsRow(rows[start])) {
    units = header.map((_, i) => {
      const cell = (rows[start][i] || "").trim();
      return cell ? cell.slice(1, -1) : units[i];
    });
    start++;
  }
  const index = (name) => keys.indexOf(name.toLowerCase());
  const dateCol = index("date");
  const clubCol = index("club");
  const playerCol = index("player");
  const shots = [];
  for (const row of rows.slice(start)) {
    const date = dateCol >= 0 ? (row[dateCol] || "").trim() : "";
    const club = (row[clubCol] || "").trim();
    if (date && club) shots.push(row);
  }
  const players = [];
  const seen = new Set();
  for (const row of shots) {
    const p = playerCol >= 0 ? (row[playerCol] || "").trim() : "";
    if (p && !seen.has(p.toLowerCase())) {
      seen.add(p.toLowerCase());
      players.push(p);
    }
  }
  return {
    ok: true,
    delimiter,
    header,
    units,
    shots,
    players,
    shotCount: shots.length,
    first_shot: shots.length ? shots[0][dateCol].trim() : null,
    last_shot: shots.length ? shots[shots.length - 1][dateCol].trim() : null,
    column: index,
  };
}

export function displayValue(cell, delimiter) {
  const value = (cell ?? "").trim();
  const decimal = delimiter === "," ? /^[-+]?\d+(\.\d+)?$/ : /^[-+]?\d+([.,]\d+)?$/;
  if (!decimal.test(value)) return value;
  const n = Number(value.replace(",", "."));
  return Number.isFinite(n) ? n.toFixed(1) : value;
}

export function previewTable(parsed) {
  const columns = PREVIEW_COLUMNS.map((name) => ({ name, index: parsed.column(name) })).filter((c) => c.index >= 0);
  const textColumns = new Set(["date", "player", "club"]);
  return {
    columns: columns.map((c) => ({ name: c.name, unit: parsed.units[c.index] || "" })),
    rows: parsed.shots.map((row) =>
      columns.map((c) => (textColumns.has(c.name.toLowerCase()) ? (row[c.index] || "").trim() : displayValue(row[c.index], parsed.delimiter))),
    ),
  };
}
