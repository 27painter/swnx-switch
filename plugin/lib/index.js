// The dsh plugin entry point for swnx-switch.
//
// One MCP row, no upstream installation step: the "server" is the project's own
// Python package, so loading this plugin is cheap and a broken checkout fails
// loudly at load time instead of handing the agent tools that never answer.
//
// The MCP client is resolved by ./mcp-client.mjs rather than a bare import: a
// plugin installed with `link:` is loaded from its real path, where Node cannot
// see the host's node_modules.
import path from "node:path";

import { loadMcpClient } from "./mcp-client.mjs";
import { missingPieces, resolveLayout } from "./paths.mjs";

const McpClient = await loadMcpClient();

export const name = "swnx-switch";
export const inject = McpClient.inject ?? [];

const LOG_PREFIX = "[swnx-switch]";
const say = (message) => process.stderr.write(`${LOG_PREFIX} ${message}\n`);

export function apply(ctx) {
  if (process.env.SWNX_ENABLE_MCP === "0") {
    say("tools not registered (SWNX_ENABLE_MCP=0)");
    return;
  }

  const layout = resolveLayout();
  const missing = missingPieces(layout);
  if (missing.length > 0) {
    throw new Error(
      `${LOG_PREFIX} cannot start: missing ${missing.join("; ")}.\n` +
        `Set SWNX_SWITCH_HOME to the checkout (the folder containing src\\swnx_switch) ` +
        `and SWNX_PYTHON to a Python 3.10+ interpreter if it is not on PATH.`,
    );
  }

  ctx.plugin(McpClient, {
    serverName: "swnx-switch",
    transport: "stdio",
    command: layout.python,
    args: ["-m", "swnx_switch.ai.mcp_server"],
    env: {
      PYTHONPATH: layout.src,
      PYTHONIOENCODING: "utf-8",
      PYTHONUNBUFFERED: "1",
      SWNX_WORKDIR: layout.workdir,
      SWNX_OUTDIR: layout.outdir,
    },
    // A SolidWorks -> NX conversion legitimately takes minutes: a short timeout
    // would kill good work and teach the agent that conversions "fail".
    toolCallTimeoutMs: 1800000,
    failOnStartupError: true,
    reconnect: { enabled: true, initialDelayMs: 1000, maxDelayMs: 60000, maxAttempts: 5 },
  });

  say(`registered swnx-switch (project ${layout.home}, python ${layout.python})`);
}
