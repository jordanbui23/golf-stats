import { test } from "node:test";
import assert from "node:assert/strict";
import { detectDelimiter, displayValue, parseExport, parseRows, previewTable, stripBom } from "../public/js/csv.js";
import { fixture } from "./helpers.mjs";

const decode = (bytes) => new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes);

test("a synthetic TrackMan export parses players, shots, first and last", () => {
  const parsed = parseExport(decode(fixture("jordan.csv")));
  assert.equal(parsed.ok, true);
  assert.equal(parsed.delimiter, ",");
  assert.deepEqual(parsed.players, ["Jordan"]);
  assert.equal(parsed.shotCount, 20);
  assert.equal(parsed.first_shot, "10/1/2026 6:00:00 PM");
  assert.match(parsed.last_shot, /^10\/1\/2026 /);
  assert.equal(parsed.units[parsed.column("Ball Speed")], "mph");
});

test("leading BOMs are stripped, including a doubled one", () => {
  assert.equal(stripBom("\ufeff\ufeffsep=,"), "sep=,");
  const parsed = parseExport("\ufeff\ufeffsep=,\nDate,Player,Club,Ball Speed\n1/1/2026 1:00:00 PM,A,Driver,150\n");
  assert.equal(parsed.ok, true);
  assert.equal(parsed.shotCount, 1);
});

test("the sep line names the delimiter and is skipped", () => {
  assert.deepEqual(detectDelimiter("sep=;\na;b"), { delimiter: ";", skipFirstLine: true });
  const parsed = parseExport("sep=;\nDate;Player;Club;Club Speed\n1/1/2026;A;7 Iron;80,5\n");
  assert.equal(parsed.delimiter, ";");
  assert.equal(parsed.shotCount, 1);
  assert.equal(displayValue("80,5", ";"), "80.5");
});

test("without a sep line, semicolons win only when they outnumber commas on the first line", () => {
  assert.equal(detectDelimiter("Date;Player;Club;Ball Speed").delimiter, ";");
  assert.equal(detectDelimiter("Date,Player;Club,Ball Speed").delimiter, ",");
});

test("RFC 4180 quoting handles delimiters, doubled quotes and newlines inside cells", () => {
  const rows = parseRows('a,"b,c","say ""hi""","line\r\nbreak"\r\nx,y,,z\r\n', ",");
  assert.deepEqual(rows, [["a", "b,c", 'say "hi"', "line\r\nbreak"], ["x", "y", "", "z"]]);
});

test("the header is found in the first 10 rows, matched trimmed, case-insensitive, without a unit suffix", () => {
  const text = "Report\nnotes\n date , player ,CLUB, Ball Speed [mph]\n1/1/2026,A,Driver,150\n";
  const parsed = parseExport(text);
  assert.equal(parsed.ok, true);
  assert.equal(parsed.units[parsed.column("Ball Speed")], "mph");
  assert.equal(parsed.shotCount, 1);
  const late = Array.from({ length: 10 }, (_, i) => `x${i}`).join("\n") + "\nDate,Club,Ball Speed\n1/1,Driver,1\n";
  assert.equal(parseExport(late).ok, false);
});

test("a units row is recognised only when every non-empty cell is bracketed", () => {
  const withUnits = parseExport("Date,Player,Club,Ball Speed\n,,,[km/h]\n1/1,A,Driver,150\n");
  assert.equal(withUnits.units[3], "km/h");
  assert.equal(withUnits.shotCount, 1);
  const without = parseExport("Date,Player,Club,Ball Speed\n,,x,[km/h]\n1/1,A,Driver,150\n");
  assert.equal(without.units[3], "");
});

test("rows without both Date and Club are not shots, and players are unique in file order", () => {
  const parsed = parseExport("Date,Player,Club,Ball Speed\n1/1,Bea,Driver,1\n,Bea,Driver,2\n1/2,,,3\n1/3,al,7 Iron,4\n1/4,BEA,7 Iron,5\n");
  assert.equal(parsed.shotCount, 3);
  assert.deepEqual(parsed.players, ["Bea", "al"]);
  assert.equal(parsed.first_shot, "1/1");
  assert.equal(parsed.last_shot, "1/4");
});

test("a file that is not a TrackMan export is refused", () => {
  assert.equal(parseExport("name,age\nbob,4\n").ok, false);
  assert.equal(parseExport("").ok, false);
  assert.equal(parseExport("Club,Carry\nDriver,200\n").ok, false);
});

test("the preview keeps present columns with their units and rounds numbers to one decimal", () => {
  const parsed = parseExport(decode(fixture("jordan.csv")));
  const table = previewTable(parsed);
  assert.deepEqual(table.columns.map((c) => c.name), [
    "Date", "Player", "Club", "Club Speed", "Ball Speed", "Carry Flat - Length", "Carry Flat - Side",
    "Launch Direction", "Face Angle", "Club Path", "Face To Path", "Spin Rate",
  ]);
  assert.equal(table.columns[3].unit, "mph");
  assert.equal(table.rows.length, 20);
  assert.match(table.rows[0][3], /^\d+\.\d$/);
  assert.equal(table.rows[0][0], "10/1/2026 6:00:00 PM");
  const minimal = previewTable(parseExport("Date,Club,Ball Speed\n1/1,Driver,150.26\n"));
  assert.deepEqual(minimal.columns.map((c) => c.name), ["Date", "Club", "Ball Speed"]);
  assert.deepEqual(minimal.rows, [["1/1", "Driver", "150.3"]]);
  assert.equal(displayValue("Measured", ","), "Measured");
});
