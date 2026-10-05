export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", json, body } = {}) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers["Content-Type"] = "application/json";
  } else if (body !== undefined) {
    init.body = body;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "The site could not be reached. Check the connection and try again.");
  }
  if (res.status === 204) return null;
  let data = null;
  try {
    data = await res.json();
  } catch {}
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/login")) {
      location.replace("/login");
    }
    throw new ApiError(res.status, (data && data.error) || `The request failed with status ${res.status}.`);
  }
  return data;
}

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function fmtDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

export function joinNames(names) {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export async function loadMe() {
  const res = await fetch("/api/me", { credentials: "same-origin" });
  if (res.status === 401) {
    location.replace("/login");
    return null;
  }
  if (!res.ok) throw new ApiError(res.status, "Your account could not be loaded.");
  return res.json();
}

export function header(me, links, tabs = null) {
  const signOut = el("button", {
    type: "button",
    className: "link",
    text: "Sign out",
    onClick: async () => {
      try {
        await api("/api/logout", { method: "POST" });
      } catch {}
      location.replace("/login");
    },
  });
  return el(
    "header",
    { className: "top" },
    el("a", { href: "/", className: "brand" }, "Golf ", el("span", { text: "stats" })),
    el("span", { className: "who", text: me.display_name }),
    tabs,
    el("nav", {}, ...links, signOut),
  );
}

export function message(node, text, kind = "info") {
  clear(node);
  if (text) node.append(el("p", { className: `msg ${kind}`, text }));
}
