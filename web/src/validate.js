import { bad } from "./http.js";

export const LIMITS = {
  storedMax: 16_000_000,
  sizeMax: 64_000_000,
  filename: 200,
  players: 50,
  player: 100,
  shotText: 100,
  displayName: 100,
};

const CONTROL = /[\u0000-\u001f\u007f]/;

export function sha256Field(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) throw bad("sha256 must be 64 lowercase hex characters.");
  return value;
}

export function encodingField(value) {
  if (value !== "gzip" && value !== "identity") throw bad("encoding must be gzip or identity.");
  return value;
}

export function filenameField(value) {
  if (typeof value !== "string" || value.length < 1 || value.length > LIMITS.filename || CONTROL.test(value)) {
    throw bad(`filename must be 1 to ${LIMITS.filename} characters without control characters.`);
  }
  return value;
}

export function sizeField(value) {
  const n = typeof value === "string" && /^\d{1,9}$/.test(value) ? Number(value) : value;
  if (!Number.isSafeInteger(n) || n < 1 || n > LIMITS.sizeMax) throw bad(`size must be a whole number from 1 to ${LIMITS.sizeMax}.`);
  return n;
}

export function playersField(value, name = "players") {
  if (!Array.isArray(value) || value.length > LIMITS.players) throw bad(`${name} must be a list of at most ${LIMITS.players} names.`);
  for (const p of value) {
    if (typeof p !== "string" || p.length < 1 || p.length > LIMITS.player || CONTROL.test(p)) {
      throw bad(`Each name in ${name} must be 1 to ${LIMITS.player} characters.`);
    }
  }
  return value;
}

export function playersJsonField(text) {
  if (text === null || text === undefined || text === "") return [];
  if (typeof text !== "string") throw bad("players must be a JSON list of names.");
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw bad("players must be a JSON list of names.");
  }
  return playersField(value);
}

export function shotsField(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "string" && /^\d{1,9}$/.test(value) ? Number(value) : value;
  if (!Number.isSafeInteger(n) || n < 0) throw bad("shots must be a whole number.");
  return n;
}

export function shotTimeField(value, name) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value !== "string" || value.length > LIMITS.shotText || CONTROL.test(value)) {
    throw bad(`${name} must be text of at most ${LIMITS.shotText} characters.`);
  }
  return value;
}

export function storedBytesField(bytes, encoding, size) {
  if (bytes.byteLength < 1 || bytes.byteLength > LIMITS.storedMax) throw bad(`The stored file must be 1 to ${LIMITS.storedMax} bytes.`);
  if (encoding === "identity" && bytes.byteLength !== size) throw bad("size does not match the file length.");
  if (encoding === "gzip" && (bytes.byteLength < 2 || bytes[0] !== 0x1f || bytes[1] !== 0x8b)) throw bad("The file is not gzip data.");
  return bytes;
}

export function uploadIdParam(text) {
  return /^[1-9]\d{0,14}$/.test(text) ? Number(text) : null;
}
