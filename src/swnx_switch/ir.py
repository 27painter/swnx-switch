"""The neutral CAD document model — the contract every driver writes and the
converter/validator/AI layer reads.

Design rules
------------
* **Plain JSON-able dicts**, not dataclasses.  Drivers are separate processes
  (SolidWorks COM, NX batch journal), so the model crosses a file boundary; a
  dict needs no shared runtime and an agent can read it directly.
* **Millimetres everywhere.**  SolidWorks works in metres, NX in mm depending on
  part units; each driver converts before it writes.  A value called ``*_mm`` is
  always millimetres.
* **Optional by default.**  Not every API read succeeds on every part.  A driver
  may omit a field and must add a warning instead of inventing a value.  The
  schema check only enforces the spine (schema/source/model) and the types of
  anything present.
* **Stable names.**  These keys are consumed by ``validate.py``, the MCP tools
  and the Markdown report; treat them as public API.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .errors import IRError

SCHEMA = "swnx.ir/1"

SURFACE_KINDS = ("plane", "cylinder", "cone", "sphere", "torus", "other")


# --------------------------------------------------------------------------- #
# identity helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_of(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new(
    *,
    backend: str,
    app: str,
    app_version: str = "unknown",
    source_path: str | os.PathLike | None = None,
    source_format: str = "unknown",
    kind: str = "part",
    name: str | None = None,
) -> dict:
    """Start a document. ``kind`` is part | assembly | drawing."""
    src_path = Path(source_path) if source_path else None
    source: dict[str, Any] = {
        "path": str(src_path) if src_path else None,
        "format": source_format,
        "size_bytes": src_path.stat().st_size if src_path and src_path.exists() else None,
        "sha256": None,
        "app": app,
        "app_version": app_version,
        "readonly": True,
        "notes": [],
    }
    if src_path and src_path.exists() and src_path.stat().st_size < (256 << 20):
        try:
            source["sha256"] = sha256_of(src_path)
        except OSError as exc:  # unreadable is a warning, not a failure
            source["notes"].append(f"sha256 unavailable: {exc}")

    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "generator": {"name": "swnx-switch", "version": __version__, "backend": backend},
        "source": source,
        "model": {
            "name": name or (src_path.stem if src_path else "unnamed"),
            "kind": kind,
            "units": "mm",
            "material": {},
            "mass_properties": {},
            "bounding_box": {},
            "bodies": [],
            "features": [],
            "sketches": [],
            "geometry_digest": {kind_: [] for kind_ in SURFACE_KINDS},
            "custom_properties": {},
            "configurations": [],
            "drawing": None,
            "assembly": None,
        },
        "diagnostics": {"warnings": [], "limits": [], "duration_s": None, "driver": {}},
    }


# --------------------------------------------------------------------------- #
# model builders — drivers call these so the shape stays uniform
# --------------------------------------------------------------------------- #
def warn(doc: dict, message: str) -> None:
    doc["diagnostics"]["warnings"].append(str(message))


def limit(doc: dict, message: str) -> None:
    doc["diagnostics"]["limits"].append(str(message))


def set_source(doc: dict, **fields: Any) -> None:
    doc["source"].update({k: v for k, v in fields.items() if v is not None})


def set_material(doc: dict, *, name: str | None = None, density_kg_m3: float | None = None) -> None:
    material = doc["model"]["material"]
    if name:
        material["name"] = name
    if density_kg_m3:
        material["density_kg_m3"] = float(density_kg_m3)


def set_mass_properties(
    doc: dict,
    *,
    volume_mm3: float | None = None,
    surface_area_mm2: float | None = None,
    mass_kg: float | None = None,
    density_kg_m3: float | None = None,
    centroid_mm: Iterable[float] | None = None,
    inertia_kg_mm2: list | None = None,
) -> None:
    props = doc["model"]["mass_properties"]
    for key, value in (
        ("volume_mm3", volume_mm3),
        ("surface_area_mm2", surface_area_mm2),
        ("mass_kg", mass_kg),
        ("density_kg_m3", density_kg_m3),
    ):
        if value is not None:
            props[key] = float(value)
    if centroid_mm is not None:
        props["centroid_mm"] = _vec(centroid_mm, 3)
    if inertia_kg_mm2 is not None:
        props["inertia_kg_mm2"] = [[float(x) for x in row] for row in inertia_kg_mm2]


def set_bbox(doc: dict, *, min_mm: Iterable[float], max_mm: Iterable[float]) -> None:
    lo, hi = _vec(min_mm, 3), _vec(max_mm, 3)
    doc["model"]["bounding_box"] = {
        "min_mm": lo,
        "max_mm": hi,
        "size_mm": [round(hi[i] - lo[i], 9) for i in range(3)],
    }


def add_body(
    doc: dict,
    *,
    name: str,
    kind: str = "solid",
    faces: int | None = None,
    edges: int | None = None,
    vertices: int | None = None,
    volume_mm3: float | None = None,
    area_mm2: float | None = None,
    min_mm: Iterable[float] | None = None,
    max_mm: Iterable[float] | None = None,
    closed: bool | None = None,
) -> dict:
    body: dict[str, Any] = {"name": str(name), "type": kind}
    for key, value in (
        ("faces", faces),
        ("edges", edges),
        ("vertices", vertices),
        ("volume_mm3", volume_mm3),
        ("area_mm2", area_mm2),
        ("closed", closed),
    ):
        if value is not None:
            body[key] = value
    if min_mm is not None and max_mm is not None:
        lo, hi = _vec(min_mm, 3), _vec(max_mm, 3)
        body["bbox"] = {"min_mm": lo, "max_mm": hi, "size_mm": [round(hi[i] - lo[i], 9) for i in range(3)]}
    doc["model"]["bodies"].append(body)
    return body


def add_feature(
    doc: dict,
    *,
    name: str,
    type: str,  # noqa: A002 - matches the JSON key, kept for readability at call sites
    suppressed: bool = False,
    parent: str | None = None,
    children: Iterable[str] | None = None,
    parameters: dict | None = None,
) -> dict:
    feature: dict[str, Any] = {
        "name": str(name),
        "type": str(type),
        "suppressed": bool(suppressed),
    }
    if parent:
        feature["parent"] = str(parent)
    if children:
        feature["children"] = [str(c) for c in children]
    if parameters:
        feature["parameters"] = _jsonable(parameters)
    doc["model"]["features"].append(feature)
    return feature


def add_sketch(
    doc: dict,
    *,
    name: str,
    plane: str | None = None,
    entities: int | None = None,
    dimensions: dict | None = None,
) -> dict:
    sketch: dict[str, Any] = {"name": str(name)}
    if plane:
        sketch["plane"] = str(plane)
    if entities is not None:
        sketch["entities"] = int(entities)
    if dimensions:
        sketch["dimensions"] = _jsonable(dimensions)
    doc["model"]["sketches"].append(sketch)
    return sketch


def add_surface(doc: dict, kind: str, **fields: Any) -> None:
    """Record one B-rep face group in ``model.geometry_digest``.

    ``kind`` is plane | cylinder | cone | sphere | torus | other.  Drivers pass
    millimetres and unit vectors; unknown keys are kept as-is so a driver can
    add evidence (``formula``, ``face_ids``) without a schema change.
    """
    if kind not in SURFACE_KINDS:
        kind = "other"
    entry = {"kind": kind}
    entry.update({key: _jsonable(value) for key, value in fields.items() if value is not None})
    doc["model"]["geometry_digest"][kind].append(entry)


def add_custom_properties(doc: dict, properties: dict) -> None:
    doc["model"]["custom_properties"].update({str(k): _jsonable(v) for k, v in properties.items()})


def add_configuration(doc: dict, *, name: str, active: bool = False) -> None:
    doc["model"]["configurations"].append({"name": str(name), "active": bool(active)})


def set_drawing(doc: dict, drawing: dict | None) -> None:
    doc["model"]["drawing"] = drawing


def set_assembly(doc: dict, assembly: dict | None) -> None:
    doc["model"]["assembly"] = assembly


def finish(doc: dict, *, duration_s: float | None = None, driver: dict | None = None) -> dict:
    if duration_s is not None:
        doc["diagnostics"]["duration_s"] = round(float(duration_s), 3)
    if driver:
        doc["diagnostics"]["driver"].update(_jsonable(driver))
    return doc


# --------------------------------------------------------------------------- #
# serialisation / validation
# --------------------------------------------------------------------------- #
def dump(doc: dict, path: str | os.PathLike) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target


def load(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    problems = validate(doc)
    if problems:
        raise IRError(f"not a valid {SCHEMA} document: {path}", detail="; ".join(problems))
    return doc


def validate(doc: Any) -> list[str]:
    """Return a list of schema problems; empty means usable."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["document is not an object"]
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema is {doc.get('schema')!r}, expected {SCHEMA!r}")
    for key in ("generated_at", "generator", "source", "model", "diagnostics"):
        if key not in doc:
            problems.append(f"missing top-level key {key!r}")
    model = doc.get("model")
    if isinstance(model, dict):
        for key in ("name", "kind", "units", "bodies", "features", "geometry_digest"):
            if key not in model:
                problems.append(f"missing model.{key}")
        if model.get("units") not in (None, "mm"):
            problems.append(f"model.units must be 'mm', got {model.get('units')!r}")
        if not isinstance(model.get("bodies"), list):
            problems.append("model.bodies must be a list")
        for body in model.get("bodies") or []:
            if not isinstance(body, dict) or "name" not in body:
                problems.append("model.bodies[] entries need a name")
        digest = model.get("geometry_digest")
        if isinstance(digest, dict):
            for kind in SURFACE_KINDS:
                if not isinstance(digest.get(kind), list):
                    problems.append(f"model.geometry_digest.{kind} must be a list")
        else:
            problems.append("model.geometry_digest must be an object")
    return problems


