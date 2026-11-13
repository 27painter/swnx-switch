#!/usr/bin/env node
// swnx-switch: doctor / where / mcp for the DSH bundle.
//
// The actual conversion work happens in Python; this wrapper exists so a human
// can check the wiring without starting dsh, and so `swnx-switch mcp` works in
// any MCP client.
import { spawn, spawnSync } from "node:child_process";
import path from "node:path";

import { missingPieces, resolveLayout } from "../lib/paths.mjs";

const LOG = "[swnx-switch]";
const say = (message) => console.error(`${LOG} ${message}`);

function layoutOrDie() {
  const layout = resolveLayout();
  const missing = missingPieces(layout);
  if (missing.length > 0) {
    say(`cannot start: missing ${missing.join("; ")}`);
    say("set SWNX_SWITCH_HOME (checkout root) and SWNX_PYTHON (python 3.10+)");
    process.exit(2);
  }
  return layout;
}

function pythonEnv(layout) {
  return {
    ...process.env,
    PYTHONPATH: layout.src,
    PYTHONIOENCODING: "utf-8",
    PYTHONUNBUFFERED: "1",
    SWNX_WORKDIR: layout.workdir,
    SWNX_OUTDIR: layout.outdir,
  };
}

function runPython(layout, args, { stdio = "inherit" } = {}) {
  return spawnSync(layout.python, ["-m", ...args], {
    env: pythonEnv(layout),
    stdio,
    encoding: "utf8",
  });
}

function cmdWhere() {
  const layout = layoutOrDie();
  console.log(`project  ${layout.home}`);
  console.log(`src      ${layout.src}`);
  console.log(`python   ${layout.python}  (via ${layout.pythonVia})`);
  console.log(`workdir  ${layout.workdir}`);
  console.log(`outdir   ${layout.outdir}`);
}

function cmdDoctor() {
  const layout = layoutOrDie();
  cmdWhere();
  const res = runPython(layout, ["swnx_switch.cli", "probe"], { stdio: "inherit" });
  if (res.error) {
    say(`could not run python: ${res.error.message}`);
    process.exit(1);
  }
  process.exit(res.status ?? 1);
}

function cmdMcp() {
  const layout = layoutOrDie();
  const child = spawn(layout.python, ["-m", "swnx_switch.ai.mcp_server"], {
    env: pythonEnv(layout),
    stdio: "inherit",
  });
  child.on("close", (code) => process.exit(code ?? 0));
  child.on("error", (err) => {
    say(`could not start the MCP server: ${err.message}`);
    process.exit(1);
  });
}

function main(argv) {
  const [command, ...rest] = argv;
  if (!command || command === "--help" || command === "-h") {
    console.log("usage: swnx-switch <doctor|where|mcp|read|convert|compare|routes> [args...]");
    console.log("  doctor   print the resolved layout and the CAD environment");
    console.log("  where    print the resolved layout");
    console.log("  mcp      run the MCP server on stdio (for any MCP client)");
    console.log("  other    forwarded to `python -m swnx_switch.cli <args>`");
    return;
  }
  if (command === "where") return cmdWhere();
  if (command === "doctor") return cmdDoctor();
  if (command === "mcp") return cmdMcp();
  const layout = layoutOrDie();
  process.exit(runPython(layout, ["swnx_switch.cli", command, ...rest]).status ?? 1);
}

main(process.argv.slice(2));
