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

function drillPart(drill) {
  if (typeof drill === "string") return el("dd", { text: drill });
  return el("dd", { className: "drill" },
    el("div", { className: "drill-name", text: drill.name }),
    el("div", {}, el("span", { className: "sub", text: "Set up " }), drill.setup),
    el("div", {}, el("span", { className: "sub", text: "Do " }), drill.reps),
    el("div", {}, el("span", { className: "sub", text: "Done when " }), drill.pass),
  );
}

function insightCard(insight, open) {
  const items = insight.items.map((item) =>
    el(
      "li",
      {},
      el("div", { className: "title", text: item.title }),
      el("dl", {},
        el("dt", { text: "Why" }), el("dd", { text: item.why }),
        el("dt", { text: "Drill" }), drillPart(item.drill),
        el("dt", { text: "Target" }), el("dd", { text: item.target }),
      ),
    ),
  );
  const before = insight.before ? el("p", { className: "before" }, el("span", { className: "sub", text: "Before you hit " }), insight.before) : null;
  const when = `Analysed ${fmtTime(insight.created_at)}. Session ${insight.session_label}.`;
  if (!open) {
    return el("details", { className: "insight" },
      el("summary", {}, el("span", { className: "meta", text: when }), el("div", { className: "takeaway", text: insight.summary })),
      before,
      el("ol", { className: "steps" }, items),
    );
  }
  return el("article", { className: "insight card" },
    el("p", { className: "meta", text: when }),
    el("p", { className: "takeaway", text: insight.summary }),
    before,
    el("ol", { className: "steps" }, items),
  );
}

function insightsSection(insights) {
  if (!insights.length) return [];
  const [latest, ...earlier] = insights;
  const parts = [insightCard(latest, true)];
  if (earlier.length) parts.push(el("h2", { text: "Earlier insights" }), el("div", { className: "earlier" }, earlier.map((i) => insightCard(i, false))));
  return parts;
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

const TABS = [["dashboard", "Dashboard"], ["insights", "Insights"]];

function tabFromHash() {
  return location.hash === "#insights" ? "insights" : "dashboard";
}

function tabView(panels) {
  let wanted = tabFromHash();
  let available = false;
  const buttons = {};
  const bar = el("div", { className: "tabs", role: "tablist", "aria-label": "View", hidden: true });
  const current = () => (available ? wanted : "dashboard");
  const show = () => {
    bar.hidden = !available;
    for (const [name] of TABS) {
      const on = name === current();
      buttons[name].setAttribute("aria-selected", String(on));
      buttons[name].tabIndex = on ? 0 : -1;
      panels[name].hidden = !on;
    }
    if (fitted) fitted();
  };
  const select = (name) => {
    wanted = name;
    history.replaceState(null, "", name === "insights" ? "#insights" : location.pathname + location.search);
    show();
  };
  for (const [name, label] of TABS) {
    panels[name].id = `panel-${name}`;
    panels[name].setAttribute("role", "tabpanel");
    panels[name].setAttribute("aria-labelledby", `tab-${name}`);
    buttons[name] = el("button", { type: "button", role: "tab", id: `tab-${name}`, "aria-controls": `panel-${name}`, text: label, onClick: () => select(name) });
    bar.append(buttons[name]);
  }
  bar.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = current() === "dashboard" ? "insights" : "dashboard";
    select(next);
    buttons[next].focus();
  });
  window.addEventListener("hashchange", () => {
    wanted = tabFromHash();
    show();
  });
  show();
  return {
    bar,
    select,
    setAvailable(value) {
      available = value;
      show();
    },
  };
}

async function start() {
  const me = await loadMe();
  if (!me) return;
  const app = document.getElementById("app");
  const status = el("div", { className: "status", role: "status", "aria-live": "polite" });
  const summary = el("section", { className: "summary" });
  const insightsSlot = el("section", { className: "insights" });
  const frameSlot = el("section", { className: "frame-slot" });
  const panels = {
    dashboard: el("div", {}, el("main", {}, status, summary), frameSlot),
    insights: el("main", {}, insightsSlot),
  };
  const tabs = tabView(panels);
  const picker = el("input", { type: "file", accept: ".csv,text/csv", multiple: true, className: "hidden" });
  const uploadButton = el("button", { type: "button", className: "primary", text: "Upload", onClick: () => picker.click() });
  const top = header(me, [uploadButton, el("a", { href: "/uploads", text: "Uploads" })], tabs.bar);
  app.replaceChildren(top, panels.dashboard, panels.insights, picker);

  let frameFor = null;
  const refresh = async () => {
    let data;
    let insights;
    try {
      [data, insights] = await Promise.all([api("/api/dashboard"), api("/api/insights").catch(() => ({ insights: [] }))]);
    } catch (err) {
      message(summary, err.message, "error");
      return;
    }
    insightsSlot.replaceChildren(...insightsSection(insights.insights));
    tabs.setAvailable(insights.insights.length > 0);
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
    tabs.select("dashboard");
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
