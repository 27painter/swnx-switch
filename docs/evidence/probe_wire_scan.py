"""Reproduce the host's lossless-JSON rejection of swnx_probe(deep=true).

The 2026-11-14 report says the probe call failed with

    tool "mcp__swnx-switch__swnx_probe" returned invalid output: value is not lossless JSON

while the conversion itself was fine.  The host validates tool output as
*lossless* JSON: NaN/Infinity, -0, and integers outside the IEEE-754 safe range
all fail that check even though they are (mostly) valid JSON text.  This script
drives the real MCP server over stdio, saves the exact response line, and scans
it for every value the host would reject.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = r"D:\27painter\Documents\swnx-switch"
SRC = os.path.join(REPO, "src")
OUT = os.path.join(REPO, "work", "verify")
ENV = {**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"}

SAFE_INT = 2**53


def scan(value, path="", problems=None):
    problems = problems if problems is not None else []
    if isinstance(value, bool):
        return problems
    if isinstance(value, int):
        if abs(value) > SAFE_INT:
            problems.append((path or "$", "int outside safe range", repr(value)[:60]))
        return problems
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            problems.append((path or "$", "non-finite float", repr(value)))
        elif value == 0 and str(value).startswith("-"):
            problems.append((path or "$", "negative zero", repr(value)))
        return problems
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            problems.append((path or "$", "unencodable string", str(exc)))
        return problems
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                problems.append((path or "$", "non-string key", repr(key)))
            scan(item, f"{path}.{key}", problems)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            scan(item, f"{path}[{index}]", problems)
    return problems


class Server:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "swnx_switch.ai.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=REPO,
            env=ENV,
        )
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        payload = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return self.proc.stdout.readline()

    def close(self):
        try:
            self.proc.stdin.close()
        finally:
            self.proc.wait(timeout=30)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    server = Server()
    try:
        server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "scan"}})
        failures = 0
        for deep in (False, True):
            line = server.rpc("tools/call", {"name": "swnx_probe", "arguments": {"deep": deep}})
            name = "probe_deep" if deep else "probe_shallow"
            target = os.path.join(OUT, f"{name}_response.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(line)
            print(f"{name}: {len(line)} bytes -> {target}")
            try:
                parsed = json.loads(line, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
            except ValueError as exc:
                failures += 1
                print(f"  PARSE FAIL: {exc}")
                continue
            result = parsed.get("result") or {}
            structured = result.get("structuredContent")
            print(f"  isError={result.get('isError')} structuredContent={'yes' if structured is not None else 'no'}")
            for label, value in (("structuredContent", structured), ("whole-line", parsed)):
                problems = scan(value, label)
                if problems:
                    failures += len(problems)
                    for path, kind, shown in problems[:20]:
                        print(f"  HOST-REJECT {kind} at {path} = {shown}")
                else:
                    print(f"  {label}: clean for the host's lossless rules")
        return 1 if failures else 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())