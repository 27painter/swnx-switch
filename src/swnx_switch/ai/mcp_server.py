"""Dependency-free MCP stdio server exposing the swnx-switch tools.

Why hand-rolled instead of the official SDK: this server has to run inside the
user's DSH/CAD environment where only ``pywin32`` is guaranteed.  The MCP stdio
transport is newline-delimited JSON-RPC 2.0, so the wire half is small; the
semantic half is ``tools/list`` + ``tools/call`` + ``initialize``.

stdout is protocol-only.  Anything diagnostic goes to stderr, because a stray
``print`` on stdout corrupts the stream and the client then sees a server that
"dies silently".
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

from .. import __version__
from .tools import TOOLS, call_tool

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _log(message: str) -> None:
    sys.stderr.write(f"[swnx-switch] {message}\n")
    sys.stderr.flush()


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle(request: dict, context: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "swnx-switch", "version": __version__},
                "instructions": (
                    "swnx-switch 读取 SolidWorks/NX 零件为中立模型并互转格式。"
                    "先调用 swnx_probe 确认环境；转换前用 swnx_read_part 看零件摘要；"
                    "swnx_convert_part 是长任务（1-5 分钟），不要重复发起同一转换。"
                ),
            },
        )
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None  # notifications never get a reply
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        _log(f"tools/call {name} {json.dumps(arguments, ensure_ascii=False)[:200]}")
        outcome = call_tool(name, arguments, context=context)
        content = [{"type": "text", "text": outcome.get("text") or ""}]
        result: dict[str, Any] = {"content": content, "isError": bool(outcome.get("isError"))}
        if isinstance(outcome.get("data"), dict):
            result["structuredContent"] = outcome["data"]
        return _result(request_id, result)
    if method in ("resources/list", "prompts/list", "logging/setLevel"):
        if method == "resources/list":
            return _result(request_id, {"resources": []})
        if method == "prompts/list":
            return _result(request_id, {"prompts": []})
        return _result(request_id, {})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


def serve(*, workdir: str | None = None, outdir: str | None = None) -> int:
    context = {"workdir": workdir, "outdir": outdir}
    _log(f"serving MCP over stdio (version {__version__})")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            _send(_error(None, -32700, f"parse error: {exc}"))
            continue
        if isinstance(payload, list):
            replies = [reply for reply in (handle(item, context) for item in payload) if reply]
            if replies:
                _send(replies)
            continue
        try:
            reply = handle(payload, context)
        except Exception as exc:  # a tool bug must not kill the session
            _log("unhandled exception:\n" + traceback.format_exc())
            reply = _error(payload.get("id"), -32603, f"internal error: {exc}")
        if reply:
            _send(reply)
    _log("stdin closed, exiting")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(serve())
