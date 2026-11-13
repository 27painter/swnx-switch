"""End-to-end: the MCP stdio server really speaks the protocol an agent needs.

This is the test that would have caught "the server starts but the client sees no
tools": it drives the real subprocess over pipes exactly as DSH does, and it does
not touch a CAD application (``swnx_list_routes`` is CAD-free).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
ENV = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}


def _run(messages: list[dict], timeout: float = 60.0) -> list[dict]:
    payload = "\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n"
    completed = subprocess.run(
        [sys.executable, "-m", "swnx_switch.ai.mcp_server"],
        input=payload,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=ENV,
        timeout=timeout,
        cwd=str(REPO),
    )
    replies = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line:
            replies.append(json.loads(line))
    return replies


def test_initialize_and_tools_list() -> None:
    replies = _run(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    )
    by_id = {reply.get("id"): reply for reply in replies}
    assert by_id[1]["result"]["serverInfo"]["name"] == "swnx-switch"
    # an older client must not get a version it cannot parse
    assert by_id[1]["result"]["protocolVersion"] == "2024-11-05"
    tools = {tool["name"] for tool in by_id[2]["result"]["tools"]}
    assert {"swnx_probe", "swnx_read_part", "swnx_convert_part", "swnx_compare_parts"} <= tools
    for tool in by_id[2]["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"].strip()


def test_tools_call_reports_structured_content() -> None:
    replies = _run(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "swnx_list_routes", "arguments": {}}},
        ]
    )
    call = [reply for reply in replies if reply.get("id") == 2][0]
    assert call["result"]["isError"] is False
    assert call["result"]["structuredContent"]["pairs"]
    assert "step" in call["result"]["content"][0]["text"].lower()


def test_unknown_method_and_unknown_tool_do_not_kill_the_server() -> None:
    replies = _run(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "definitely/not/a/method", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
        ]
    )
    by_id = {reply.get("id"): reply for reply in replies}
    assert by_id[2]["error"]["code"] == -32601
    assert by_id[3]["result"]["isError"] is True
    assert "unknown tool" in by_id[3]["result"]["content"][0]["text"]
    assert by_id[4]["result"] == {}


if __name__ == "__main__":  # pragma: no cover
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001 - plain runner
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
