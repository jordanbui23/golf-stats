import { ApiError, api, el, fmtTime, header, joinNames, loadMe, message, plural } from "./common.js";
import { showShotsButton } from "./shots.js";
import { uploadFiles } from "./upload.js";

function stateText(u) {
  if (u.reverted_at) return "Reverted";
  if (u.result === null) return "New";
  return "Restored or values setting changed";
}

function pendingItem(u, refresh, status) {
  const shots = el("div", { className: "shots-slot" });
  const undo = el("button", {
    type: "button",
    text: "Undo",
    onClick: async () => {
      undo.disabled = true;
      try {
        await api(`/api/uploads/${u.id}/${u.reverted_at ? "restore" : "revert"}`, { method: "POST" });
        await refresh();
      } catch (err) {
        message(status, err instanceof ApiError ? err.message : "Undo failed.", "error");
        undo.disabled = false;
      }
    },
  });
  const players = u.players.length ? joinNames(u.players) : "No player names";
  return el(
    "li",
    { className: "item" },
    el("div", { className: "row" },
      el("div", { className: "main" },
        el("div", { className: "file", text: u.filename }),
        el("div", { className: "meta", text: `${u.shots === null ? "Unknown shot count" : plural(u.shots, "shot")}. ${players}. ${fmtTime(u.uploaded_at)}.` }),
      ),
      el("span", { className: `state ${u.reverted_at ? "reverted" : ""}`, text: stateText(u) }),
      el("div", { className: "actions" }, showShotsButton(u.id, shots), undo),
    ),
    shots,
  );
}

let fitted = null;

function fitFrame(frame, top) {
  if (fitted) window.removeEventListener("resize", fitted);
  fitted = () => {
    frame.style.height = `${Math.max(320, window.innerHeight - top.offsetHeight)}px`;
  };
  fitted();
  window.addEventListener("resize", fitted);
}

async function start() {
  const me = await loadMe();
  if (!me) return;
  const app = document.getElementById("app");
  const status = el("div", { className: "status", role: "status", "aria-live": "polite" });
  const summary = el("section", { className: "summary" });
  const frameSlot = el("section", { className: "frame-slot" });
  const picker = el("input", { type: "file", accept: ".csv,text/csv", multiple: true, className: "hidden" });
  const uploadButton = el("button", { type: "button", className: "primary", text: "Upload", onClick: () => picker.click() });
  const top = header(me, [uploadButton, el("a", { href: "/uploads", text: "Uploads" })]);
  app.replaceChildren(top, el("main", {}, status, summary), frameSlot, picker);

  let frameFor = null;
  const refresh = async () => {
    let data;
    try {
      data = await api("/api/dashboard");
    } catch (err) {
      message(summary, err.message, "error");
      return;
    }
    const parts = [];
    if (data.analysis) {
      parts.push(el("p", { className: "meta", text: `Analysis from ${fmtTime(data.analysis.published_at)}.` }));
      if (data.analysis.sessions === 0) parts.push(el("p", { text: "The last analysis found no sessions for you yet." }));
    } else {
      parts.push(el("p", { text: "Your uploads are saved. The stats appear after the next analysis run." }));
    }
    if (data.pending.length) {
      parts.push(
        el("h2", { text: `Waiting for the next analysis (${data.pending.length})` }),
        el("ul", { className: "list" }, data.pending.map((u) => pendingItem(u, refresh, status))),
      );
    }
    summary.replaceChildren(...parts);
    const publishedAt = data.analysis && data.analysis.sessions > 0 ? data.analysis.published_at : null;
    if (publishedAt && publishedAt !== frameFor) {
      const frame = el("iframe", { sandbox: "allow-scripts", className: "dashboard", title: "Dashboard", src: "/api/dashboard/html" });
      frameSlot.replaceChildren(frame);
      fitFrame(frame, top);
    } else if (!publishedAt && frameFor) {
      frameSlot.replaceChildren();
    }
    frameFor = publishedAt;
  };

  picker.addEventListener("change", async () => {
    const files = Array.from(picker.files || []);
    picker.value = "";
    if (!files.length) return;
    uploadButton.disabled = true;
    try {
      await uploadFiles(files, me, status, refresh);
    } finally {
      uploadButton.disabled = false;
    }
  });

  await refresh();
}

start().catch(() => {
  const app = document.getElementById("app");
  app.replaceChildren(el("p", { className: "msg error", text: "The page could not be loaded. Reload to try again." }));
});
