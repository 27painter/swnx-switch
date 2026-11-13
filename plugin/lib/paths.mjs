// Path resolution for the swnx-switch DSH bundle.
//
// The plugin ships inside the project checkout (plugin/) but dsh loads it from a
// profile's node_modules via a `link:` dependency, so __dirname is *not* next to
// src/ at runtime. Three independent anchors, first hit wins, each verified by
// looking for the Python package rather than by trusting a path string.
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const PKG_ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")), "..");
export const PKG = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, "package.json"), "utf8"));

const MARKER = path.join("src", "swnx_switch", "ai", "mcp_server.py");

export function exists(target) {
  try {
    fs.accessSync(target);
    return true;
  } catch {
    return false;
  }
}

function isProjectHome(dir) {
  return typeof dir === "string" && dir.length > 0 && exists(path.join(dir, MARKER));
}

// Anchors, in order:
//   1. SWNX_SWITCH_HOME (explicit override, also used by tests)
//   2. the default checkout used on this machine
//   3. plugin/.. and plugin/../.. (running from the checkout itself)
export function projectCandidates() {
  const out = [];
  if (process.env.SWNX_SWITCH_HOME) out.push(path.resolve(process.env.SWNX_SWITCH_HOME));
  out.push("D:\\27painter\\Documents\\swnx-switch");
  out.push(path.resolve(PKG_ROOT, ".."));
  out.push(path.resolve(PKG_ROOT, "..", ".."));
  return [...new Set(out)];
}

function probePython() {
  const candidates = [];
  if (process.env.SWNX_PYTHON) candidates.push({ command: process.env.SWNX_PYTHON, args: [] });
  candidates.push({ command: "python", args: [] });
  candidates.push({ command: "py", args: ["-3"] });
  for (const candidate of candidates) {
    const res = spawnSync(candidate.command, [...candidate.args, "-c", "import sys;print(sys.executable)"], {
      encoding: "utf8",
      timeout: 15000,
    });
    if (res.status === 0 && res.stdout && res.stdout.trim()) {
      return { exe: res.stdout.trim(), via: candidate.command };
    }
  }
  return null;
}

export function resolveLayout() {
  const home = projectCandidates().find(isProjectHome) || null;
  const python = probePython();
  return {
    home,
    src: home ? path.join(home, "src") : null,
    python: python ? python.exe : null,
    pythonVia: python ? python.via : null,
    workdir: home ? path.join(home, "work") : null,
    outdir: home ? path.join(home, "out") : null,
  };
}

export function missingPieces(layout) {
  const missing = [];
  if (!layout.home) missing.push(`project (looked in: ${projectCandidates().join(", ")})`);
  if (!layout.python) missing.push("python interpreter on PATH");
  return missing;
}
