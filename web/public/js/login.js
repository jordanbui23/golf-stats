import { ApiError, api, message } from "./common.js";

const ITERATIONS = 600000;

export async function deriveLoginKey(username, password) {
  const enc = new TextEncoder();
  const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: enc.encode("golf-stats:" + username.trim().toLowerCase()), iterations: ITERATIONS },
    base,
    256,
  );
  return Array.from(new Uint8Array(bits), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function start() {
  const res = await fetch("/api/me", { credentials: "same-origin" }).catch(() => null);
  if (res && res.ok) {
    location.replace("/");
    return;
  }
  const form = document.getElementById("login");
  const status = document.getElementById("status");
  const button = document.getElementById("submit");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = document.getElementById("username").value;
    const password = document.getElementById("password").value;
    const remember = document.getElementById("remember").checked;
    if (!crypto.subtle) {
      message(status, "This browser cannot sign in here. Open the site over https.", "error");
      return;
    }
    button.disabled = true;
    message(status, "Signing in.");
    try {
      const key = await deriveLoginKey(username, password);
      await api("/api/login", { method: "POST", json: { username: username.trim(), key, remember } });
      location.replace("/");
    } catch (err) {
      message(status, err instanceof ApiError ? err.message : "Signing in failed. Try again.", "error");
      button.disabled = false;
    }
  });
}

start();
