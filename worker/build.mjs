import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const directory = resolve(process.argv[2] ?? resolve(root, "dist/server"));
const mediaTypes = {
  "index.html": "text/html",
  "telegram.html": "text/html",
  "styles.css": "text/css",
  "app.js": "text/javascript",
  "telegram.js": "text/javascript"
};

const source = await readFile(new URL("index.mjs", import.meta.url), "utf8");
const assets = Object.fromEntries(await Promise.all(Object.entries(mediaTypes).map(async ([name, type]) => [
  "/" + name,
  [type + "; charset=utf-8", await readFile(resolve(root, "dist", name), "utf8")]
])));

await mkdir(directory, { recursive: true });
await writeFile(resolve(directory, "index.js"), source + "\nexport default createWorker(" + JSON.stringify(assets) + ");\n");
