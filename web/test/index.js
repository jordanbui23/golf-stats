import { readdirSync } from "node:fs";

const dir = new URL("./", import.meta.url);
for (const name of readdirSync(dir).filter((n) => n.endsWith(".test.mjs")).sort()) {
  await import(new URL(name, dir));
}