def summarize(doc: dict) -> dict:
    """Short machine summary used by the CLI and by agents before a conversion."""
    model = doc.get("model") or {}
    props = model.get("mass_properties") or {}
    bbox = model.get("bounding_box") or {}
    digest = model.get("geometry_digest") or {}
    return {
        "name": model.get("name"),
        "kind": model.get("kind"),
        "units": model.get("units"),
        "source_format": (doc.get("source") or {}).get("format"),
        "bodies": len(model.get("bodies") or []),
        "features": len(model.get("features") or []),
        "volume_mm3": props.get("volume_mm3"),
        "surface_area_mm2": props.get("surface_area_mm2"),
        "mass_kg": props.get("mass_kg"),
        "bbox_size_mm": bbox.get("size_mm"),
        "faces": sum(int(b.get("faces") or 0) for b in model.get("bodies") or []),
        "surfaces": {kind: len(digest.get(kind) or []) for kind in SURFACE_KINDS if digest.get(kind)},
        "warnings": list((doc.get("diagnostics") or {}).get("warnings") or []),
    }


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def _vec(values: Iterable[float], count: int) -> list[float]:
    out = [float(v) for v in values]
    if len(out) != count:
        raise IRError(f"expected {count} components, got {len(out)}")
    return [round(v, 9) for v in out]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float):
            return round(value, 9)
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)
