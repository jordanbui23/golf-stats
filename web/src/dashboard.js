import { json, notFound } from "./http.js";
import { UPLOAD_COLUMNS, analysisFor, canSee, isPending, ledgerVersion, summary } from "./db.js";

export const DASHBOARD_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'";

export async function dashboard(user, env) {
  const db = env.DB;
  const [version, analysis, { results }] = await Promise.all([
    ledgerVersion(db),
    analysisFor(db, user.id),
    db.prepare(`SELECT ${UPLOAD_COLUMNS} FROM uploads ORDER BY id DESC`).all(),
  ]);
  const pending = results.filter((r) => canSee(user, r) && isPending(r, analysis)).map((r) => summary(r, analysis));
  return json({
    ledger_version: version,
    analysis: analysis ? { published_at: analysis.published_at, based_on_version: analysis.based_on_version, sessions: analysis.sessions } : null,
    pending,
  });
}

export async function dashboardHtml(user, env) {
  const row = await env.DB.prepare("SELECT sessions, html_gz FROM analyses WHERE user_id = ?").bind(user.id).first();
  if (!row || row.sessions < 1 || row.html_gz === null) throw notFound("No dashboard has been published yet.");
  const bytes = new Uint8Array(row.html_gz);
  return new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Encoding": "gzip",
      "Content-Length": String(bytes.byteLength),
      "Content-Security-Policy": DASHBOARD_CSP,
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
    encodeBody: "manual",
  });
}
