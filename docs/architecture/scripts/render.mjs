#!/usr/bin/env node
/**
 * Render every Mermaid code block in docs/architecture/*.md to SVG (and PNG when
 * supported) under docs/architecture/assets/.
 *
 * Usage:  node docs/architecture/scripts/render.mjs [--png]
 *
 * Requires Node.js only. Uses @mermaid-js/mermaid-cli via `npx` (no install needed).
 * If the headless browser cannot be downloaded (offline/locked-down env), the
 * Markdown diagrams still render fine on GitHub / in the Cursor Markdown preview.
 */
import { readFileSync, writeFileSync, readdirSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const DOCS_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const ASSETS_DIR = join(DOCS_DIR, "assets");
const TMP_DIR = join(ASSETS_DIR, ".tmp");
const WANT_PNG = process.argv.includes("--png");

const MERMAID_BLOCK = /```mermaid\s*\n([\s\S]*?)```/g;

function extractBlocks(md) {
  const blocks = [];
  let m;
  while ((m = MERMAID_BLOCK.exec(md)) !== null) blocks.push(m[1].trim());
  return blocks;
}

function render(input, output) {
  // -y so npx auto-installs the CLI on first run; -q keeps it quiet.
  const args = ["-y", "@mermaid-js/mermaid-cli", "-i", input, "-o", output, "-b", "transparent"];
  const res = spawnSync(process.platform === "win32" ? "npx.cmd" : "npx", args, {
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  return res.status === 0;
}

function main() {
  mkdirSync(ASSETS_DIR, { recursive: true });
  mkdirSync(TMP_DIR, { recursive: true });

  const mdFiles = readdirSync(DOCS_DIR).filter((f) => f.endsWith(".md"));
  let total = 0;
  let ok = 0;

  for (const file of mdFiles) {
    const md = readFileSync(join(DOCS_DIR, file), "utf8");
    const blocks = extractBlocks(md);
    const stem = basename(file, ".md");

    blocks.forEach((code, i) => {
      total++;
      const idx = String(i + 1).padStart(2, "0");
      const mmd = join(TMP_DIR, `${stem}-${idx}.mmd`);
      writeFileSync(mmd, code, "utf8");

      const svg = join(ASSETS_DIR, `${stem}-${idx}.svg`);
      if (render(mmd, svg)) {
        ok++;
        if (WANT_PNG) render(mmd, join(ASSETS_DIR, `${stem}-${idx}.png`));
      } else {
        console.error(`  ! failed: ${stem} block ${idx}`);
      }
    });
  }

  rmSync(TMP_DIR, { recursive: true, force: true });
  console.log(`\nRendered ${ok}/${total} Mermaid diagrams into ${ASSETS_DIR}`);
  if (ok === 0 && total > 0) {
    console.log(
      "No diagrams rendered. mermaid-cli likely could not download its headless " +
        "browser. Markdown diagrams still render on GitHub and in Cursor preview."
    );
  }
}

main();
