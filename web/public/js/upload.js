import { parseExport } from "./csv.js";
import { ApiError, api, el, fmtDate, joinNames, plural } from "./common.js";

const STORED_MAX = 16_000_000;

function hex(buffer) {
  return Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, "0")).join("");
}

function confirmInPage(container, text) {
  return new Promise((resolve) => {
    const done = (answer) => {
      box.remove();
      resolve(answer);
    };
    const box = el(
      "div",
      { className: "confirm", role: "alertdialog", "aria-label": text },
      el("p", { text }),
      el("div", { className: "actions" },
        el("button", { type: "button", className: "primary", text: "Upload anyway", onClick: () => done(true) }),
        el("button", { type: "button", text: "Skip this file", onClick: () => done(false) }),
      ),
    );
    container.append(box);
    box.querySelector("button").focus();
  });
}

function line(container, text, kind) {
  const p = el("p", { className: `msg ${kind}`, text });
  container.append(p);
  return p;
}

async function uploadOne(file, me, container) {
  const name = file.name || "upload.csv";
  if (file.size > STORED_MAX) return line(container, `${name} is larger than 16 MB, so it was not uploaded.`, "error");
  const progress = line(container, `Reading ${name}.`, "info");
  const original = new Uint8Array(await file.arrayBuffer());
  const sha256 = hex(await crypto.subtle.digest("SHA-256", original));
  const parsed = parseExport(new TextDecoder("utf-8").decode(original));
  if (!parsed.ok || parsed.shotCount === 0) {
    progress.remove();
    return line(container, `${name} is not a TrackMan export with shots, so it was not uploaded.`, "error");
  }
  const mine = new Set(me.players.map((p) => p.toLowerCase()));
  if (!parsed.players.some((p) => mine.has(p.toLowerCase()))) {
    progress.remove();
    const who = parsed.players.length ? joinNames(parsed.players) : "no named player";
    const ok = await confirmInPage(container, `This file has shots for ${who}, not you. Upload anyway?`);
    if (!ok) return line(container, `${name} was skipped.`, "info");
    container.append(progress);
  }
  progress.textContent = `Uploading ${name}.`;
  const form = new FormData();
  form.set("file", new Blob([original]), name);
  form.set("encoding", "identity");
  form.set("sha256", sha256);
  form.set("size", String(original.byteLength));
  form.set("filename", name.slice(0, 200));
  form.set("players", JSON.stringify(parsed.players.slice(0, 50).map((p) => p.slice(0, 100))));
  form.set("shots", String(parsed.shotCount));
  if (parsed.first_shot) form.set("first_shot", parsed.first_shot.slice(0, 100));
  if (parsed.last_shot) form.set("last_shot", parsed.last_shot.slice(0, 100));
  try {
    const data = await api("/api/uploads", { method: "POST", body: form });
    progress.remove();
    if (!data.duplicate) return line(container, `${name} is uploaded with ${plural(parsed.shotCount, "shot")}.`, "ok");
    if (data.upload) return line(container, `${name} was already uploaded on ${fmtDate(data.upload.uploaded_at)}.`, "info");
    return line(container, `${name} was already uploaded by someone else.`, "info");
  } catch (err) {
    progress.remove();
    return line(container, `${name} was not uploaded. ${err instanceof ApiError ? err.message : "Something went wrong."}`, "error");
  }
}

export async function uploadFiles(files, me, container, onDone) {
  container.replaceChildren();
  for (const file of files) {
    try {
      await uploadOne(file, me, container);
    } catch {
      line(container, `${file.name} could not be read.`, "error");
    }
  }
  if (onDone) await onDone();
}
