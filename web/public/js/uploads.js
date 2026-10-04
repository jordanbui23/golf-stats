import { ApiError, api, el, fmtTime, header, joinNames, loadMe, message, plural } from "./common.js";

function resultText(result) {
  if (!result) return ["Not analysed yet."];
  if (!result.ok) return [`The analysis could not read this file${result.error ? `: ${result.error}` : "."}`];
  const lines = [];
  if (Number.isInteger(result.shots_used) && Number.isInteger(result.shots_in_file)) {
    lines.push(`Uses ${result.shots_used} of ${plural(result.shots_in_file, "shot")}.`);
  }
  if (result.conflicts > 0) lines.push(`${plural(result.conflicts, "shot")} differ from the values another upload stored.`);
  return lines;
}

function toggle(u, onChange) {
  const make = (on, text) =>
    el("button", {
      type: "button",
      className: u.replace_stored === (on ? 1 : 0) ? "seg on" : "seg",
      "aria-pressed": u.replace_stored === (on ? 1 : 0) ? "true" : "false",
      text,
      onClick: () => {
        if (u.replace_stored !== (on ? 1 : 0)) onChange(on);
      },
    });
  return el("div", { className: "choice" },
    el("p", { className: "hint", text: "For shots that differ from another upload:" }),
    el("div", { className: "segmented", role: "group", "aria-label": "Values for shots that differ from another upload" },
      make(false, "Keep stored values"),
      make(true, "Use this file's values"),
    ),
  );
}

function card(u, act) {
  const result = u.result;
  const state = u.reverted_at
    ? `Reverted${u.reverted_by ? ` by ${u.reverted_by}` : ""} on ${fmtTime(u.reverted_at)}`
    : "Active";
  const warnings = result && Array.isArray(result.warnings) && result.warnings.length
    ? el("ul", { className: "warnings" }, result.warnings.map((w) => el("li", { text: String(w) })))
    : null;
  return el(
    "li",
    { className: `item ${u.reverted_at ? "is-reverted" : ""}` },
    el("div", { className: "row" },
      el("div", { className: "main" },
        el("div", { className: "file", text: u.filename }),
        el("div", { className: "meta", text: `${fmtTime(u.uploaded_at)}. Uploaded by ${u.uploaded_by}.` }),
        el("div", { className: "meta", text: `${u.players.length ? joinNames(u.players) : "No player names"}. ${u.shots === null ? "Unknown shot count" : plural(u.shots, "shot")}.` }),
      ),
      el("span", { className: `state ${u.reverted_at ? "reverted" : ""}`, text: state }),
    ),
    el("div", { className: "result" },
      resultText(result).map((t) => el("p", { text: t })),
      warnings,
      u.pending ? el("p", { className: "hint", text: "Waiting for the next analysis." }) : null,
    ),
    result && result.conflicts > 0 ? toggle(u, (on) => act(`/api/uploads/${u.id}/replace`, { on })) : null,
    el("div", { className: "actions" },
      el("button", {
        type: "button",
        text: u.reverted_at ? "Restore" : "Revert",
        onClick: () => act(`/api/uploads/${u.id}/${u.reverted_at ? "restore" : "revert"}`),
      }),
      el("a", { className: "button", href: `/api/uploads/${u.id}/raw`, text: "Download" }),
    ),
  );
}

async function start() {
  const me = await loadMe();
  if (!me) return;
  const app = document.getElementById("app");
  const status = el("div", { className: "status", role: "status", "aria-live": "polite" });
  const list = el("ul", { className: "list" });
  app.replaceChildren(header(me, [el("a", { href: "/", text: "Home" })]), el("main", {}, el("h2", { text: "Uploads" }), status, list));

  let busy = false;
  const act = async (path, json) => {
    if (busy) return;
    busy = true;
    try {
      await api(path, { method: "POST", json });
      message(status, "");
      await refresh();
    } catch (err) {
      message(status, err instanceof ApiError ? err.message : "That did not work.", "error");
    } finally {
      busy = false;
    }
  };

  const refresh = async () => {
    const data = await api("/api/uploads");
    if (!data.uploads.length) {
      list.replaceChildren(el("li", { className: "hint", text: "No uploads yet." }));
      return;
    }
    list.replaceChildren(...data.uploads.map((u) => card(u, act)));
  };

  try {
    await refresh();
  } catch (err) {
    message(status, err.message, "error");
  }
}

start().catch(() => {
  document.getElementById("app").replaceChildren(el("p", { className: "msg error", text: "The page could not be loaded. Reload to try again." }));
});
