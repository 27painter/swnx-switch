"""Compare two IR documents and decide whether a conversion preserved the part.

Why this module exists
----------------------
A SolidWorks -> STEP -> NX conversion can "succeed" (every driver call returns
0, an output file appears) while the part it produced is wrong: a body was
dropped, an importer read inches as millimetres, an exporter wrote a
tessellated shell instead of a B-Rep.  This module is the only place that turns
two :mod:`swnx_switch.ir` documents into a defensible answer, so every check
states what it measured, what it expected, and whether the evidence existed at
all.

Design rules
------------
* **Missing evidence is ``unavailable``, never a pass and never an exception.**
  Drivers may omit any optional field (see the IR docstring); a validator that
  crashes or guesses would be worse than useless.
* **A required check that is unavailable makes the overall verdict ``fail``.**
  "Could not verify" must not be reported as "verified": the pipeline treats a
  pass as permission to ship the converted part.
* **Tolerances are explicit numbers**, merged from :data:`DEFAULT_TOLERANCES`
  and overridable per call.  A clean STEP round-trip agrees to ~1e-6 relative;
  the defaults are deliberately looser so that real loss (missing bodies, unit
  errors, tessellation) is still caught while kernel-noise does not fail.
* **Informational checks never change the verdict.**  Face/edge counts and the
  feature tree legitimately change when a B-Rep crosses STEP, so they are
  reported (status ``warn`` for visibility) with ``severity="info"``.

The comparison dict shape is public API::

    {"schema": "swnx.compare/1",
     "verdict": "pass" | "warn" | "fail",
     "checks": [{"id", "label", "status", "detail", "expected", "actual",
                 "delta", "tolerance", "unit", "severity"}, ...],
     "summary": {...}, "tolerances": {...},
     "source": {...}, "candidate": {...},
     "warnings": [...], "limitations": [...]}

``status`` is one of ``pass | warn | fail | unavailable``; ``delta`` and
``tolerance`` are numeric whenever a measurement exists.  Units are millimetres
(the IR contract), kilograms, and kg/m3 for density.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping
from typing import Any

from . import ir

COMPARE_SCHEMA = "swnx.compare/1"

STATUSES = ("pass", "warn", "fail", "unavailable")
SEVERITIES = ("required", "optional", "info")

#: Tolerance defaults.  Every value is absolute unless its name ends in ``_rel``
#: (relative to the reference value).  Rationale per key:
#:
#: * ``volume_rel`` / ``volume_abs_mm3`` - clean STEP/Parasolid round-trips
#:   agree to ~1e-6 relative; 1e-3 catches a lost body or a unit error while
#:   tolerating kernel mass-property noise.  The absolute floor keeps tiny
#:   parts (volume ~0) from dividing by zero.
#: * ``area_rel`` / ``area_abs_mm2`` - area is summed over faces, so it is
#:   noisier than volume through STEP (faces may be split, seams added).
#: * ``bbox_abs_mm`` / ``bbox_rel`` - an exported part should keep its size to
#:   better than 0.05 mm; the relative part protects large parts.
#: * ``bbox_corner_abs_mm`` - max Euclidean deviation of the 8 bounding-box
#:   corners; catches a rigid offset or a rotated placement that per-axis
#:   sizes cannot see.  Kept generous (0.1 mm) because it combines 3 axes.
#: * ``centroid_abs_mm`` - a moved/rotated part shows up here even when volume
#:   and bbox size match.
#: * ``mass_rel`` / ``mass_abs_kg`` - mass inherits volume and density error.
#: * ``density_rel`` - density is a material lookup, not a geometry result, so
#:   both sides must agree almost exactly; a mismatch means one driver read a
#:   different material.
DEFAULT_TOLERANCES: dict[str, float] = {
    "volume_rel": 1e-3,
    "volume_abs_mm3": 1e-3,
    "area_rel": 2e-3,
    "area_abs_mm2": 1e-3,
    "bbox_abs_mm": 0.05,
    "bbox_rel": 1e-3,
    "bbox_corner_abs_mm": 0.1,
    "centroid_abs_mm": 0.05,
    "mass_rel": 1e-3,
    "mass_abs_kg": 1e-9,
    "density_rel": 1e-6,
}

#: Limitations always attached to a comparison; the report repeats them so a
#: human does not over-read a green verdict.
DEFAULT_LIMITATIONS = (
    "验证基于 IR 中的质量属性 (mass properties)、包围盒 (bounding box)、实体数量与特征树；"
    "IR 未提供的字段标记为 unavailable，不做推测。",
    "STEP/Parasolid 交换的是 B-Rep 几何，参数化特征树 (feature tree) 无法保留；"
    "特征差异仅作信息记录，不参与结论。",
    "面/边计数 (face/edge counts) 可能因 STEP 导出重排或切分面而变化，仅作信息记录。",
    "本报告不检查 PMI/标注、装配约束、颜色与外观。",
)


# --------------------------------------------------------------------------- #
# small numeric / JSON helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _num(value: Any) -> float | None:
    """Return a finite float, or ``None`` when the value is not usable evidence.

    Bools are rejected on purpose: ``True`` would silently compare as ``1``.
    Numeric strings are accepted because a hand-written or third-party IR may
    quote numbers; the schema prefers real numbers.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _vec3(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out = [_num(item) for item in value]
    if any(item is None for item in out):
        return None
    return [float(item) for item in out]  # type: ignore[arg-type]


def _round(value: float | None, digits: int = 9) -> float | None:
    return None if value is None else round(float(value), digits)


def _clean(value: Any) -> Any:
    """Make a value safe for the JSON report (floats rounded, no NaN)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        number = _num(value)
        return _round(number) if number is not None else None
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    return str(value)


def _g(value: float) -> str:
    """Compact number for a human-readable detail string."""
    return f"{value:.6g}"


def _model(doc: Any) -> dict:
    if isinstance(doc, dict) and isinstance(doc.get("model"), dict):
        return doc["model"]
    return {}


def _props(doc: Any) -> dict:
    props = _model(doc).get("mass_properties")
    return props if isinstance(props, dict) else {}


def _diagnostics(doc: Any) -> dict:
    if isinstance(doc, dict) and isinstance(doc.get("diagnostics"), dict):
        return doc["diagnostics"]
    return {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


# --------------------------------------------------------------------------- #
# measurement extraction
# --------------------------------------------------------------------------- #
def _bodies(doc: Any) -> dict | None:
    """Body count/type/topology totals, or ``None`` when the field is absent.

    ``None`` and ``[]`` are deliberately different: an absent ``bodies`` key
    means the driver did not report anything, while an empty list is a valid
    measurement of zero bodies.
    """
    bodies = _model(doc).get("bodies")
    if not isinstance(bodies, list):
        return None
    facts: dict[str, Any] = {"count": len(bodies), "solid": 0, "types": {}, "faces": None, "edges": None}
    faces_total = 0
    edges_total = 0
    have_faces = False
    have_edges = False
    for body in bodies:
        if not isinstance(body, dict):
            continue
        kind = str(body.get("type") or "unknown")
        facts["types"][kind] = facts["types"].get(kind, 0) + 1
        if kind == "solid":
            facts["solid"] += 1
        faces = _num(body.get("faces"))
        if faces is not None:
            faces_total += int(faces)
            have_faces = True
        edges = _num(body.get("edges"))
        if edges is not None:
            edges_total += int(edges)
            have_edges = True
    if have_faces:
        facts["faces"] = faces_total
    if have_edges:
        facts["edges"] = edges_total
    return facts


def _axes3(value: Any) -> list[float | None]:
    """Three-axis vector that keeps per-axis holes (``[110, 60]`` -> z missing)."""
    if not isinstance(value, (list, tuple)):
        return [None, None, None]
    return [_num(value[index]) if index < len(value) else None for index in range(3)]


def _bbox(doc: Any) -> dict:
    """Bounding box as ``size``/``min``/``max`` axis lists (``None`` per axis).

    ``size_mm`` is preferred; a missing or partial size is derived from
    ``min_mm``/``max_mm`` when possible so a driver that only filled the
    corners can still be checked.  Per-axis holes are preserved: a two-element
    ``size_mm`` marks only the Z check unavailable instead of discarding all
    three axes.
    """
    bbox = _model(doc).get("bounding_box")
    if not isinstance(bbox, dict):
        return {"size": [None, None, None], "min": None, "max": None, "derived": False, "present": False}
    lo_axes = _axes3(bbox.get("min_mm"))
    hi_axes = _axes3(bbox.get("max_mm"))
    lo = lo_axes if all(axis is not None for axis in lo_axes) else None
    hi = hi_axes if all(axis is not None for axis in hi_axes) else None
    axes: list[float | None] = _axes3(bbox.get("size_mm"))
    derived = False
    for index in range(3):
        if axes[index] is None and lo is not None and hi is not None:
            axes[index] = hi[index] - lo[index]  # type: ignore[operator]
            derived = True
    return {"size": axes, "min": lo, "max": hi, "derived": derived, "present": True}


def _features(doc: Any) -> list[dict] | None:
    features = _model(doc).get("features")
    if not isinstance(features, list):
        return None
    out: list[dict] = []
    for feature in features:
        if isinstance(feature, dict):
            out.append(
                {
                    "name": str(feature.get("name") or ""),
                    "type": str(feature.get("type") or ""),
                    "suppressed": bool(feature.get("suppressed", False)),
                }
            )
        else:
            out.append({"name": str(feature), "type": "", "suppressed": False})
    return out


def _density(doc: Any) -> float | None:
    density = _num(_props(doc).get("density_kg_m3"))
    if density is None:
        material = _model(doc).get("material")
        if isinstance(material, dict):
            density = _num(material.get("density_kg_m3"))
    return density


def _material_name(doc: Any) -> str | None:
    material = _model(doc).get("material")
    if isinstance(material, dict):
        name = material.get("name")
        if name:
            return str(name)
    return None


def _units(doc: Any) -> str | None:
    units = _model(doc).get("units")
    return str(units) if units is not None else None


# --------------------------------------------------------------------------- #
# check construction
# --------------------------------------------------------------------------- #
def _check(
    check_id: str,
    label: str,
    status: str,
    detail: str,
    *,
    expected: Any = None,
    actual: Any = None,
    delta: float | None = None,
    tolerance: float | None = None,
    unit: str = "",
    severity: str = "required",
    extra: dict | None = None,
) -> dict:
    entry: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": str(detail),
        "expected": _clean(expected),
        "actual": _clean(actual),
        "delta": _round(delta),
        "tolerance": _round(tolerance, 12),
        "unit": unit,
        "severity": severity,
    }
    if extra:
        entry.update({str(key): _clean(value) for key, value in extra.items()})
    return entry


def _unavailable(check_id: str, label: str, detail: str, *, severity: str = "required", unit: str = "") -> dict:
    return _check(check_id, label, "unavailable", detail, severity=severity, unit=unit)


def _scalar_check(
    check_id: str,
    label: str,
    expected: float | None,
    actual: float | None,
    *,
    rel_tol: float,
    abs_tol: float,
    unit: str,
    severity: str = "required",
    missing: str | None = None,
    fail_hint: str = "",
) -> dict:
    """Compare one scalar measurement with a relative tolerance and abs floor."""
    if expected is None or actual is None:
        detail = missing or "缺少测量值：参考件或转换件未提供该字段，无法核对 (unavailable)"
        return _unavailable(check_id, label, detail, severity=severity, unit=unit)
    delta = actual - expected
    tolerance = max(abs_tol, rel_tol * abs(expected))
    status = "pass" if abs(delta) <= tolerance else "fail"
    delta_rel = None if expected == 0 else delta / abs(expected)
    detail = (
        f"实测 {_g(actual)}，期望 {_g(expected)} {unit}；"
        f"偏差 {_g(delta)} {unit}，容差 ±{_g(tolerance)} {unit}"
    )
    if delta_rel is not None:
        detail += f"（相对偏差 {delta_rel:+.3%}）"
    if fail_hint and status == "fail":
        detail += "；" + fail_hint
    return _check(
        check_id,
        label,
        status,
        detail,
        expected=expected,
        actual=actual,
        delta=delta,
        tolerance=tolerance,
        unit=unit,
        severity=severity,
        extra={"delta_rel": None if delta_rel is None else _round(delta_rel, 12), "tolerance_rel": rel_tol},
    )


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #
def _check_ir_schema(reference: dict, candidate: dict) -> dict:
    """Is each document a well-formed ``swnx.ir/1`` object?

    Why: a driver can emit a JSON file that looks plausible but breaks the IR
    contract (missing ``geometry_digest`` buckets, ``bodies`` not a list).  The
    numeric checks below still read what they can, but the report must say the
    evidence came from a malformed document.
    """
    problems: list[str] = []
    for role, doc in (("参考件", reference), ("转换件", candidate)):
        if not isinstance(doc, dict):
            problems.append(f"{role}不是 JSON object")
            continue
        for problem in ir.validate(doc):
            problems.append(f"{role}: {problem}")
    if not problems:
        return _check("ir_schema", "IR 结构 (schema)", "pass", "两份文档均通过 swnx.ir/1 结构校验", severity="optional")
    detail = "结构异常：" + "；".join(problems[:6])
    if len(problems) > 6:
        detail += f" 等 {len(problems)} 项"
    return _check("ir_schema", "IR 结构 (schema)", "warn", detail, severity="optional")


def _check_units(reference: dict, candidate: dict) -> dict:
    """Both documents must declare millimetres; a unit mismatch invalidates every number.

    Why: the IR contract is mm-only, but a broken unit conversion is exactly
    the failure mode this validator exists for, so the units claim is checked
    before any numeric comparison.
    """
    ref_units, cand_units = _units(reference), _units(candidate)
    if ref_units is None and cand_units is None:
        return _check(
            "units",
            "单位 (units)",
            "warn",
            "两份文档均未声明单位；按 IR 约定视为 mm",
            severity="required",
        )
    if ref_units is None or cand_units is None:
        missing = "参考件" if ref_units is None else "转换件"
        return _check(
            "units",
            "单位 (units)",
            "warn",
            f"{missing}未声明单位；其余数值按 mm 处理（IR 约定）",
            severity="required",
        )
    if ref_units == cand_units == "mm":
        return _check("units", "单位 (units)", "pass", "两侧单位均为 mm", expected="mm", actual="mm", severity="required")
    return _check(
        "units",
        "单位 (units)",
        "fail",
        f"单位不一致：{ref_units} != {cand_units}，数值不可直接比较（疑似单位换算错误）",
        expected=ref_units,
        actual=cand_units,
        severity="required",
    )


def _check_body_count(reference: dict, candidate: dict) -> dict:
    """The converted part must expose the same number of bodies.

    Why: a dropped body is the single most common silent STEP/Parasolid loss.
    """
    ref_bodies, cand_bodies = _bodies(reference), _bodies(candidate)
    if ref_bodies is None or cand_bodies is None:
        missing = "参考件 " if ref_bodies is None else ""
        missing += "转换件" if cand_bodies is None else ""
        return _unavailable("body_count", "实体数量 (bodies)", f"{missing}缺少 model.bodies，无法核对")
    expected, actual = ref_bodies["count"], cand_bodies["count"]
    base = {
        "expected": expected,
        "actual": actual,
        "delta": float(actual - expected),
        "tolerance": 0.0,
        "extra": {"expected_types": ref_bodies["types"], "actual_types": cand_bodies["types"]},
    }
    if expected != actual:
        return _check(
            "body_count",
            "实体数量 (bodies)",
            "fail",
            f"实体数量 {expected} -> {actual}；转换可能丢失或拆分实体 (body loss/split)",
            **base,
        )
    if ref_bodies["solid"] != cand_bodies["solid"]:
        return _check(
            "body_count",
            "实体数量 (bodies)",
            "warn",
            f"实体总数一致（{expected}），但实体类型分布不同：solid {ref_bodies['solid']} -> {cand_bodies['solid']}",
            **base,
        )
    return _check("body_count", "实体数量 (bodies)", "pass", f"实体数量一致（{expected}）", **base)


def _check_volume(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Relative volume error; the primary "is it the same solid" signal.

    Why: volume is integrated from the B-Rep, so any lost body, inverted shell
    or unit error moves it immediately.  It is also the cheapest mass property
    every CAD API exposes.
    """
    return _scalar_check(
        "volume",
        "体积 (volume)",
        _num(_props(reference).get("volume_mm3")),
        _num(_props(candidate).get("volume_mm3")),
        rel_tol=tol["volume_rel"],
        abs_tol=tol["volume_abs_mm3"],
        unit="mm3",
        missing="mass_properties.volume_mm3 缺失（参考件或转换件未提供），无法核对",
        fail_hint="体积差异超出容差：可能是实体丢失、壳面反向或单位错误",
    )


def _check_surface_area(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Relative surface-area error.

    Why: area catches the case where volume survives but the surface was
    tessellated or re-trimmed (a shell can enclose the same volume with a very
    different area).  The default tolerance is looser than volume because STEP
    may split faces and add seam edges.
    """
    return _scalar_check(
        "surface_area",
        "表面积 (area)",
        _num(_props(reference).get("surface_area_mm2")),
        _num(_props(candidate).get("surface_area_mm2")),
        rel_tol=tol["area_rel"],
        abs_tol=tol["area_abs_mm2"],
        unit="mm2",
        missing="mass_properties.surface_area_mm2 缺失（参考件或转换件未提供），无法核对",
        fail_hint="表面积差异超出容差：可能是面被裁剪、合并或 tessellation 化",
    )


def _check_bbox_axis(reference: dict, candidate: dict, axis: int, tol: Mapping[str, float]) -> dict:
    """Per-axis bounding-box size, absolute + relative tolerance.

    Why: volume can be preserved while the part is scaled or rotated; the box
    is the cheapest placement/size sanity check.  Per-axis checks make the
    failure report name the offending axis instead of hiding it in one number.
    """
    ref_bbox, cand_bbox = _bbox(reference), _bbox(candidate)
    name = ("X", "Y", "Z")[axis]
    check_id = f"bbox_{name.lower()}"
    label = f"包围盒 {name} (bbox {name.lower()})"
    expected, actual = ref_bbox["size"][axis], cand_bbox["size"][axis]
    if expected is None or actual is None:
        return _unavailable(check_id, label, f"包围盒 {name} 轴尺寸缺失（size_mm/min_mm/max_mm 不足）")
    check = _scalar_check(
        check_id,
        label,
        expected,
        actual,
        rel_tol=tol["bbox_rel"],
        abs_tol=tol["bbox_abs_mm"],
        unit="mm",
        fail_hint="该轴尺寸差异超出容差：可能是缩放、单位错误或几何被裁剪",
    )
    if ref_bbox["derived"] or cand_bbox["derived"]:
        check["detail"] += "（尺寸由 min/max 推导）"
        check["derived"] = True
    return check


def _check_bbox_corner(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Max Euclidean deviation between corresponding bounding-box corners.

    Why: per-axis sizes are blind to a part that was translated or rotated as a
    rigid body.  Comparing corner positions catches placement changes; it is
    optional because a legitimate re-origin through STEP is not "loss".
    """
    ref_bbox, cand_bbox = _bbox(reference), _bbox(candidate)
    if ref_bbox["min"] is None or ref_bbox["max"] is None or cand_bbox["min"] is None or cand_bbox["max"] is None:
        return _unavailable(
            "bbox_corner",
            "包围盒角点 (corners)",
            "缺少 min_mm/max_mm（至少一侧），无法计算角点偏差",
            severity="optional",
            unit="mm",
        )
    ref_size = [ref_bbox["max"][i] - ref_bbox["min"][i] for i in range(3)]
    diagonal = math.sqrt(sum(size * size for size in ref_size))
    tolerance = max(tol["bbox_corner_abs_mm"], tol["bbox_rel"] * diagonal)
    max_deviation = 0.0
    for mask in range(8):
        distance_sq = 0.0
        for axis in range(3):
            low, high = ref_bbox["min"][axis], ref_bbox["max"][axis]
            ref_corner = high if (mask >> axis) & 1 else low
            cand_corner = cand_bbox["max"][axis] if (mask >> axis) & 1 else cand_bbox["min"][axis]
            distance_sq += (cand_corner - ref_corner) ** 2
        max_deviation = max(max_deviation, math.sqrt(distance_sq))
    status = "pass" if max_deviation <= tolerance else "warn"
    detail = (
        f"最大角点偏差 {_g(max_deviation)} mm，容差 ±{_g(tolerance)} mm；"
        "整体平移或放置变化会体现在这里（仅提示）"
    )
    return _check(
        "bbox_corner",
        "包围盒角点 (corners)",
        status,
        detail,
        expected=0.0,
        actual=max_deviation,
        delta=max_deviation,
        tolerance=tolerance,
        unit="mm",
        severity="optional",
        extra={"max_corner_mm": max_deviation, "diagonal_mm": diagonal},
    )


def _check_centroid(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Euclidean centroid deviation, only when both sides report a centroid.

    Why: a part can keep volume, area and bbox size while being mirrored or
    re-origined; the centroid exposes that.  Optional because some driver
    paths cannot read it.
    """
    ref_centroid = _vec3(_props(reference).get("centroid_mm"))
    cand_centroid = _vec3(_props(candidate).get("centroid_mm"))
    if ref_centroid is None or cand_centroid is None:
        missing = "参考件" if ref_centroid is None else "转换件"
        return _unavailable(
            "centroid",
            "质心 (centroid)",
            f"{missing}未提供 centroid_mm，无法核对（仅双方都有时检查）",
            severity="optional",
            unit="mm",
        )
    distance = math.sqrt(sum((cand_centroid[i] - ref_centroid[i]) ** 2 for i in range(3)))
    tolerance = tol["centroid_abs_mm"]
    status = "pass" if distance <= tolerance else "fail"
    detail = f"质心偏差 {_g(distance)} mm，容差 ±{_g(tolerance)} mm"
    if status == "fail":
        detail += "；可能是整体平移/镜像或几何丢失"
    return _check(
        "centroid",
        "质心 (centroid)",
        status,
        detail,
        expected=ref_centroid,
        actual=cand_centroid,
        delta=distance,
        tolerance=tolerance,
        unit="mm",
        severity="optional",
    )


def _check_mass(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Relative mass error when both documents carry ``mass_kg``.

    Why: mass is what a downstream simulation/quoting step consumes; it also
    cross-checks volume and density in one number.
    """
    return _scalar_check(
        "mass",
        "质量 (mass)",
        _num(_props(reference).get("mass_kg")),
        _num(_props(candidate).get("mass_kg")),
        rel_tol=tol["mass_rel"],
        abs_tol=tol["mass_abs_kg"],
        unit="kg",
        severity="optional",
        missing="mass_properties.mass_kg 缺失（参考件或转换件未提供），无法核对",
        fail_hint="质量差异超出容差：可能是体积或密度读取错误",
    )


def _check_density(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Density agreement when a material is present.

    Why: volume and mass can both be right while the importer pulled a
    different material; density is a lookup, so it should match almost exactly.
    """
    ref_density, cand_density = _density(reference), _density(candidate)
    if ref_density is None and cand_density is None:
        return _unavailable(
            "density",
            "密度 (density)",
            "两侧均未提供材料密度 (density_kg_m3)，无法核对",
            severity="optional",
            unit="kg/m3",
        )
    if ref_density is None or cand_density is None:
        missing = "参考件" if ref_density is None else "转换件"
        return _unavailable(
            "density",
            "密度 (density)",
            f"{missing}未提供材料密度，无法核对",
            severity="optional",
            unit="kg/m3",
        )
    tolerance = max(1e-9, tol["density_rel"] * abs(ref_density))
    delta = cand_density - ref_density
    status = "pass" if abs(delta) <= tolerance else "fail"
    ref_material, cand_material = _material_name(reference), _material_name(candidate)
    detail = f"实测 {_g(cand_density)}，期望 {_g(ref_density)} kg/m3，容差 ±{_g(tolerance)} kg/m3"
    if ref_material != cand_material:
        detail += f"；材料名 {ref_material or '-'} -> {cand_material or '-'}"
    return _check(
        "density",
        "密度 (density)",
        status,
        detail,
        expected=ref_density,
        actual=cand_density,
        delta=delta,
        tolerance=tolerance,
        unit="kg/m3",
        severity="optional",
    )


def _check_mass_density(reference: dict, candidate: dict, tol: Mapping[str, float]) -> dict:
    """Does ``mass_kg`` agree with ``volume_mm3 * density_kg_m3``?

    Why: this is the unit-conversion tripwire.  A driver that reads m^3 as mm^3
    (SolidWorks reports metres) produces a mass off by 1e9 while volume and
    density individually look plausible.  A CAD part may legitimately override
    its mass, so an inconsistency is a warning, not a failure.
    """
    volume = _num(_props(candidate).get("volume_mm3"))
    density = _density(candidate)
    mass = _num(_props(candidate).get("mass_kg"))
    if not (volume is not None and density is not None and mass is not None):
        return _unavailable(
            "mass_density",
            "质量一致 (mass/density)",
            "转换件缺少 volume_mm3/density_kg_m3/mass_kg 组合，无法做单位一致性核对",
            severity="optional",
            unit="kg",
        )
    side = "转换件"
    expected_mass = volume * density * 1e-9
    tolerance = max(tol["mass_abs_kg"], tol["mass_rel"] * abs(expected_mass))
    delta = mass - expected_mass
    status = "pass" if abs(delta) <= tolerance else "warn"
    detail = (
        f"{side}质量 {_g(mass)} kg，体积×密度推算 {_g(expected_mass)} kg；"
        f"偏差 {_g(delta)} kg（容差 ±{_g(tolerance)} kg）"
    )
    if status == "warn":
        detail += "；可能质量被 CAD 覆盖或密度/体积单位换算有误（仅提示）"
    return _check(
        "mass_density",
        "质量一致 (mass/density)",
        status,
        detail,
        expected=expected_mass,
        actual=mass,
        delta=delta,
        tolerance=tolerance,
        unit="kg",
        severity="optional",
        extra={"checked_side": side},
    )


def _check_topology(reference: dict, candidate: dict) -> dict:
    """Face/edge count delta, informational only.

    Why: STEP exporters may reorder, split or merge faces and add seam edges on
    cylinders; a non-zero delta is expected and must not fail a good
    conversion.  It is still reported because a huge jump can reveal
    tessellation.
    """
    ref_bodies, cand_bodies = _bodies(reference), _bodies(candidate)
    if ref_bodies is None or cand_bodies is None:
        return _unavailable(
            "topology",
            "面/边计数 (faces/edges)",
            "缺少 model.bodies，无法比较面/边计数",
            severity="info",
        )
    ref_faces, cand_faces = ref_bodies["faces"], cand_bodies["faces"]
    ref_edges, cand_edges = ref_bodies["edges"], cand_bodies["edges"]
    if ref_faces is None and ref_edges is None and cand_faces is None and cand_edges is None:
        return _unavailable(
            "topology",
            "面/边计数 (faces/edges)",
            "两侧 bodies 均未提供 faces/edges 计数",
            severity="info",
        )
    face_delta = None if ref_faces is None or cand_faces is None else cand_faces - ref_faces
    edge_delta = None if ref_edges is None or cand_edges is None else cand_edges - ref_edges
    total_delta = abs(face_delta or 0) + abs(edge_delta or 0)
    status = "pass" if total_delta == 0 else "warn"
    ref_faces_text = ref_faces if ref_faces is not None else "-"
    cand_faces_text = cand_faces if cand_faces is not None else "-"
    ref_edges_text = ref_edges if ref_edges is not None else "-"
    cand_edges_text = cand_edges if cand_edges is not None else "-"
    face_text = f"面 {ref_faces_text} -> {cand_faces_text}"
    if face_delta is not None:
        face_text += f"（Δ{face_delta:+d}）"
    edge_text = f"边 {ref_edges_text} -> {cand_edges_text}"
    if edge_delta is not None:
        edge_text += f"（Δ{edge_delta:+d}）"
    detail = f"{face_text}，{edge_text}；STEP 可能重排/切分面，仅作信息参考"
    return _check(
        "topology",
        "面/边计数 (faces/edges)",
        status,
        detail,
        expected={"faces": ref_faces, "edges": ref_edges},
        actual={"faces": cand_faces, "edges": cand_edges},
        delta=float(total_delta),
        tolerance=0.0,
        unit="count",
        severity="info",
        extra={"face_delta": face_delta, "edge_delta": edge_delta},
    )


def _normalise_feature(feature: dict) -> tuple[str, str]:
    return (str(feature.get("name") or "").strip().lower(), str(feature.get("type") or "").strip().lower())


def _check_features(reference: dict, candidate: dict) -> dict:
    """Which parametric features did not survive, informational only.

    Why: a geometry exchange cannot carry a feature tree, so STEP -> native
    always loses parametrics.  Reporting the lost names is still useful (it
    tells a human what would need rebuilding), but it must never fail the
    conversion verdict.
    """
    ref_features, cand_features = _features(reference), _features(candidate)
    if ref_features is None or cand_features is None:
        return _unavailable(
            "feature_tree",
            "特征树 (features)",
            "缺少 model.features，无法比较特征树",
            severity="info",
        )
    if not ref_features and not cand_features:
        return _check(
            "feature_tree",
            "特征树 (features)",
            "pass",
            "两份文档均未暴露特征树（几何交换不保留参数化特征）",
            expected=0,
            actual=0,
            delta=0.0,
            tolerance=0.0,
            severity="info",
        )
    ref_map = {_normalise_feature(feature): feature for feature in ref_features}
    cand_map = {_normalise_feature(feature): feature for feature in cand_features}
    lost = [feature["name"] for key, feature in ref_map.items() if key not in cand_map]
    added = [feature["name"] for key, feature in cand_map.items() if key not in ref_map]
    type_changed = [
        ref_map[key]["name"]
        for key in ref_map.keys() & cand_map.keys()
        if ref_map[key]["type"] != cand_map[key]["type"]
    ]
    changed = bool(lost or added or type_changed)
    delta = float(len(lost) + len(type_changed))
    if not changed:
        return _check(
            "feature_tree",
            "特征树 (features)",
            "pass",
            f"特征树一致（{len(ref_features)} 个特征）",
            expected=len(ref_features),
            actual=len(cand_features),
            delta=0.0,
            tolerance=0.0,
            severity="info",
        )
    parts = []
    if lost:
        parts.append(f"丢失 {len(lost)} 个: " + "、".join(lost[:8]) + (" ..." if len(lost) > 8 else ""))
    if added:
        parts.append(f"新增 {len(added)} 个: " + "、".join(added[:5]) + (" ..." if len(added) > 5 else ""))
    if type_changed:
        parts.append(f"类型变化 {len(type_changed)} 个: " + "、".join(type_changed[:5]))
    detail = "特征树差异：" + "；".join(parts) + "（参数化特征无法通过 STEP/Parasolid 保留，仅信息）"
    return _check(
        "feature_tree",
        "特征树 (features)",
        "warn",
        detail,
        expected={"count": len(ref_features), "names": [f["name"] for f in ref_features[:10]]},
        actual={"count": len(cand_features), "names": [f["name"] for f in cand_features[:10]]},
        delta=delta,
        tolerance=0.0,
        severity="info",
        extra={"lost": lost[:20], "added": added[:20], "type_changed": type_changed[:20]},
    )


# --------------------------------------------------------------------------- #
# document summaries
# --------------------------------------------------------------------------- #
def _document_summary(doc: Any, role: str) -> dict:
    """Stable, report-ready summary of one side of the comparison."""
    safe = doc if isinstance(doc, dict) else {}
    source = safe.get("source") if isinstance(safe.get("source"), dict) else {}
    model = _model(safe)
    props = _props(safe)
    bbox = _bbox(safe)
    bodies = _bodies(safe)
    features = _features(safe)
    diagnostics = _diagnostics(safe)
    generator = safe.get("generator") if isinstance(safe.get("generator"), dict) else {}
    bbox_size = bbox["size"] if any(axis is not None for axis in bbox["size"]) else None
    return {
        "role": role,
        "path": source.get("path"),
        "format": source.get("format"),
        "sha256": source.get("sha256"),
        "app": source.get("app"),
        "app_version": source.get("app_version"),
        "name": model.get("name"),
        "kind": model.get("kind"),
        "units": model.get("units"),
        "generated_at": safe.get("generated_at"),
        "generator": generator,
        "bodies": bodies["count"] if bodies is not None else None,
        "solid_bodies": bodies["solid"] if bodies is not None else None,
        "features": len(features) if features is not None else None,
        "volume_mm3": _num(props.get("volume_mm3")),
        "surface_area_mm2": _num(props.get("surface_area_mm2")),
        "mass_kg": _num(props.get("mass_kg")),
        "density_kg_m3": _density(safe),
        "material": _material_name(safe),
        "bbox_size_mm": bbox_size,
        "warnings": _string_list(diagnostics.get("warnings")),
        "limits": _string_list(diagnostics.get("limits")),
    }


# --------------------------------------------------------------------------- #
# tolerances
# --------------------------------------------------------------------------- #
def _effective_tolerances(overrides: Mapping[str, Any] | None) -> tuple[dict[str, float], list[str]]:
    """Merge per-call overrides onto :data:`DEFAULT_TOLERANCES`.

    Invalid overrides (negative, non-numeric) are ignored with a note instead
    of raising: a report that explains a bad tolerance argument is more useful
    to an agent than a traceback.
    """
    effective: dict[str, float] = {key: float(value) for key, value in DEFAULT_TOLERANCES.items()}
    notes: list[str] = []
    if overrides is None:
        return effective, notes
    if not isinstance(overrides, Mapping):
        raise TypeError("tolerances must be a mapping of tolerance name -> non-negative number")
    for key, value in overrides.items():
        name = str(key)
        number = _num(value)
        if number is None or number < 0:
            notes.append(f"tolerances: 忽略非法覆盖 {name}={value!r}（应为非负数字）")
            continue
        effective[name] = number
    return effective, notes


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #
def verdict_of(result: dict) -> str:
    """Compute ``pass | warn | fail`` from a comparison result's checks.

    Rules (documented because the pipeline's behaviour depends on them):

    * any check ``fail`` -> ``fail``;
    * a ``required`` check that is ``unavailable`` -> ``fail`` (cannot verify
      is not verified);
    * a ``warn`` on a ``required`` or ``optional`` check -> ``warn``;
    * ``info``-severity checks never change the verdict;
    * no checks at all -> ``fail``.
    """
    checks = result.get("checks") if isinstance(result, dict) else None
    if not isinstance(checks, list) or not checks:
        return "fail"
    entries = [check for check in checks if isinstance(check, dict)]
    if len(entries) != len(checks):
        return "fail"
    if any(check.get("status") not in STATUSES or check.get("severity") not in SEVERITIES for check in entries):
        return "fail"
    if any(check.get("status") == "fail" for check in entries):
        return "fail"
    if any(check.get("severity") == "required" and check.get("status") == "unavailable" for check in entries):
        return "fail"
    if any(
        check.get("status") == "warn" and check.get("severity") in ("required", "optional")
        for check in entries
    ):
        return "warn"
    return "pass"


def _summarise(checks: list[dict], verdict: str) -> dict:
    counts = {status: 0 for status in STATUSES}
    for check in checks:
        status = check.get("status")
        if status in counts:
            counts[status] += 1
    required_unavailable = [
        check["id"] for check in checks if check.get("severity") == "required" and check.get("status") == "unavailable"
    ]
    failed = [check["id"] for check in checks if check.get("status") == "fail"]
    warned = [
        check["id"]
        for check in checks
        if check.get("status") == "warn" and check.get("severity") in ("required", "optional")
    ]
    informational = [
        check["id"]
        for check in checks
        if check.get("status") == "warn" and check.get("severity") == "info"
    ]
    if verdict == "pass":
        basis = "所有必需检查通过 (all required checks passed)"
        if informational:
            basis += "；信息性差异: " + ", ".join(informational) + "（不影响结论）"
    elif verdict == "warn":
        basis = "无失败项，但存在警告: " + ", ".join(warned)
    else:
        reasons = []
        if failed:
            reasons.append("失败: " + ", ".join(failed))
        if required_unavailable:
            reasons.append("无法确认（必需检查不可用）: " + ", ".join(required_unavailable))
        basis = "；".join(reasons) if reasons else "检查不完整"
    return {
        "checks": len(checks),
        **counts,
        "required_unavailable": required_unavailable,
        "failed": failed,
        "warned": warned,
        "informational": informational,
        "verdict_basis": basis,
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def compare(reference: dict, candidate: dict, tolerances: dict | None = None) -> dict:
    """Compare a reference IR document against a converted candidate.

    Parameters
    ----------
    reference:
        IR document of the source part (what the conversion started from).
    candidate:
        IR document of the converted part (what the conversion produced).
    tolerances:
        Optional overrides merged onto :data:`DEFAULT_TOLERANCES`; e.g.
        ``{"volume_rel": 5e-3, "bbox_abs_mm": 0.2}``.  Extra/unknown keys are
        echoed into ``result["tolerances"]`` and otherwise ignored.

    Returns
    -------
    dict
        ``{"schema": "swnx.compare/1", "verdict", "checks", "summary",
        "tolerances", "source", "candidate", "warnings", "limitations"}``.
        The function never raises on malformed documents: unusable fields
        become ``unavailable`` checks and make the verdict ``fail``.
    """
    reference = reference if isinstance(reference, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    effective, tolerance_notes = _effective_tolerances(tolerances)

    checks: list[dict] = [
        _check_ir_schema(reference, candidate),
        _check_units(reference, candidate),
        _check_body_count(reference, candidate),
        _check_volume(reference, candidate, effective),
        _check_surface_area(reference, candidate, effective),
        _check_bbox_axis(reference, candidate, 0, effective),
        _check_bbox_axis(reference, candidate, 1, effective),
        _check_bbox_axis(reference, candidate, 2, effective),
        _check_bbox_corner(reference, candidate, effective),
        _check_centroid(reference, candidate, effective),
        _check_mass(reference, candidate, effective),
        _check_density(reference, candidate, effective),
        _check_mass_density(reference, candidate, effective),
        _check_topology(reference, candidate),
        _check_features(reference, candidate),
    ]

    verdict = verdict_of({"checks": checks})
    summary = _summarise(checks, verdict)

    warnings: list[str] = list(tolerance_notes)
    source_summary = _document_summary(reference, "reference")
    candidate_summary = _document_summary(candidate, "candidate")
    for label, side in (("参考件", source_summary), ("转换件", candidate_summary)):
        for message in side["warnings"]:
            warnings.append(f"{label}驱动警告: {message}")

    limitations = list(DEFAULT_LIMITATIONS)
    for label, side in (("参考件", source_summary), ("转换件", candidate_summary)):
        for limit_note in side["limits"]:
            limitations.append(f"{label}驱动限制: {limit_note}")

    return {
        "schema": COMPARE_SCHEMA,
        "generated_at": _now(),
        "verdict": verdict,
        "checks": checks,
        "summary": summary,
        "tolerances": effective,
        "source": source_summary,
        "candidate": candidate_summary,
        "warnings": warnings,
        "limitations": limitations,
    }
