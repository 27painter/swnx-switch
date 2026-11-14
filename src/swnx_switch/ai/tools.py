"""Agent-facing tool layer.

One registry, three transports: :mod:`swnx_switch.ai.mcp_server` speaks MCP over
stdio, the CLI uses the same handlers, and the DSH plugin exposes the MCP server.

Every handler returns ``{"ok": bool, "text": str, "data": dict}``.  ``text`` is
what the model reads; ``data`` is the structured half for a client that wants to
chain calls without re-parsing prose.

Two hard rules, both driven by a real user report
-------------------------------------------------
1. **Every byte on the wire is strict JSON** — no ``NaN``/``Infinity``.  The host
   parses tool results with a lossless-JSON validator, and one non-finite float
   deep inside a comparison used to turn a *successful* conversion into
   ``invalid output``.  All serialisation goes through :mod:`swnx_switch.ai.wire`.
2. **The payload is small.**  ``data`` carries the verdict, the target hash, the
   key checks and the report path — never the full 15-check comparison or the
   whole IR (those are on disk, and ``run_dir``/``ir_path`` point at them).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .. import ir
from ..errors import SwnxError
from ..jobs import MANAGER
from ..pipeline import Pipeline, probe_environment, supported_pairs
from . import wire

MAX_TEXT = 24_000

#: One line the model can quote so users do not think a conversion went wrong.
EXPECTATION = "预期差异：几何 1:1 一致；参数化特征树/草图不保留（跨内核固有限制，不是转换失败）。"

#: Checks worth showing in the slim payload.  Everything else stays on disk.
KEY_CHECKS = ("units", "body_count", "volume", "surface_area", "bbox_x", "bbox_y", "bbox_z", "centroid")


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
_TOLERANCE = {
    "type": "object",
    "description": "覆盖容差，例如 {\"volume\": 0.002}",
    "additionalProperties": {"type": "number"},
}
_CONVERT_PROPS = {
    "source": _PATH,
    "target": _PATH,
    "mode": {"type": "string", "enum": ["auto", "step", "parasolid"], "description": "中间格式，默认 auto"},
    "step_variant": {"type": "string", "enum": ["ap203", "ap214", "ap242"], "description": "STEP 协议版本，默认 ap242"},
    "force": {"type": "boolean", "description": "覆盖已存在的目标文件"},
    "report": {"type": "boolean", "description": "是否生成 Markdown 报告（默认 true）"},
    "tolerance": _TOLERANCE,
    "keep_app": {
        "type": "boolean",
        "description": "转换结束后保持 SolidWorks 运行，供下一次作业直接复用（默认 false）",
    },
    "preflight": {
        "type": "boolean",
        "description": "开始前先做一次深度探测，环境不可用时快速失败（默认 false）",
    },
}

TOOLS: list[dict] = [
    {
        "name": "swnx_probe",
        "description": (
            "探测本机 CAD 环境：NX 安装根、SolidWorks 版本、pywin32、各 backend 是否可用、工作目录。"
            "默认只做静态检查（快，不启动 CAD）；deep=true 时会真的尝试启动/连接一次（约 5-20 s），"
            "返回是否 launchable / reachable 与 last_error。"
        ),
        "inputSchema": _schema(
            {"deep": {"type": "boolean", "description": "深度探测：尝试实际启动/连接 CAD（默认 false）"}}
        ),
    },
    {
        "name": "swnx_read_part",
        "description": (
            "读取一个 CAD 文件为中立 JSON 模型（IR），返回摘要 + 给大模型看的零件简报。"
            "支持 .sldprt / .prt / .slddrw / .sldasm / .step / .stp / .x_t。"
            "原生文件用对应 CAD 应用读取；STEP/Parasolid 会先导入到 via 指定的应用再读。"
            "一次调用可能耗时 30-180 秒，NX 尤其慢。IR 全文在 ir_path 指向的文件里，返回值只带摘要。"
        ),
        "inputSchema": _schema(
            {
                "path": _PATH,
                "via": {"type": "string", "enum": ["nx", "solidworks"], "description": "读取 STEP/Parasolid 时用哪个应用"},
                "save_ir": {"type": "boolean", "description": "是否把 IR JSON 落盘（默认 true）"},
                "brief": {"type": "boolean", "description": "是否附带 Markdown 简报（默认 true）"},
                "keep_app": {"type": "boolean", "description": "读完后保持 CAD 运行（默认 false）"},
            },
            ["path"],
        ),
    },
    {
        "name": "swnx_convert_part",
        "description": (
            "同步转换：把一个零件转换成另一种格式并做容差校验，完成后返回。"
            "支持 sldprt<->prt（经 STEP/Parasolid 中转）、原生->STEP/Parasolid、STEP/Parasolid->原生。"
            "返回 verdict（pass/warn/fail）、关键检查、目标文件 sha256、报告路径。"
            "这是长任务：SW<->NX 一次通常 1-5 分钟。若调用方可能超时，改用 swnx_convert_start。"
        ),
        "inputSchema": _schema(_CONVERT_PROPS, ["source", "target"]),
    },
    {
        "name": "swnx_convert_start",
        "description": (
            "异步转换：立即返回 job_id，转换在后台继续；用 swnx_job_status 轮询进度"
            "（stage: source_read -> export -> import -> target_read -> validate -> report）。"
            "长任务优先用这个，能避免调用方同步等待几分钟，也能看到卡在哪一步。"
        ),
        "inputSchema": _schema(_CONVERT_PROPS, ["source", "target"]),
    },
    {
        "name": "swnx_job_status",
        "description": (
            "查询 swnx_convert_start 启动的作业：state（running/done/failed）、stage、elapsed_s、"
            "已完成阶段列表，以及作业完成后与 swnx_convert_part 相同的精简结果。"
        ),
        "inputSchema": _schema({"job_id": {"type": "string", "description": "swnx_convert_start 返回的 job_id"}}, ["job_id"]),
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
                "tolerance": _TOLERANCE,
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
            "text": wire.dumps({"error": exc.as_dict()}, indent=2),
            "data": {"error": exc.as_dict()},
            "isError": True,
        }


def _pipeline(context: dict) -> Pipeline:
    return Pipeline(workdir=context.get("workdir"), outdir=context.get("outdir"))


def _probe(arguments: dict, context: dict) -> dict:
    env = probe_environment(deep=bool(arguments.get("deep", False)))
    text = wire.dumps(env, indent=2)
    return {"ok": True, "text": _clip(text), "data": wire.sanitize(env)}


def _read(arguments: dict, context: dict) -> dict:
    from .brief import render

    path = Path(arguments["path"]).expanduser()
    pipeline = _pipeline(context)
    doc = pipeline.read(path, via=arguments.get("via"), keep_app=bool(arguments.get("keep_app", False)))
    summary = ir.summarize(doc)
    ir_path = None
    if arguments.get("save_ir", True):
        ir_path = Path(context.get("outdir") or pipeline.outdir) / f"{path.stem}.ir.json"
        ir.dump(doc, ir_path)
    brief_text = render(doc) if arguments.get("brief", True) else ""
    text = EXPECTATION + "\n\n" + wire.dumps(summary, indent=2)
    if ir_path:
        text += f"\n\nIR: `{ir_path}`"
    if brief_text:
        text += "\n\n" + brief_text
    data = wire.sanitize(
        {
            "summary": summary,
            "ir_path": str(ir_path) if ir_path else None,
            "brief": brief_text or None,
        }
    )
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
        keep_app=bool(arguments.get("keep_app", False)),
        preflight=bool(arguments.get("preflight", False)),
    )
    data = _slim_payload(result)
    return {"ok": result.ok, "text": _clip(_convert_text(result, data)), "data": data, "isError": not result.ok}


def _convert_start(arguments: dict, context: dict) -> dict:
    source = arguments["source"]
    target = arguments["target"]
    options = {
        "mode": arguments.get("mode", "auto"),
        "step_variant": arguments.get("step_variant", "ap242"),
        "force": bool(arguments.get("force", False)),
        "tolerances": arguments.get("tolerance"),
        "report": bool(arguments.get("report", True)),
        "keep_app": bool(arguments.get("keep_app", False)),
        "preflight": bool(arguments.get("preflight", False)),
    }

    def work(progress: Callable[[str, dict], None]) -> dict:
        pipeline = _pipeline(context)
        result = pipeline.convert(source, target, progress=progress, **options)
        return _slim_payload(result)

    job_id = MANAGER.start("convert", work, params={"source": str(source), "target": str(target), "mode": options["mode"]})
    text = (
        f"已启动转换作业 `{job_id}`（后台执行，约 30 s – 5 min）。\n"
        f"轮询：swnx_job_status {{\"job_id\": \"{job_id}\"}}；"
        f"完成前不要把同一个转换再发起一次。"
    )
    data = {"job_id": job_id, "state": "running", "source": str(source), "target": str(target)}
    return {"ok": True, "text": text, "data": data}


def _job_status(arguments: dict, context: dict) -> dict:
    job_id = str(arguments["job_id"])
    snapshot = MANAGER.get(job_id)
    if snapshot is None:
        raise SwnxError(
            f"unknown job id {job_id!r}",
            hint="job ids live in this MCP server process; a server restart forgets them. Check `run_dir` under work/ instead.",
        )
    text = (
        f"job {job_id}: state={snapshot['state']} stage={snapshot['stage']} "
        f"elapsed={snapshot['elapsed_s']}s\n"
        + "  stages: "
        + " -> ".join(f"{entry['stage']}@{entry['at_s']}s" for entry in snapshot["stages"])
    )
    if snapshot["state"] == "done" and snapshot["result"]:
        text += "\n\n" + _result_short_text(snapshot["result"])
    if snapshot["state"] == "failed":
        text += f"\n\nerror: {snapshot['error']}"
    return {
        "ok": snapshot["state"] != "failed",
        "text": _clip(text),
        "data": wire.sanitize(snapshot),
        "isError": snapshot["state"] == "failed",
    }


def _compare(arguments: dict, context: dict) -> dict:
    from ..validate import compare

    reference = _load_any(arguments["reference"], context)
    candidate = _load_any(arguments["candidate"], context)
    comparison = compare(reference, candidate, tolerances=arguments.get("tolerance"))
    text = wire.dumps(comparison, indent=2) + "\n\n" + _comparison_text(comparison)
    ok = comparison.get("verdict") in ("pass", "warn")
    return {"ok": ok, "text": _clip(text), "data": wire.sanitize(comparison), "isError": not ok}


def _part_brief(arguments: dict, context: dict) -> dict:
    from .brief import render

    doc = ir.load(arguments["ir"])
    text = render(doc)
    return {"ok": True, "text": _clip(text), "data": {"brief": text}}


def _routes(arguments: dict, context: dict) -> dict:
    payload = {"pairs": supported_pairs()}
    return {"ok": True, "text": wire.dumps(payload, indent=2), "data": payload}


_HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "swnx_probe": _probe,
    "swnx_read_part": _read,
    "swnx_convert_part": _convert,
    "swnx_convert_start": _convert_start,
    "swnx_job_status": _job_status,
    "swnx_compare_parts": _compare,
    "swnx_part_brief": _part_brief,
    "swnx_list_routes": _routes,
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _slim_payload(result) -> dict:
    """The small, stable shape a client needs to decide success and next steps."""
    comparison = result.comparison or {}
    checks = comparison.get("checks") or []
    key_checks = []
    for check in checks:
        if check.get("id") not in KEY_CHECKS:
            continue
        key_checks.append(
            {
                "id": check.get("id"),
                "status": check.get("status"),
                "actual": check.get("actual"),
                "expected": check.get("expected"),
                "delta": check.get("delta"),
                "tolerance": check.get("tolerance"),
                "detail": check.get("detail"),
            }
        )
    payload = {
        "ok": result.ok,
        "verdict": result.verdict,
        "mode": f"{result.source_format}->{result.target_format}",
        "target": str(result.target),
        "target_sha256": result.paths.get("target_sha256"),
        "seconds": round(result.seconds, 2),
        "key_checks": key_checks,
        "report_md": result.paths.get("report_md_near") or result.paths.get("report_md"),
        "report_json": result.paths.get("report_json_near") or result.paths.get("report_json"),
        "run_dir": str(result.run_dir),
        "attempts": [attempt.as_dict() for attempt in result.attempts],
        "warnings": result.warnings[:10],
        "error": result.error,
        "expected_difference": "parametric features are not preserved (B-Rep exchange)",
    }
    return wire.sanitize(payload)


def _convert_text(result, data: dict) -> str:
    lines = [EXPECTATION, ""]
    lines.append(f"结论 verdict: **{data['verdict']}**  ({data['seconds']} s)")
    if data.get("target"):
        lines.append(f"目标 target: `{data['target']}`")
    if data.get("target_sha256"):
        lines.append(f"SHA-256: `{data['target_sha256'][:32]}…`")
    lines.append("")
    lines.append(_key_checks_text(data.get("key_checks") or []))
    if data.get("warnings"):
        lines.append("")
        lines.append("警告: " + "; ".join(str(item) for item in data["warnings"][:4]))
    if data.get("report_md"):
        lines.append("")
        lines.append(f"报告 report: `{data['report_md']}`")
    if data.get("error"):
        lines.append("")
        lines.append(f"错误 error: {data['error']}")
    return "\n".join(lines)


def _result_short_text(data: dict) -> str:
    lines = [f"结论 verdict: **{data.get('verdict')}**  ({data.get('seconds')} s)"]
    if data.get("target"):
        lines.append(f"目标 target: `{data['target']}`")
    lines.append(_key_checks_text(data.get("key_checks") or []))
    if data.get("report_md"):
        lines.append(f"报告 report: `{data['report_md']}`")
    if data.get("error"):
        lines.append(f"错误 error: {data['error']}")
    return "\n".join(lines)


def _key_checks_text(checks: list[dict]) -> str:
    if not checks:
        return "检查: (无，源或目标缺少可比较的质量属性)"
    parts = []
    for check in checks:
        parts.append(
            f"{check.get('id')}={check.get('status')}"
            f"({_fmt(check.get('actual'))} vs {_fmt(check.get('expected'))}, Δ{_fmt(check.get('delta'))})"
        )
    return "关键检查: " + "; ".join(parts)


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
