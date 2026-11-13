// Resolve the host's MCP client for a *linked* plugin.
//
// A plugin installed with `link:` is imported through its real path
// (D:\...\swnx-switch\plugin), so Node resolves bare specifiers from that
// directory — not from the profile's node_modules where the host lives. The
// plain import therefore fails with ERR_MODULE_NOT_FOUND even though dsh itself
// has the package. This module tries the normal path first and then the places
// the running host actually keeps @deepseek-ai/dsh-mcp-client.
import fs from "node:fs";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const SPEC = "@deepseek-ai/dsh-mcp-client";

function globalRoots() {
  const home = os.homedir();
  const roots = [
    // npm -g install of dsh (the usual DSH desktop/CLI layout)
    path.join(home, "AppData", "Roaming", "npm", "node_modules"),
    // pnpm global
    path.join(home, "AppData", "Local", "pnpm", "global", "5", "node_modules"),
    path.join(home, "AppData", "Local", "pnpm", "global", "4", "node_modules"),
    // dsh's own runtime dirs, if the app pins its dependencies there
    path.join(home, ".dsh", "dsh-runtimes", "node_modules"),
  ];
  for (const env of ["DSH_HOME", "DSH_INSTALL_DIR", "APPDATA"]) {
    if (process.env[env]) roots.push(path.join(process.env[env], "node_modules"));
  }
  return roots;
}

function candidates() {
  const out = [];
  for (const root of globalRoots()) {
    out.push(path.join(root, "@deepseek-ai", "dsh", "node_modules", "@deepseek-ai", "dsh-mcp-client", "lib", "index.js"));
    out.push(path.join(root, "@deepseek-ai", "dsh-mcp-client", "lib", "index.js"));
  }
  return out;
}

export async function loadMcpClient() {
  const attempts = [];

  // 1) the normal resolution (works when the plugin is copied into the profile)
  try {
    return await import(SPEC);
  } catch (error) {
    attempts.push(`plain import: ${error.code || error.message}`);
  }

  // 2) resolve as if standing next to the running dsh process
  const anchors = [process.argv[1], process.execPath].filter(Boolean).map((value) => path.resolve(value));
  for (const anchor of anchors) {
    try {
      const request = createRequire(anchor);
      const resolved = request.resolve(SPEC);
      return await import(pathToFileURL(resolved).href);
    } catch (error) {
      attempts.push(`require.resolve from ${anchor}: ${error.code || error.message}`);
    }
  }

  // 3) the known global install locations, checked on disk
  for (const candidate of candidates()) {
    try {
      if (!fs.existsSync(candidate)) continue;
      return await import(pathToFileURL(candidate).href);
    } catch (error) {
      attempts.push(`${candidate}: ${error.code || error.message}`);
    }
  }

  throw new Error(
    `swnx-switch cannot load ${SPEC}; the host package was not found.\n` +
      `Tried: ${attempts.join(" | ")}\n` +
      `Fix: set NODE_PATH to the dsh installation's node_modules, or reinstall the plugin inside the profile ` +
      `(copy instead of link:).`,
  );
}
