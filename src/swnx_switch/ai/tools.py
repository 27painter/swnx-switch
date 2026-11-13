"""Agent-facing tool layer.

One registry, two transports: :mod:`swnx_switch.ai.mcp_server` speaks MCP over
stdio, and the CLI uses the same handlers.  Tool descriptions are written for a
model, not for a human — they say when *not* to use the tool as well as how.

Every handler returns ``{"ok": bool, "text": str, "data": dict}``.  ``text`` is
what the model reads; ``data`` is the structured half for a client that wants to
chain calls without re-parsing prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .. import ir
from ..errors import SwnxError
from ..pipeline import Pipeline, probe_environment, supported_pairs

MAX_TEXT = 24_000


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #
def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_PATH = {"type": "string", "description": "绝对路径"}

TOOLS: list[dict] = [
    {
        "name": "swnx_probe",
        "description": (
            "探测本机 CAD 环境：NX 安装根、SolidWorks 版本、pywin32、各 backend 是否可用、工作目录。"
            "在任何转换之前先调用；返回 available=false 时不要继续调用其他工具，而是把 hint 告诉用户。"
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "swnx_read_part",
        "description": (
            "读取一个 CAD 文件为中立 JSON 模型（IR），返回摘要 + 给大模型看的零件简报。"
            "支持 .sldprt / .prt / .slddrw / .sldasm / .step / .stp / .x_t。"
            "原生文件用对应 CAD 应用读取；STEP/Parasolid 会先导入到 via 指定的应用再读。"
            "一次调用可能耗时 30-180 秒，NX 尤其慢。"
        ),
        "inputSchema": _schema(
            {
                "path": _PATH,
                "via": {"type": "string", "enum": ["nx", "solidworks"], "description": "读取 STEP/Parasolid 时用哪个应用"},
                "save_ir": {"type": "boolean", "description": "是否把 IR JSON 落盘（默认 true）"},
                "brief": {"type": "boolean", "description": "是否附带 Markdown 简报（默认 true）"},
            },
            ["path"],
        ),
    },
    {
        "name": "swnx_convert_part",
        "description": (
            "把一个零件转换成另一种格式并做容差校验。支持 sldprt<->prt（经 STEP/Parasolid 中转）、"
            "以及原生->STEP/Parasolid、STEP/Parasolid->原生。"
            "返回 verdict（pass/warn/fail）、逐项检查、报告路径。"
            "这是长任务：SW<->NX 一次通常 1-5 分钟，不要重复调用同一个转换。"
        ),
        "inputSchema": _schema(
            {
                "source": _PATH,
                "target": _PATH,
                "mode": {"type": "string", "enum": ["auto", "step", "parasolid"], "description": "中间格式，默认 auto"},
                "step_variant": {"type": "string", "enum": ["ap203", "ap214", "ap242"], "description": "STEP 协议版本，默认 ap242"},
                "force": {"type": "boolean", "description": "覆盖已存在的目标文件"},
                "report": {"type": "boolean", "description": "是否生成 Markdown 报告（默认 true）"},
                "tolerance": {
                    "type": "object",
                    "description": "覆盖容差，例如 {\"volume\": 0.002}",
                    "additionalProperties": {"type": "number"},
                },
            },
            ["source", "target"],
        ),
    },
    {
        "name": "swnx_compare_parts",
        "description": (
            "对比两个零件是否一致：可传两个 IR JSON，也可传两个原生文件（会先各读一次）。"
            "输出逐项检查与整体结论；用于验收一次手工转换，或核对两个来源是否是同一个零件。"
        ),
        "inputSchema": _schema(
            {
                "reference": _PATH,
                "candidate": _PATH,
                "tolerance": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                    "description": "覆盖容差，例如 {\"volume\": 0.002}",
                },
            },
            ["reference", "candidate"],
        ),
    },
    {
        "name": "swnx_part_brief",
        "description": "把一个已有 IR JSON 渲染成零件简报（Markdown），用于把零件信息喂给模型。",
        "inputSchema": _schema({"ir": _PATH}, ["ir"]),
    },
    {
        "name": "swnx_list_routes",
        "description": "列出本项目支持的转换路线与当前版本，不需要 CAD 应用。",
        "inputSchema": _schema({}),
    },
]

TOOL_NAMES = [tool["name"] for tool in TOOLS]


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def call_tool(name: str, arguments: dict | None = None, *, context: dict | None = None) -> dict:
    arguments = arguments or {}
    context = context or {}
    try:
        handler = _HANDLERS.get(name)
        if handler is None:
            raise SwnxError(f"unknown tool {name!r}", hint="available: " + ", ".join(TOOL_NAMES))
        return handler(arguments, context)
    except SwnxError as exc:
        return {
            "ok": False,
            "text": json.dumps({"error": exc.as_dict()}, ensure_ascii=False, indent=2),
            "data": {"error": exc.as_dict()},
            "isError": True,
        }


def _pipeline(context: dict) -> Pipeline:
    return Pipeline(workdir=context.get("workdir"), outdir=context.get("outdir"))


def _probe(arguments: dict, context: dict) -> dict:
    env = probe_environment()
    text = json.dumps(env, ensure_ascii=False, indent=2)
    return {"ok": True, "text": _clip(text), "data": env}


def _read(arguments: dict, context: dict) -> dict:
    from .brief import render

    path = Path(arguments["path"]).expanduser()
    pipeline = _pipeline(context)
    doc = pipeline.read(path, via=arguments.get("via"))
    summary = ir.summarize(doc)
    ir_path = None
    if arguments.get("save_ir", True):
        ir_path = Path(context.get("outdir") or pipeline.outdir) / f"{path.stem}.ir.json"
        ir.dump(doc, ir_path)
    brief_text = render(doc) if arguments.get("brief", True) else ""
    text = "## 摘要 / Summary\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n```\n"
    if ir_path:
        text += f"\nIR: `{ir_path}`\n"
    if brief_text:
        text += "\n\n" + brief_text
    data = {"summary": summary, "ir_path": str(ir_path) if ir_path else None, "ir": doc}
    return {"ok": True, "text": _clip(text), "data": data}


def _convert(arguments: dict, context: dict) -> dict:
    pipeline = _pipeline(context)
    result = pipeline.convert(
        arguments["source"],
        arguments["target"],
        mode=arguments.get("mode", "auto"),
        step_variant=arguments.get("step_variant", "ap242"),
        force=bool(arguments.get("force", False)),
        tolerances=arguments.get("tolerance"),
        report=bool(arguments.get("report", True)),
    )
    payload = result.summary()
    brief = ""
    if result.comparison:
        brief = _comparison_text(result.comparison)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if brief:
        text += "\n\n" + brief
    return {"ok": result.ok, "text": _clip(text), "data": payload, "isError": not result.ok}


def _compare(arguments: dict, context: dict) -> dict:
    from ..validate import compare

    reference = _load_any(arguments["reference"], context)
    candidate = _load_any(arguments["candidate"], context)
    comparison = compare(reference, candidate, tolerances=arguments.get("tolerance"))
    text = json.dumps(comparison, ensure_ascii=False, indent=2) + "\n\n" + _comparison_text(comparison)
    ok = comparison.get("verdict") in ("pass", "warn")
    return {"ok": ok, "text": _clip(text), "data": comparison, "isError": not ok}


def _part_brief(arguments: dict, context: dict) -> dict:
    from .brief import render

    doc = ir.load(arguments["ir"])
    text = render(doc)
    return {"ok": True, "text": _clip(text), "data": {"brief": text}}


def _routes(arguments: dict, context: dict) -> dict:
    payload = {"pairs": supported_pairs()}
    return {"ok": True, "text": json.dumps(payload, ensure_ascii=False, indent=2), "data": payload}


_HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "swnx_probe": _probe,
    "swnx_read_part": _read,
    "swnx_convert_part": _convert,
    "swnx_compare_parts": _compare,
    "swnx_part_brief": _part_brief,
    "swnx_list_routes": _routes,
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_any(path: str, context: dict) -> dict:
    target = Path(path).expanduser()
    if target.suffix.lower() == ".json":
        return ir.load(target)
    return _pipeline(context).read(target)


def _comparison_text(comparison: dict) -> str:
    lines = [f"结论 verdict: **{comparison.get('verdict')}**", ""]
    lines.append("| 检查 | 结论 | 实测 | 期望 | 偏差 |")
    lines.append("|---|---|---:|---:|---:|")
    for check in comparison.get("checks") or []:
        lines.append(
            f"| {check.get('id')} | {check.get('status')} | "
            f"{_fmt(check.get('actual'))} | {_fmt(check.get('expected'))} | {_fmt(check.get('delta'))} |"
        )
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_fmt(v) for v in value) + "]"
    return str(value)[:24]


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"\n\n… (truncated, {len(text) - MAX_TEXT} chars omitted)"


__all__ = ["TOOLS", "TOOL_NAMES", "call_tool"]
