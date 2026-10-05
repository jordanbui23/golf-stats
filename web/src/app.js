import { HttpError, errorResponse } from "./http.js";
import { checkSyncToken, login, logout, me, originAllowed, requireUser } from "./auth.js";
import { createUpload, listUploads, rawUpload, replaceUpload, restoreUpload, revertUpload } from "./uploads.js";
import { dashboard, dashboardHtml } from "./dashboard.js";
import { listInsights, syncInsight } from "./insights.js";
import { deleteUser, putUser, syncPublish, syncPush, syncRaw, syncState, syncUsers } from "./sync.js";
import { uploadIdParam } from "./validate.js";

function route(method, pattern, handler) {
  return { method, pattern: new RegExp(`^${pattern}$`), handler };
}

const withUser = (fn) => async (request, env, params) => fn(request, await requireUser(request, env), env, params);
const uploadId = (params) => uploadIdParam(params[0]);

const USER_ROUTES = [
  route("POST", "/api/login", (request, env) => login(request, env)),
  route("POST", "/api/logout", (request, env) => logout(request, env)),
  route("GET", "/api/me", (request, env) => me(request, env)),
  route("GET", "/api/dashboard", withUser((request, user, env) => dashboard(user, env))),
  route("GET", "/api/dashboard/html", withUser((request, user, env) => dashboardHtml(user, env))),
  route("GET", "/api/insights", withUser((request, user, env) => listInsights(user, env))),
  route("GET", "/api/uploads", withUser((request, user, env) => listUploads(user, env))),
  route("POST", "/api/uploads", withUser((request, user, env) => createUpload(request, user, env))),
  route("GET", "/api/uploads/([^/]+)/raw", withUser((request, user, env, p) => rawUpload(request, user, env, uploadId(p)))),
  route("POST", "/api/uploads/([^/]+)/revert", withUser((request, user, env, p) => revertUpload(user, env, uploadId(p)))),
  route("POST", "/api/uploads/([^/]+)/restore", withUser((request, user, env, p) => restoreUpload(user, env, uploadId(p)))),
  route("POST", "/api/uploads/([^/]+)/replace", withUser((request, user, env, p) => replaceUpload(request, user, env, uploadId(p)))),
];

const SYNC_ROUTES = [
  route("GET", "/api/sync/state", (request, env) => syncState(env)),
  route("GET", "/api/sync/uploads/([^/]+)/raw", (request, env, p) => syncRaw(env, uploadIdParam(p[0]))),
  route("POST", "/api/sync/uploads", (request, env) => syncPush(request, env)),
  route("POST", "/api/sync/publish", (request, env) => syncPublish(request, env)),
  route("POST", "/api/sync/insights", (request, env) => syncInsight(request, env)),
  route("GET", "/api/sync/users", (request, env) => syncUsers(env)),
  route("PUT", "/api/sync/users/([^/]+)", (request, env, p) => putUser(request, env, p[0])),
  route("DELETE", "/api/sync/users/([^/]+)", (request, env, p) => deleteUser(env, p[0])),
];

async function dispatch(routes, request, env, path) {
  const allowed = new Set();
  for (const r of routes) {
    const match = r.pattern.exec(path);
    if (!match) continue;
    if (r.method === request.method) return r.handler(request, env, match.slice(1));
    allowed.add(r.method);
  }
  if (allowed.size) throw new HttpError(405, "This method is not allowed here.", { Allow: [...allowed].join(", ") });
  throw new HttpError(404, "No such API route.");
}

export async function handle(request, env) {
  try {
    const path = new URL(request.url).pathname;
    if (path !== "/api" && !path.startsWith("/api/")) throw new HttpError(404, "No such API route.");
    if (path === "/api/sync" || path.startsWith("/api/sync/")) {
      await checkSyncToken(request, env);
      return await dispatch(SYNC_ROUTES, request, env, path);
    }
    if (request.method !== "GET" && !originAllowed(request)) throw new HttpError(403, "This request must come from the site itself.");
    return await dispatch(USER_ROUTES, request, env, path);
  } catch (err) {
    if (err instanceof HttpError) return errorResponse(err.status, err.message, err.headers);
    console.error(err);
    return errorResponse(500, "Something went wrong on the server.");
  }
}
