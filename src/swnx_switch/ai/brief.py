"""Render an IR document as a compact Markdown brief for an LLM.

The brief is the "AI reads the drawing data features" half of the project: an
agent that never opens SolidWorks still gets the part's identity, mass
properties, B-Rep primitive digest, feature tree and the honest limits of what a
format conversion can preserve.  Keep it dense — every line should either be a
number, a name, or a fact that changes what the agent does next.
"""

from __future__ import annotations

from typing import Iterable

from .. import ir

MAX_FEATURES = 40
MAX_SURFACE_ROWS = 12


def _num(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _vec(values: Iterable[float] | None, digits: int = 2) -> str:
    if not values:
        return "-"
    return " × ".join(f"{float(v):.{digits}f}" for v in values)


def render(doc: dict) -> str:
    problems = ir.validate(doc)
    if problems:
        return "# 无效的 IR 文档\n\n" + "\n".join(f"- {p}" for p in problems)

    model = doc["model"]
    source = doc["source"]
    props = model.get("mass_properties") or {}
    bbox = model.get("bounding_box") or {}
    summary = ir.summarize(doc)
    lines: list[str] = []

    lines.append(f"# 零件简报 / Part brief — {model.get('name') or 'unnamed'}")
    lines.append("")
    lines.append(f"- 来源 source: `{source.get('format')}` {source.get('path')}")
    lines.append(
        f"- 应用 app: {source.get('app')} {source.get('app_version')} · 读取时间 {doc.get('generated_at')}"
        f" · 生成器 swnx-switch {doc.get('generator', {}).get('version')}"
    )
    if source.get("sha256"):
        lines.append(f"- SHA-256: `{source['sha256'][:32]}…`")
    lines.append(
        f"- 类型 kind: {model.get('kind')} · 单位 units: {model.get('units')}"
        f" · 实体 bodies: {summary['bodies']} · 面 faces: {summary['faces']} · 特征 features: {summary['features']}"
    )
    lines.append("")

    lines.append("## 关键指标 / Key figures")
    lines.append("")
    lines.append("| 指标 | 值 | 单位 |")
    lines.append("|---|---:|---|")
    lines.append(f"| 体积 volume | {_num(props.get('volume_mm3'))} | mm³ |")
    lines.append(f"| 表面积 surface area | {_num(props.get('surface_area_mm2'))} | mm² |")
    if props.get("mass_kg") is not None:
        lines.append(f"| 质量 mass | {_num(props.get('mass_kg'), 6)} | kg |")
    if props.get("density_kg_m3") is not None:
        lines.append(f"| 密度 density | {_num(props.get('density_kg_m3'), 1)} | kg/m³ |")
    if bbox.get("size_mm"):
        lines.append(f"| 包围盒 bbox | {_vec(bbox['size_mm'])} | mm |")
    if props.get("centroid_mm"):
        lines.append(f"| 质心 centroid | {_vec(props['centroid_mm'])} | mm |")
    material = model.get("material") or {}
    if material.get("name"):
        lines.append(f"| 材料 material | {material['name']} | - |")
    lines.append("")

    digest = model.get("geometry_digest") or {}
    lines.append("## 几何特征摘要 / Geometry digest")
    lines.append("")
    lines.append("| 类型 | 数量 | 主要尺寸 |")
    lines.append("|---|---:|---|")
    for kind in ("plane", "cylinder", "cone", "sphere", "torus", "other"):
        group = digest.get(kind) or []
        if not group:
            continue
        lines.append(f"| {kind} | {len(group)} | {_describe_group(kind, group)} |")
    lines.append("")

    bodies = model.get("bodies") or []
    if bodies:
        lines.append("## 实体 / Bodies")
        lines.append("")
        lines.append("| 名称 | 类型 | 面 | 边 | 体积 mm³ | 面积 mm² |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for body in bodies[:20]:
            lines.append(
                f"| {body.get('name')} | {body.get('type')} | {_num(body.get('faces'), 0)} | "
                f"{_num(body.get('edges'), 0)} | {_num(body.get('volume_mm3'))} | {_num(body.get('area_mm2'))} |"
            )
        lines.append("")

    features = model.get("features") or []
    if features:
        lines.append("## 特征树 / Feature tree")
        lines.append("")
        lines.append(f"共 {len(features)} 个特征，展示前 {min(len(features), MAX_FEATURES)} 个：")
        lines.append("")
        for index, feature in enumerate(features[:MAX_FEATURES], start=1):
            flags = " [suppressed]" if feature.get("suppressed") else ""
            params = feature.get("parameters") or {}
            param_text = ""
            if params:
                pieces = [f"{k}={_short(v)}" for k, v in list(params.items())[:4]]
                param_text = " · " + ", ".join(pieces)
            lines.append(f"{index}. {feature.get('name')} — `{feature.get('type')}`{flags}{param_text}")
        if len(features) > MAX_FEATURES:
            lines.append(f"… 其余 {len(features) - MAX_FEATURES} 个特征省略")
        lines.append("")

    sketches = model.get("sketches") or []
    if sketches:
        lines.append("## 草图 / Sketches")
        lines.append("")
        for sketch in sketches[:MAX_SURFACE_ROWS * 2]:
            dims = sketch.get("dimensions") or {}
            dim_text = ", ".join(f"{k}={_short(v)}" for k, v in list(dims.items())[:6])
            plane = f" @ {sketch.get('plane')}" if sketch.get("plane") else ""
            entities = f" ({sketch.get('entities')} entities)" if sketch.get("entities") is not None else ""
            lines.append(f"- {sketch.get('name')}{plane}{entities}" + (f": {dim_text}" if dim_text else ""))
        lines.append("")

    custom = model.get("custom_properties") or {}
    if custom:
        lines.append("## 自定义属性 / Custom properties")
        lines.append("")
        for key, value in list(custom.items())[:20]:
            lines.append(f"- {key}: {_short(value)}")
        lines.append("")

    drawing = model.get("drawing")
    if drawing:
        sheets = drawing.get("sheets") or []
        lines.append("## 工程图 / Drawing")
        lines.append("")
        lines.append(f"- 图纸数 sheets: {len(sheets)}")
        for sheet in sheets[:6]:
            views = sheet.get("views") or []
            lines.append(f"- {sheet.get('name')} ({sheet.get('size') or '?'}) 视图 {len(views)}")
        lines.append("")

    lines.append("## 转换提示 / Conversion notes")
    lines.append("")
    lines.append("- 几何 B-Rep 可经 STEP AP242 或 Parasolid 传递；参数化特征树与建模历史不会保留，目标端得到的是导入实体/哑体。")
    lines.append("- 曲面细分（STL）只用于预览，不能作为转换中间格式。")
    lines.append("- 工程图标注、PMI、装配约束属于部分支持：以各自应用的实际读取结果为准。")
    lines.append("")

    diagnostics = doc.get("diagnostics") or {}
    warnings = diagnostics.get("warnings") or []
    limits = diagnostics.get("limits") or []
    if warnings or limits:
        lines.append("## 警告与限制 / Warnings and limits")
        lines.append("")
        for warning in warnings[:20]:
            lines.append(f"- warning: {warning}")
        for limit in limits[:20]:
            lines.append(f"- limit: {limit}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _describe_group(kind: str, group: list[dict]) -> str:
    if kind == "cylinder":
        radii = sorted({round(float(item.get("radius_mm", 0)), 3) for item in group if item.get("radius_mm")})
        if radii:
            shown = ", ".join(f"R{r:g}" for r in radii[:8])
            return f"半径 {shown}" + (" …" if len(radii) > 8 else "")
    if kind == "plane":
        areas = sorted((float(item.get("area_mm2", 0)) for item in group if item.get("area_mm2")), reverse=True)
        if areas:
            return "最大面积 " + f"{areas[0]:,.1f} mm²"
    if kind == "sphere":
        radii = sorted({round(float(item.get("radius_mm", 0)), 3) for item in group if item.get("radius_mm")})
        if radii:
            return "半径 " + ", ".join(f"R{r:g}" for r in radii[:6])
    if kind == "cone":
        angles = sorted({round(float(item.get("half_angle_deg", 0)), 3) for item in group if item.get("half_angle_deg")})
        if angles:
            return "半角 " + ", ".join(f"{a:g}°" for a in angles[:6])
    area = sum(float(item.get("area_mm2", 0)) for item in group if item.get("area_mm2"))
    return f"面积合计 {area:,.1f} mm²" if area else "-"


def _short(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_short(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_short(v)}" for k, v in list(value.items())[:4]) + "}"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "…"
