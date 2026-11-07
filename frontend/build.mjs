/* Build step for the genai-bench WebUI frontend.

This is a ZERO-dependency build (guardrail #2): no npm, no bundler, no
network. It simply copies the hand-written, browser-native assets from
frontend/ into the Python package's shipped static directory
(genai_bench/web/static/), which is included in the wheel by Hatchling.

The final Docker image runs ONLY `pip install .` + the Python entry point:
it contains NO Node, no frontend/ source, and no build tool. The Node stage
in the multi-stage Dockerfile exists only to run this script at build time.

Usage:  node frontend/build.mjs
        python -m genai_bench.web.build   (pure-Python fallback, no Node)
*/

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const out = join(root, "genai_bench", "web", "static");
if (!existsSync(out)) mkdirSync(out, { recursive: true });

const files = ["index.html", "app.css", "app.js", "chart.js"];
for (const f of files) {
  const src = readFileSync(join(here, f));
  writeFileSync(join(out, f), src);
  console.log(`  static/${f}  ${src.length} bytes`);
}
console.log(`Built ${files.length} static assets -> ${out}`);
