import { parseExport, previewTable } from "./csv.js";
import { ApiError, el } from "./common.js";

export function shotsTable(parsed) {
  const table = previewTable(parsed);
  const head = el("tr", {}, table.columns.map((c) => el("th", { scope: "col" }, c.name, c.unit ? el("small", { text: ` [${c.unit}]` }) : null)));
  const body = table.rows.map((row) => el("tr", {}, row.map((cell, i) => el("td", { className: i < 3 ? "text" : "", text: cell }))));
  return el("div", { className: "tablewrap" }, el("table", { className: "shots" }, el("thead", {}, head), el("tbody", {}, body)));
}

export async function loadShots(id) {
  let res;
  try {
    res = await fetch(`/api/uploads/${id}/raw?inline=1`, { credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "The file could not be downloaded.");
  }
  if (!res.ok) throw new ApiError(res.status, "The file could not be downloaded.");
  const text = new TextDecoder("utf-8").decode(await res.arrayBuffer());
  const parsed = parseExport(text);
  if (!parsed.ok) throw new ApiError(0, "This file could not be read for display.");
  return parsed;
}

export function showShotsButton(id, target) {
  let open = false;
  const button = el("button", { type: "button", text: "Show shots" });
  button.addEventListener("click", async () => {
    if (open) {
      target.replaceChildren();
      button.textContent = "Show shots";
      open = false;
      return;
    }
    button.disabled = true;
    try {
      const parsed = await loadShots(id);
      target.replaceChildren(parsed.shotCount ? shotsTable(parsed) : el("p", { className: "hint", text: "This file has no shots." }));
      button.textContent = "Hide shots";
      open = true;
    } catch (err) {
      target.replaceChildren(el("p", { className: "msg error", text: err.message }));
    } finally {
      button.disabled = false;
    }
  });
  return button;
}
