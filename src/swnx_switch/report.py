"""Render a validation comparison as a human report (Markdown + JSON).

The report is Chinese-first with English technical terms in brackets because
the people reading it are Chinese CAD engineers while the machine-readable
keys stay English.  Design constraints:

* **Terminal-safe tables.**  A Markdown table must not be wider than ~120
  display columns or it wraps into unreadable noise in a console.  Column
  widths are computed from content and clipped; details that need room live in
  bullet lists below the table.
* **No emojis and no decorative Unicode.**  The report is also read through
  CI logs and plain terminals.
* **Every claim comes from the comparison dict.**  ``render_markdown`` never
  recomputes measurements; it only formats them, so the Markdown and the JSON
  report can never disagree.
* **Missing metadata renders as "未提供"**, never as an exception: a report is
  most needed when the conversion failed and some inputs are missing.

Public API::

    render_markdown(comparison, meta=None) -> str
    write_report(comparison, meta=None, out_dir=None, *, stem="report")
        -> {"markdown": Path, "json": Path}
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import unicodedata
from pathlib import Path
from typing import Any

from .validate import verdict_of

REPORT_SCHEMA = "swnx.report/1"

VERDICT_LABELS = {"pass": "通过 (PASS)", "warn": "警告 (WARN)", "fail": "失败 (FAIL)"}
STATUS_LABELS = {
    "pass": "通过 pass",
    "warn": "警告 warn",
    "fail": "失败 fail",
    "unavailable": "不可用 n/a",
}
_STATUS_SORT = {"fail": 0, "warn": 1, "unavailable": 2, "pass": 3}

_TIMING_LABELS = {
    "source_read": "读取参考件",
    "read_source": "读取参考件",
    "convert": "格式转换",
    "conversion": "格式转换",
    "candidate_read": "读取转换件",
    "read_candidate": "读取转换件",
    "total": "合计",
    "write": "写盘",
    "validation": "验证",
}

_REPORT_LIMITATIONS = (
    "验证基于 IR 中的质量属性 (mass properties)、包围盒 (bounding box)、实体数量与特征树；"
    "IR 未提供的字段标记为 unavailable，不做推测。",
    "STEP/Parasolid 交换的是 B-Rep 几何，参数化特征树 (feature tree) 无法保留；"
    "特征差异仅作信息记录，不参与结论。",
    "面/边计数 (face/edge counts) 可能因 STEP 导出重排或切分面而变化，仅作信息记录。",
    "本报告不检查 PMI/标注、装配约束、颜色与外观。",
)

#: Cell caps keep the six-column check table under ~120 terminal columns.
_COLUMN_CAPS = (26, 14, 14, 13, 15, 11)
_TABLE_HEADERS = ("检查项", "实测", "期望", "偏差", "容差", "结论")


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _display_width(text: str) -> int:
    """Terminal columns, counting East-Asian wide characters as 2."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _clip(text: str, max_width: int) -> str:
    if max_width <= 2 or _display_width(text) <= max_width:
        return text
    kept: list[str] = []
    width = 0
    for char in text:
        char_width = _display_width(char)
        if width + char_width > max_width - 2:
            break
        kept.append(char)
        width += char_width
    return "".join(kept).rstrip() + ".."


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any) -> str:
    """Compact cell text; numbers keep 6 significant digits."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        number = _as_float(value)
        if number is None:
            return "-"
        if number == 0:
            return "0"
        return f"{number:.6g}"
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            body = ", ".join(_fmt(item) for item in value)
            return _clip(f"({body})", 24)
        return f"{len(value)} 项"
    if isinstance(value, dict):
        if set(value.keys()) <= {"faces", "edges"}:
            return "/".join(f"{key[0].upper()}{_fmt(value[key])}" for key in ("faces", "edges") if key in value)
        if set(value.keys()) <= {"count", "names"}:
            return f"{_fmt(value.get('count'))} 项"
        return _clip(", ".join(f"{key}={_fmt(item)}" for key, item in value.items()), 24)
    return str(value)


def _fmt_signed(value: Any) -> str:
    number = _as_float(value)
    if number is None:
        return _fmt(value)
    return ("+" if number > 0 else "") + _fmt(number)


def _fmt_tolerance(check: dict) -> str:
    tolerance = check.get("tolerance")
    if tolerance is None:
        return "-"
    unit = str(check.get("unit") or "").strip()
    text = ("±" if _as_float(tolerance) is not None else "") + _fmt(tolerance)
    return f"{text} {unit}".strip()


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


# --------------------------------------------------------------------------- #
# table rendering
# --------------------------------------------------------------------------- #
def _render_table(headers: tuple[str, ...], rows: list[list[str]], caps: tuple[int, ...]) -> list[str]:
    """Render a compact Markdown table with content-derived, capped widths."""
    widths: list[int] = []
    for index, header in enumerate(headers):
        cap = caps[index] if index < len(caps) else 20
        column_width = min(_display_width(header), cap)
        for row in rows:
            if index < len(row):
                column_width = max(column_width, min(_display_width(row[index]), cap))
        widths.append(max(column_width, 1))

    def render_row(cells: list[str]) -> str:
        padded = []
        for index, width in enumerate(widths):
            cell = cells[index] if index < len(cells) else ""
            cell = _clip(cell, width)
            padded.append(cell + " " * (width - _display_width(cell)))
        return "| " + " | ".join(padded) + " |"

    lines = [render_row(list(headers))]
    lines.append("|" + "|".join("-" * (width + 2) for width in widths) + "|")
    for row in rows:
        lines.append(render_row(row))
    return lines


def _check_rows(checks: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for check in checks:
        label = str(check.get("label") or check.get("id") or "?")
        rows.append(
            [
                label,
                _fmt(check.get("actual")),
                _fmt(check.get("expected")),
                _fmt_signed(check.get("delta")),
                _fmt_tolerance(check),
                STATUS_LABELS.get(str(check.get("status")), str(check.get("status"))),
            ]
        )
    return rows


# --------------------------------------------------------------------------- #
# metadata extraction
# --------------------------------------------------------------------------- #
def _meta_first(meta: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = meta.get(key)
        if value is None or value == "" or value == []:
            continue
        return value
    return None


def _meta_path(meta: dict, keys: tuple[str, ...], fallback: Any) -> str | None:
    value = _meta_first(meta, keys)
    if isinstance(value, dict):
        value = value.get("path")
    if value:
        return str(value)
    return str(fallback) if fallback else None


def _meta_sha(meta: dict, keys: tuple[str, ...], fallback: Any) -> str | None:
    value = _meta_first(meta, keys)
    if value:
        return str(value)
    return str(fallback) if fallback else None


def _format_app(app: Any, version: Any) -> str | None:
    if not app:
        return None
    if version and str(version) not in ("unknown", "None", ""):
        return f"{app} {version}"
    return str(app)


def _render_timings(meta: dict) -> str | None:
    timings = _meta_first(meta, ("timings", "durations"))
    if isinstance(timings, dict) and timings:
        parts: list[str] = []
        for key, value in timings.items():
            label = _TIMING_LABELS.get(str(key), str(key))
            number = _as_float(value)
            parts.append(f"{label} {number:.3g} s" if number is not None else f"{label} {value}")
        return " | ".join(parts)
    duration = _meta_first(meta, ("duration_s", "total_s", "elapsed_s", "seconds"))
    number = _as_float(duration)
    if number is not None:
        return f"合计 {number:.3g} s"
    return None


def _verdict_label(verdict: str) -> str:
    return VERDICT_LABELS.get(verdict, f"{verdict} (UNKNOWN)")


# --------------------------------------------------------------------------- #
# report body
# --------------------------------------------------------------------------- #
def render_markdown(comparison: dict, meta: dict | None = None) -> str:
    """Render the comparison as a Chinese-first Markdown report.

    Parameters
    ----------
    comparison:
        Result of :func:`swnx_switch.validate.compare`.  Missing keys are
        tolerated so a partially written comparison still produces a report.
    meta:
        Conversion metadata: ``source_path``, ``target_path``,
        ``source_sha256``, ``target_sha256``, ``mode``, ``timings``,
        ``source_app``, ``target_app``, ``started_at``, ``finished_at``,
        ``json_path``, ``warnings``, ``limitations``.  Unknown keys are
        ignored; nested ``{"source": {"path": ...}}`` is also accepted.

    Returns
    -------
    str
        Markdown ending with a newline.
    """
    comparison = comparison if isinstance(comparison, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    checks = [check for check in comparison.get("checks") or [] if isinstance(check, dict)]
    source = comparison.get("source") if isinstance(comparison.get("source"), dict) else {}
    candidate = comparison.get("candidate") if isinstance(comparison.get("candidate"), dict) else {}
    summary = comparison.get("summary") if isinstance(comparison.get("summary"), dict) else {}

    raw_verdict = comparison.get("verdict")
    verdict = raw_verdict if raw_verdict in VERDICT_LABELS else verdict_of(comparison)

    source_path = _meta_path(meta, ("source_path", "reference_path", "input_path", "source"), source.get("path"))
    target_path = _meta_path(
        meta, ("target_path", "candidate_path", "output_path", "target"), candidate.get("path")
    )
    source_sha = _meta_sha(meta, ("source_sha256", "reference_sha256", "input_sha256"), source.get("sha256"))
    target_sha = _meta_sha(meta, ("target_sha256", "candidate_sha256", "output_sha256"), candidate.get("sha256"))
    source_app = _format_app(
        _meta_first(meta, ("source_app",)) or source.get("app"),
        _meta_first(meta, ("source_app_version",)) or source.get("app_version"),
    )
    target_app = _format_app(
        _meta_first(meta, ("target_app",)) or candidate.get("app"),
        _meta_first(meta, ("target_app_version",)) or candidate.get("app_version"),
    )

    mode = _meta_first(meta, ("mode", "conversion_mode", "route"))
    if mode is None:
        source_format = source.get("format")
        target_format = candidate.get("format")
        if source_format or target_format:
            mode = f"{source_format or '?'} -> {target_format or '?'}"
    timings = _render_timings(meta)
    started_at = _meta_first(meta, ("started_at", "started"))
    finished_at = _meta_first(meta, ("finished_at", "finished"))
    generated_at = comparison.get("generated_at") or meta.get("generated_at") or _now()

    warnings = _string_list(comparison.get("warnings")) + _string_list(meta.get("warnings"))
    limitations = _string_list(comparison.get("limitations")) + _string_list(meta.get("limitations"))
    if not limitations:
        limitations = list(_REPORT_LIMITATIONS)
    limitations = list(dict.fromkeys(limitations))

    counts = {status: 0 for status in STATUS_LABELS}
    for check in checks:
        if check.get("status") in counts:
            counts[check["status"]] += 1
    required_unavailable = [
        str(check.get("id")) for check in checks if check.get("severity") == "required" and check.get("status") == "unavailable"
    ]
    failed = [str(check.get("id")) for check in checks if check.get("status") == "fail"]
    warned = [
        str(check.get("id"))
        for check in checks
        if check.get("status") == "warn" and check.get("severity") in ("required", "optional")
    ]
    informational = [
        str(check.get("id")) for check in checks if check.get("status") == "warn" and check.get("severity") == "info"
    ]

    lines: list[str] = []
    lines.append("# 转换验证报告 (Conversion Verification Report)")
    lines.append("")
    lines.append(f"- 结论 (Verdict): **{_verdict_label(verdict)}**")
    basis = summary.get("verdict_basis")
    if basis:
        lines.append(f"- 结论依据 (Basis): {basis}")
    lines.append("- 模式 (Mode): 只比较几何与质量属性 (geometry + mass properties)，不比较参数化特征")
    lines.append("")

    lines.append("## 概要 (Summary)")
    lines.append("")
    lines.append(f"- 参考件 (Reference/source): `{source_path}`" if source_path else "- 参考件 (Reference/source): 未提供")
    if source_sha:
        lines.append(f"- 参考件 SHA-256: `{source_sha}`")
    if source_app:
        lines.append(f"- 参考件应用 (app): {source_app}")
    lines.append(f"- 转换件 (Candidate/target): `{target_path}`" if target_path else "- 转换件 (Candidate/target): 未提供")
    if target_sha:
        lines.append(f"- 转换件 SHA-256: `{target_sha}`")
    if target_app:
        lines.append(f"- 转换件应用 (app): {target_app}")
    lines.append(f"- 转换方式 (Conversion mode): {mode}" if mode else "- 转换方式 (Conversion mode): 未提供 (unknown)")
    if timings:
        lines.append(f"- 耗时 (Timings): {timings}")
    if started_at or finished_at:
        lines.append(f"- 起止时间 (Timestamps): {started_at or '-'} -> {finished_at or '-'}")
    lines.append(f"- 报告生成时间 (Generated at): {generated_at}")
    lines.append("")

    lines.append("## 检查结果 (Checks)")
    lines.append("")
    if checks:
        lines.extend(_render_table(_TABLE_HEADERS, _check_rows(checks), _COLUMN_CAPS))
    else:
        lines.append("（无检查项 / no checks recorded）")
    lines.append("")

    lines.append("## 统计 (Statistics)")
    lines.append("")
    lines.append(
        f"- 检查总数 (checks): {len(checks)} | 通过 pass: {counts['pass']} | 警告 warn: {counts['warn']}"
        f" | 失败 fail: {counts['fail']} | 不可用 unavailable: {counts['unavailable']}"
    )
    if failed:
        lines.append("- 失败项 (failed): " + ", ".join(failed))
    if required_unavailable:
        lines.append("- 无法确认的必需项 (required unavailable): " + ", ".join(required_unavailable))
    if warned:
        lines.append("- 警告项 (warned): " + ", ".join(warned))
    if informational:
        lines.append("- 信息性差异 (informational): " + ", ".join(informational))
    lines.append("")

    findings = [check for check in checks if check.get("status") != "pass"]
    findings.sort(key=lambda check: _STATUS_SORT.get(str(check.get("status")), 9))
    if findings:
        lines.append("## 发现详情 (Findings)")
        lines.append("")
        for check in findings:
            status = str(check.get("status"))
            label = str(check.get("label") or check.get("id") or "?")
            lines.append(f"- [{status}] {label} (`{check.get('id')}`): {check.get('detail') or '-'}")
        lines.append("")

    lines.append("## 警告 (Warnings)")
    lines.append("")
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- 无 (none)")
    lines.append("")

    lines.append("## 限制 (Limitations)")
    lines.append("")
    lines.extend(f"- {limitation}" for limitation in limitations)
    lines.append("")

    json_path = _meta_first(meta, ("json_path", "report_json", "comparison_json"))
    lines.append("## 机器可读结果 (Machine-readable)")
    lines.append("")
    lines.append(
        f"- 对比 JSON 报告 (comparison JSON): `{json_path}`"
        if json_path
        else "- 对比 JSON 报告 (comparison JSON): 见同目录 JSON 文件 (same stem, .json)"
    )
    lines.append("- 检查项标识 (check ids) 与状态枚举 pass|warn|fail|unavailable 定义见 JSON 报告。")
    lines.append("")
    lines.append(
        "本报告由 swnx-switch 自动生成 (generated by swnx-switch)；数值单位为 mm、kg、kg/m3。"
        "通过结论 (PASS) 不等于参数化特征可用，几何保持 (geometry preserved) 与特征重建 (feature rebuild) 是两件事。"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# file output
# --------------------------------------------------------------------------- #
def _safe_stem(stem: str) -> str:
    name = Path(str(stem)).name
    for suffix in (".md", ".markdown", ".json"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    name = name.strip() or "report"
    if name in (".", ".."):
        name = "report"
    return name


def write_report(
    comparison: dict,
    meta: dict | None = None,
    out_dir: str | os.PathLike | None = None,
    *,
    stem: str = "report",
) -> dict[str, Path]:
    """Write ``<stem>.md`` and ``<stem>.json`` for a comparison.

    Parameters
    ----------
    comparison:
        Result of :func:`swnx_switch.validate.compare`.
    meta:
        Conversion metadata; see :func:`render_markdown`.  ``out_dir`` from
        ``meta`` is used when the argument is omitted.
    out_dir:
        Destination directory (created if needed).  Defaults to
        ``meta["out_dir"]`` or the current working directory.
    stem:
        Output file stem without extension.

    Returns
    -------
    dict[str, Path]
        ``{"markdown": Path, "json": Path}`` so the pipeline can log the
        exact artifacts it produced.
    """
    meta = dict(meta) if isinstance(meta, dict) else {}
    comparison = comparison if isinstance(comparison, dict) else {}
    base = Path(out_dir if out_dir is not None else (meta.get("out_dir") or ".")).expanduser()
    stem_name = _safe_stem(stem)
    markdown_path = base / f"{stem_name}.md"
    json_path = base / f"{stem_name}.json"

    # The written artifact is the authoritative JSON path for the report text.
    meta["json_path"] = str(json_path)
    markdown = render_markdown(comparison, meta)

    payload = {
        "schema": REPORT_SCHEMA,
        "generated_at": _now(),
        "meta": meta,
        "comparison": comparison,
    }

    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return {"markdown": markdown_path, "json": json_path}
