export class HttpError extends Error {
  constructor(status, message, headers) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

export const bad = (message) => new HttpError(400, message);
export const notFound = (message = "Not found.") => new HttpError(404, message);

export const BASE_HEADERS = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
};

export function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...BASE_HEADERS, ...extra },
  });
}

export function empty(status = 204, extra = {}) {
  return new Response(null, { status, headers: { ...BASE_HEADERS, ...extra } });
}

export function errorResponse(status, message, extra = {}) {
  return json({ error: message }, status, extra);
}

export async function readBody(request, limit) {
  const declared = request.headers.get("Content-Length");
  if (declared !== null) {
    if (!/^\d+$/.test(declared.trim())) throw bad("The request has an invalid Content-Length.");
    if (Number(declared) > limit) throw bad("The request body is too large.");
  }
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const parts = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel().catch(() => {});
      throw bad("The request body is too large.");
    }
    parts.push(value);
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.byteLength;
  }
  return out;
}

export async function readJson(request, limit) {
  const bytes = await readBody(request, limit);
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw bad("The request body is not valid UTF-8.");
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch {
    throw bad("The request body is not valid JSON.");
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw bad("The request body must be a JSON object.");
  return value;
}

export function parseJsonColumn(text, fallback) {
  if (typeof text !== "string") return fallback;
  try {
    return JSON.parse(text);
  } catch {
    return fallback;
  }
}

export function nowIso() {
  return new Date().toISOString();
}
