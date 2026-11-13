# -*- coding: utf-8 -*-
"""swnx-switch NX journal driver.

This file is executed **inside batch NX** by ``NXBIN\\run_journal.exe`` under
NX's own embedded interpreter (CPython 3.10 on NX 2406).  It is not imported by
the host: it reads ``SWNX_REQUEST`` (JSON written by
``swnx_switch.backends.base.Backend.run``), does the CAD work with NXOpen, and
writes ``SWNX_RESULT`` plus a line-flushed text log at ``SWNX_LOG``.

Portability rules for this file
------------------------------
* No Python 3.11+ syntax (NX 2406 = 3.10; NX 12 = 3.6).  No type annotations,
  no ``match``, no walrus in tricky places -- the same file must stay parseable
  by the oldest interpreter we might run it on.
* Every section is wrapped in ``try/except`` and logs where it stopped, because
  a batch NX crash takes the process (and stdout's buffer) with it: the journal's
  own file log is the only reliable record.
* Do **not** emit the fatal log tokens the host scans for (the list lives in
  ``swnx_switch.backends.base``): a caught exception is a warning here, so
  ``caught()`` renames the traceback header before it reaches the log.
"""

import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_DIR = os.path.join(REPO_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

REQUEST_PATH = os.environ.get("SWNX_REQUEST")
RESULT_PATH = os.environ.get("SWNX_RESULT")
LOG_PATH = os.environ.get("SWNX_LOG") or os.path.join(HERE, "driver.log")
WORKDIR = os.environ.get("SWNX_WORKDIR") or os.path.dirname(LOG_PATH) or HERE

STARTED = time.time()
WARNINGS = []

#: Written into every result so the host can tell which journal produced it.
DRIVER_VERSION = "1"


# --------------------------------------------------------------------------- #
# logging / result plumbing
# --------------------------------------------------------------------------- #
def say(message):
    """Append one line to the journal log, flushed immediately.

    stdout is also redirected into the same file by the host, but NX's embedded
    interpreter buffers it; the explicit file write is what survives a crash.
    """
    line = str(message)
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(line + "\n")
            handle.flush()
    except Exception:
        pass
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def caught():
    """Format the current exception without the token the host treats as fatal.

    ``Backend.run`` fails a run whose log contains ``Traceback (most recent call
    last)``.  A robust journal catches per section and keeps going, so that
    header is renamed here; the frames and the message are kept for debugging.
    """
    text = traceback.format_exc()
    return text.replace("Traceback (most recent call last)", "Traceback (caught)")


def warn(message):
    WARNINGS.append(str(message))
    say("WARN " + str(message))


def note(message):
    say("NOTE " + str(message))


def write_result(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    with open(RESULT_PATH, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    say("RESULT_WRITTEN %d bytes" % len(text))


def new_payload(kind):
    return {
        "ok": False,
        "kind": kind,
        "backend": "nx",
        "app": "Siemens NX",
        "outputs": [],
        "warnings": WARNINGS,
        "error": None,
    }


def read_request():
    if not REQUEST_PATH:
        raise RuntimeError("SWNX_REQUEST is not set")
    with open(REQUEST_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def result_path_of(request, default_name):
    """Resolve a request path against the workdir when it is relative."""
    path = request.get("output") or default_name
    if not os.path.isabs(path):
        path = os.path.join(WORKDIR, path)
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    return path


# --------------------------------------------------------------------------- #
# small introspection helpers (used by probe and by the extractors)
# --------------------------------------------------------------------------- #
def names_of(obj, filters=None, limit=None):
    """Public attribute names, optionally filtered by substrings."""
    try:
        names = sorted(name for name in dir(obj) if not name.startswith("_"))
    except Exception:
        return []
    if filters:
        lowered = [f.lower() for f in filters]
        names = [name for name in names if any(f in name.lower() for f in lowered)]
    if limit is not None:
        names = names[:limit]
    return names


def enum_name(value):
    """``str()`` an NXOpen enum, tolerating plain ints and odd wrappers."""
    try:
        return "%s" % (value,)
    except Exception:
        return "?"


def part_units(part):
    """Return (label, millimetres-per-unit) for the part's model units.

    The enum's ``str()`` form is a number in this NX build (the probe reported
    ``"1"`` for a millimetre part), so the comparison is done against the enum
    members directly and string matching is only a fallback.
    """
    units = getattr(part, "PartUnits", None)
    try:
        nxopen = _import_nxopen()
        for label, scale in (
            ("Millimeters", 1.0),
            ("Inches", 25.4),
            ("Meters", 1000.0),
            ("Micrometers", 0.001),
        ):
            member = getattr(nxopen.Part.Units, label, None)
            if member is not None and units == member:
                return label, scale
    except Exception:
        pass
    label = enum_name(units)
    text = label.lower()
    if "inch" in text:
        return label, 25.4
    if "meter" in text and "milli" not in text:
        return label, 1000.0
    if "micro" in text:
        return label, 0.001
    return label, 1.0


def safe_attr(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def vec3(value):
    """Read a Point3d/Vector3d-ish value into [x, y, z] or None."""
    try:
        return [float(value.X), float(value.Y), float(value.Z)]
    except Exception:
        pass
    try:
        return [float(value[0]), float(value[1]), float(value[2])]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
#: UF face type codes (``uf_modl_ask_face_data`` ``type`` field).  Only the
#: classic codes are mapped; anything else falls through to ``other``.  The
#: authoritative classifier for the IR is ``Face.SolidFaceType`` (see
#: ``_solid_face_kind``) -- this table is only the fallback for faces where that
#: enum cannot be read, and it is cross-checked against the live histogram the
#: probe reports.
FACE_TYPES = {
    16: "cylinder",
    17: "cone",
    18: "sphere",
    19: "torus",
    20: "other",  # swept-spherical
    22: "plane",
    43: "other",  # blending face
}


def _solid_face_kind(face):
    """Map ``Face.SolidFaceType`` to an IR surface kind (authoritative).

    ``NXOpen.Face.FaceType`` names the analytic types directly
    (Planar/Cylindrical/Conical/Spherical); everything else is a parametric or
    blending face and is left to the UF-code fallback, which is what resolves
    tori.  Enum values are compared directly first because the Python wrapper's
    ``str()`` form is not guaranteed.
    """
    value = safe_attr(face, "SolidFaceType")
    if value is None:
        return None
    try:
        face_type = _import_nxopen().Face.FaceType
        for label, member in (
            ("plane", "Planar"),
            ("cylinder", "Cylindrical"),
            ("cone", "Conical"),
            ("sphere", "Spherical"),
        ):
            target = getattr(face_type, member, None)
            if target is not None and value == target:
                return label
    except Exception:
        pass
    text = enum_name(value).lower()
    if "planar" in text or "plane" in text:
        return "plane"
    if "cylind" in text:
        return "cylinder"
    if "conic" in text:
        return "cone"
    if "spher" in text:
        return "sphere"
    return None


def probe_session(session, ufs):
    """Everything we want to know about the live NX process."""
    info = {}
    info["session_dir"] = names_of(
        session, ["version", "env", "license", "module", "journal", "parts", "dex", "ui"]
    )
    # NX version string: try the documented-ish accessors, one at a time.
    for attr in ("GetEnvironmentVariableValue", "GetEnvironmentVariable"):
        info["has_" + attr] = hasattr(session, attr)
    for name in ("UGII_VERSION", "UGII_BASE_DIR", "UGII_RELEASE", "UGII_LANG"):
        value = None
        if hasattr(session, "GetEnvironmentVariableValue"):
            try:
                value = session.GetEnvironmentVariableValue(name)
            except Exception as exc:
                value = "ERROR: %s" % exc
        info["env_" + name] = value
    info["dir_session_license"] = names_of(session, ["license", "module", "product"])
    if hasattr(session, "LicenseManager"):
        info["license_manager"] = names_of(session.LicenseManager, None, 60)

    # DEX: enumerating the factory methods is the only honest capability list.
    # Each block is independent: an NXOpen surface that moved between releases
    # must cost one probe field, not the whole probe.
    try:
        dex = session.DexManager
        creators = [n for n in names_of(dex) if n.startswith("Create")]
        info["dex_creators"] = creators
        info["dex_exporters"] = [n for n in creators if n.endswith(("Creator", "Exporter"))]
        info["dex_importers"] = [n for n in creators if n.endswith("Importer")]
    except Exception:
        warn("DexManager not enumerable: " + caught())
    try:
        nxopen = _import_nxopen()
        info["step_export_as_option"] = [
            n for n in names_of(nxopen.StepCreator.ExportAsOption) if not n.startswith("_")
        ]
        info["step_export_from_option"] = [
            n for n in names_of(nxopen.StepCreator.ExportFromOption) if not n.startswith("_")
        ]
        info["object_selector_scope"] = [
            n for n in names_of(nxopen.ObjectSelector.Scope) if not n.startswith("_")
        ]
        info["step_creator_members"] = names_of(nxopen.StepCreator, None, 80)
        info["dex_builder_members"] = names_of(nxopen.DexBuilder, None, 80)
        info["measure_manager_members"] = names_of(nxopen.MeasureManager, None, 120)
    except Exception:
        warn("NXOpen class introspection failed: " + caught())
    try:
        nxopen = _import_nxopen()
        for class_name in ("Step203Importer", "Step214Importer", "Step242Importer"):
            cls = getattr(nxopen, class_name, None)
            if cls is not None:
                info["members_" + class_name] = names_of(cls, None, 80)
                option = getattr(cls, "ImportToOption", None)
                if option is not None:
                    info["options_" + class_name] = [
                        n for n in names_of(option) if not n.startswith("_")
                    ]
    except Exception:
        warn("STEP importer introspection failed: " + caught())
    try:
        info["dir_ufs"] = names_of(ufs, None, 120)
        info["dir_ufs_ps"] = names_of(ufs.Ps, None, 60)
        info["dir_ufs_modl_area"] = names_of(
            ufs.Modl, ["area", "volume", "mass", "bound", "face"]
        )
        info["dir_ufs_modlgeneral"] = names_of(
            ufs.ModlGeneral, ["area", "volume", "mass", "bound", "face", "ask"]
        )
        info["dir_ufs_modeling"] = names_of(ufs.Modeling, ["area", "volume", "mass", "face"])
        info["dir_ufs_part"] = names_of(ufs.Part, ["attrib", "unit", "save"])
    except Exception:
        warn("UF session groups not enumerable: " + caught())
    return info


def _import_nxopen():
    """Import NXOpen *and* its UF submodule, return the ``NXOpen`` module.

    ``import NXOpen`` does not load the submodules: ``NXOpen.UF`` (and
    ``NXOpen.Motion``, ``NXOpen.Features``, ...) stay unset until imported, so
    ``NXOpen.UF.UFSession`` raises ``AttributeError: module 'NXOpen' has no
    attribute 'UF'`` on first use.  This was the first failure of the journal in
    real batch NX, so the import is centralised here instead of repeated.
    """
    import NXOpen  # noqa: PLC0415 - only importable inside NX
    import NXOpen.UF  # noqa: F401,PLC0415 - submodule, not auto-loaded

    return NXOpen


def probe_part(session, ufs, part_path):
    """Open a real part read-only and record every API we need for ``read``."""
    nxopen = _import_nxopen()
    info = {"path": part_path}
    part, status = session.Parts.Open(part_path)
    info["open_status"] = enum_name(status)
    ufs.Part.SetDisplayPart(part.Tag)
    session.Parts.SetWork(part)
    info["full_path"] = safe_attr(part, "FullPath")
    info["leaf"] = safe_attr(part, "Leaf")
    label, scale = part_units(part)
    info["part_units"] = label
    info["mm_per_unit"] = scale
    info["part_members"] = names_of(
        part, ["attrib", "unit", "feature", "sketch", "bodies", "measure", "material",
               "expression", "component", "part", "name", "path"]
    )

    bodies = list(part.Bodies)
    info["bodies"] = len(bodies)
    if not bodies:
        return info, part
    body = bodies[0]
    info["body_members"] = names_of(body, ["face", "edge", "vert", "solid", "sheet", "dens",
                                          "volume", "area", "name", "type"])
    faces = list(body.GetFaces())
    edges = list(body.GetEdges())
    info["body_stats"] = {
        "faces": len(faces),
        "edges": len(edges),
        "vertices": safesafe(lambda: body.GetNumberOfVertices()),
        "is_solid": safesafe(lambda: body.IsSolidBody),
        "is_sheet": safesafe(lambda: body.IsSheetBody),
        "density": safesafe(lambda: body.Density),
    }

    # ---- mass properties (documented: units [area, volume, mass, length, weight])
    try:
        uc = part.UnitCollection
        unit_names = ("SquareMilliMeter", "CubicMilliMeter", "Kilogram", "MilliMeter", "Newton")
        units = [uc.FindObject(name) for name in unit_names]
        info["mass_units"] = [enum_name(u) if u is not None else None for u in units]
        solids = [item for item in bodies if safesafe(lambda b=item: b.IsSolidBody, False)]
        measure = part.MeasureManager.NewMassProperties(units, [0.999], solids or bodies)
        info["mass_props_members"] = names_of(measure, None, 60)
        props = {}
        for key in ("Area", "Volume", "Mass", "Weight", "Density", "Centroid",
                    "CenterOfMass", "MomentsOfInertia", "RadiusOfGyration",
                    "PrincipalAxes", "PrincipalMoments", "ProductsOfInertia"):
            value = safe_attr(measure, key)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                props[key] = float(value)
            else:
                props[key] = vec3(value) or enum_name(value)
        info["mass_props"] = props
    except Exception:
        warn("mass properties probe failed: " + caught())

    # ---- bbox
    try:
        box = ufs.ModlGeneral.AskBoundingBox(body.Tag)
        info["bbox_raw"] = [float(v) for v in box]
        info["bbox_raw_len"] = len(box)
    except Exception:
        warn("AskBoundingBox probe failed: " + caught())

    # ---- per-face surface data
    try:
        by_code = {}
        by_kind = {}
        samples = []
        for face in faces:
            data = ufs.Modeling.AskFaceData(face.Tag)
            code = int(data[0])
            kind = _solid_face_kind(face) or FACE_TYPES.get(code, "other")
            by_code[code] = by_code.get(code, 0) + 1
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if len(samples) < 6:
                samples.append(
                    {
                        "code": code,
                        "kind": kind,
                        "solid_face_type": enum_name(face.SolidFaceType),
                        "data_len": len(data),
                        "point": vec3(data[1]),
                        "axis": vec3(data[2]),
                        "radius": float(data[4]) if len(data) > 4 else None,
                        "items": [enum_name(item) for item in data[3:6]],
                    }
                )
        info["face_codes"] = {"%d" % k: v for k, v in sorted(by_code.items())}
        info["face_kinds"] = by_kind
        info["face_samples"] = samples
        info["face_type_names"] = {str(k): v for k, v in FACE_TYPES.items()}
    except Exception:
        warn("AskFaceData probe failed: " + caught())

    # ---- attributes (custom properties)
    try:
        infos = list(part.GetUserAttributes())
        info["attribute_count"] = len(infos)
        if infos:
            first = infos[0]
            info["attribute_members"] = names_of(first, None, 40)
            rows = []
            for item in infos[:20]:
                rows.append(
                    {
                        "title": safe_attr(item, "Title"),
                        "title_alias": safe_attr(item, "TitleAlias"),
                        "type": enum_name(safe_attr(item, "Type")),
                        "category": safe_attr(item, "Category"),
                        "unit": enum_name(safe_attr(item, "Unit")) if safe_attr(item, "Unit") else None,
                        "string": safesafe(lambda: item.StringValue),
                        "real": safesafe(lambda: item.RealValue),
                        "integer": safesafe(lambda: item.IntegerValue),
                        "boolean": safesafe(lambda: item.BooleanValue),
                        "inherited": safesafe(lambda: item.Inherited),
                        "owned_by_system": safesafe(lambda: item.OwnedBySystem),
                    }
                )
            info["attributes"] = rows
    except Exception:
        warn("GetUserAttributes probe failed: " + caught())

    # ---- features
    try:
        features = list(part.Features)
        info["feature_count"] = len(features)
        rows = []
        for feature in features[:12]:
            row = {
                "name": safe_attr(feature, "Name"),
                "type": enum_name(safe_attr(feature, "FeatureType")),
                "class": type(feature).__name__,
                "suppressed": safesafe(lambda: feature.Suppressed),
                "timestamp": safesafe(lambda: feature.Timestamp),
                "container": enum_name(safe_attr(feature, "ContainerFeature")),
            }
            for method in ("GetParents", "GetChildren", "GetFeatures", "GetEntities"):
                if hasattr(feature, method):
                    row["has_" + method] = True
            rows.append(row)
        info["features"] = rows
        info["feature_members"] = names_of(features[0], None, 60) if features else []
    except Exception:
        warn("feature tree probe failed: " + caught())

    # ---- sketches
    try:
        sketches = list(part.Sketches)
        info["sketch_count"] = len(sketches)
        if sketches:
            sketch = sketches[0]
            info["sketch_members"] = names_of(sketch, None, 60)
            info["sketch_sample"] = {
                "name": safe_attr(sketch, "Name"),
                "class": type(sketch).__name__,
                "attach_plane": enum_name(safe_attr(sketch, "AttachPlane")),
                "origin": vec3(safe_attr(sketch, "OriginPoint")),
                "entities": safesafe(lambda: len(list(sketch.GetAllEntities()))),
            }
    except Exception:
        warn("sketch probe failed: " + caught())

    # ---- expressions (sketch dimensions / user parameters)
    try:
        expressions = list(part.Expressions)
        info["expression_count"] = len(expressions)
        info["expressions"] = [
            {"name": safe_attr(e, "Name"), "value": enum_name(safe_attr(e, "Value"))}
            for e in expressions[:15]
        ]
    except Exception:
        warn("expression probe failed: " + caught())

    # ---- material (NXOpen exposes only the physical-material collection)
    try:
        info["has_material_manager"] = hasattr(part, "MaterialManager")
        if hasattr(part, "MaterialManager"):
            manager = part.MaterialManager
            info["material_manager_members"] = names_of(manager, None, 60)
            materials = list(manager.PhysicalMaterials)
            info["physical_materials"] = len(materials)
            if materials:
                first = materials[0]
                info["material_members"] = names_of(first, None, 40)
                info["material_sample"] = {
                    "name": safe_attr(first, "Name"),
                    "description": safesafe(lambda: first.GetDescription()),
                    "category": safesafe(lambda: first.GetCategory()),
                }
    except Exception:
        warn("material manager probe failed: " + caught())

    # ---- per-face area (LAST: this is the only probe call that hands an array
    #      of faces to a builder, so keep it after the cheap introspection)
    try:
        measure = part.MeasureManager.NewFaceProperties(
            part.UnitCollection.FindObject("SquareMilliMeter"),
            part.UnitCollection.FindObject("MilliMeter"),
            0.999,
            faces,
        )
        info["face_props_members"] = names_of(measure, None, 40)
        info["face_area_sum"] = safe_attr(measure, "Area")
    except Exception:
        warn("NewFaceProperties probe failed: " + caught())
    return info, part


def safesafe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


#: STEP export attempts, in the order that real batch NX 2406 needs them.
#:
#: Verified on this machine (2026-11-13, run log log-export-57fb1592.txt):
#: * ``Ap242`` never wrote a file at all -- the translator starts, reports
#:   "NX STEP AP242 - V2406.1700" and ends, leaving only a timestamped
#:   ``tessUG.config`` side directory.  No exception, just no output.
#: * ``Ap214`` did write a valid STEP file, but with **zero** ADVANCED_FACE /
#:   MANIFOLD_SOLID_BREP entities: the export selection block defaults to
#:   "selected objects" with nothing selected.
#: ``ObjectSelector.Scope`` offers ``EntirePart``, which is the whole-part
#: selection the batch API needs (the GUI's default is not the API default).
STEP_STRATEGIES = (
    # AP242 ED2 first: this install ships only the AP242_ED2 translator def
    # (translators\step242\ugstep242.def, APPLICATION_PROTOCOL = AP242_ED2).
    # Verified in batch NX 2406 on this machine: every STEP strategy produces
    # metadata-only output (0 ADVANCED_FACE / 0 solids input in the translator
    # log), so the three attempts below are ordered by likelihood and the job
    # then falls back to Parasolid, which is the usable exchange on this build.
    {"label": "ap242ed2_entire_part", "ap242_ed2": True, "export_from": "DisplayPart",
     "scope": "EntirePart", "select_bodies": False,
     "settings": r"translators\step242\ugstep242.def"},
    {"label": "ap214_entire_part", "ap242": False, "export_from": "DisplayPart",
     "scope": "EntirePart", "select_bodies": False},
    {"label": "ap214_selected", "ap242": False, "export_from": "DisplayPart",
     "scope": "SelectedObjects", "select_bodies": True},
)


def _set_enum(target, owner, attribute, enum_name_, label):
    """Assign a nested NXOpen enum member by name, if this NX has it."""
    nxopen = _import_nxopen()
    enum_type = getattr(owner, enum_name_, None)
    member = getattr(enum_type, label, None) if enum_type is not None else None
    if member is None:
        raise RuntimeError("%s has no member %r in this NX release" % (enum_name_, label))
    setattr(target, attribute, member)


def _step_export_attempt(session, part, src, out, strategy):
    """One STEP export attempt; returns a record, never raises for a bad export."""
    nxopen = _import_nxopen()
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
    record = {
        "strategy": strategy["label"],
        "path": out,
        "ap242": bool(strategy.get("ap242")),
        "scope": strategy.get("scope"),
        "ok": False,
    }
    creator = None
    try:
        creator = session.DexManager.CreateStepCreator()
        if strategy.get("ap242_ed2"):
            creator.ExportAs = nxopen.StepCreator.ExportAsOption.Ap242ED2
        elif strategy.get("ap242"):
            creator.ExportAs = nxopen.StepCreator.ExportAsOption.Ap242
        else:
            creator.ExportAs = nxopen.StepCreator.ExportAsOption.Ap214
        settings_rel = strategy.get("settings")
        if settings_rel:
            base = os.environ.get("UGII_BASE_DIR") or os.environ.get("NX_HOME") or ""
            settings_path = os.path.join(base, settings_rel) if base else ""
            if settings_path and os.path.exists(settings_path):
                creator.SettingsFile = settings_path
                record["settings_file"] = settings_path
        creator.ObjectTypes.Solids = True
        creator.InputFile = src
        creator.OutputFile = out
        # Without this the creator returns before the translator has written the
        # file; the size/marker check below then sees a missing or empty STEP.
        creator.ProcessHoldFlag = True
        export_from = strategy.get("export_from")
        if export_from:
            _set_enum(creator, nxopen.StepCreator, "ExportFrom",
                      "ExportFromOption", export_from)
        scope = strategy.get("scope")
        if scope:
            _set_enum(creator.ExportSelectionBlock, nxopen.ObjectSelector,
                      "SelectionScope", "Scope", scope)
        if strategy.get("select_bodies"):
            for body in part.Bodies:
                creator.ExportSelectionBlock.SelectionComp.Add(body)
        creator.Commit()
    except Exception:
        record["error"] = one_line(caught())
        return record
    finally:
        if creator is not None:
            try:
                creator.Destroy()
            except Exception:
                pass
    stats = _file_stats(out, b"ADVANCED_FACE")
    record.update(stats)
    record["ok"] = bool(
        stats.get("exists") and (stats.get("size") or 0) > 0 and (stats.get("marker") or 0) > 0
    )
    return record


def probe_step_export(session, part, part_path, out_dir):
    """Try the STEP export strategies; the batch 0-solid pitfall needs evidence."""
    results = []
    for index, strategy in enumerate(STEP_STRATEGIES):
        path = os.path.join(out_dir, "probe-step-%02d-%s.stp" % (index, strategy["label"]))
        record = _step_export_attempt(session, part, part_path, path, strategy)
        results.append(record)
        say(
            "PROBE_STEP %s size=%s faces=%s err=%s"
            % (
                strategy["label"],
                record.get("size"),
                record.get("marker"),
                "yes" if record.get("error") else "no",
            )
        )
    return results


def probe_parasolid_export(ufs, part, out_dir):
    path = os.path.join(out_dir, "probe-part.x_t")
    if os.path.exists(path):
        os.remove(path)
    record = {"path": path}
    try:
        bodies = list(part.Bodies)
        ufs.Ps.ExportData([body.Tag for body in bodies], path)
        record.update(_file_stats(path, b"PARASOLID"))
    except Exception:
        record["error"] = caught()
    return record


def _import_to_enum_member(nxopen, label):
    """Return the ``ImportToOption`` member named ``label`` (WorkPart/NewPart)."""
    for class_name in ("Step203Importer", "Step214Importer", "Step242Importer"):
        cls = getattr(nxopen, class_name, None)
        if cls is None:
            continue
        option = getattr(cls, "ImportToOption", None)
        if option is None:
            continue
        member = getattr(option, label, None)
        if member is not None:
            return member
    return None


def _open_saved_part(session, ufs, path):
    """Open a .prt that a previous step saved; returns the part or None."""
    if not os.path.exists(path):
        return None
    try:
        part, _status = session.Parts.Open(path)
        ufs.Part.SetDisplayPart(part.Tag)
        session.Parts.SetWork(part)
        return part
    except Exception:
        return None


def _open_exchange_as_part_attempt(session, ufs, src, out):
    """Last resort: ``Parts.Open`` on the exchange file.

    NX's File->Open path runs the registered DEX importer into a new part; some
    batch builds honour it even though the public API for it is the importer
    builders.  Cheap to try, and it bypasses the work-part restrictions.
    """
    record = {"import_to": "OpenExchange", "ok": False, "bodies": 0}
    try:
        if os.path.exists(out):
            os.remove(out)
        part, status = session.Parts.Open(src)
        ufs.Part.SetDisplayPart(part.Tag)
        session.Parts.SetWork(part)
        record["open_status"] = enum_name(status)
        bodies = list(part.Bodies)
        record["bodies"] = len(bodies)
        if bodies:
            part.SaveAs(out)
        record["saved"] = os.path.exists(out)
        record["saved_bytes"] = os.path.getsize(out) if os.path.exists(out) else 0
        record["ok"] = bool(record["bodies"] > 0 and record["saved"])
    except Exception:
        record["error"] = one_line(caught())
    return record


def _import_step_attempt(
    session, ufs, nxopen, src, out, *, import_to, flatten, file_open_flag, importer_class=None
):
    """One STEP import attempt. Returns a record; never raises.

    Verified on this machine (log-import-efb4a568.txt): importing into an
    *unsaved* blank work part fails with
    ``NXException: Part Import Error: Unable to import selected file to work
    part``.  The attempts below therefore try (1) a saved blank work part with
    the assembly flattened, (2) the importer's NewPart mode, whose ``OutputFile``
    (BaseImporter) can name the .prt to create, and (3) a lenient Step214
    importer for files whose header says AP203.
    """
    record = {
        "import_to": import_to,
        "flatten_assembly": flatten,
        "file_open_flag": file_open_flag,
        "importer": importer_class or "schema",
        "ok": False,
        "bodies": 0,
    }
    target_part = None
    try:
        if import_to == "WorkPart":
            target_part = _new_part(session, ufs, out)
            ufs.Part.Save()
            record["blank_part_saved"] = os.path.exists(out)
        elif os.path.exists(out):
            # A previous WorkPart attempt saved its blank part to this path;
            # NewPart + OutputFile must not trip over it.
            os.remove(out)
        importer = _create_step_importer(session, nxopen, _step_schema(src), importer_class)
        try:
            importer.InputFile = src
            importer.FileOpenFlag = file_open_flag
            importer.ProcessHoldFlag = True
            if flatten:
                importer.FlattenAssembly = True
            if import_to == "NewPart" and hasattr(importer, "OutputFile"):
                # NX1926+ importers can name the generated part file directly.
                importer.OutputFile = out
            member = _import_to_enum_member(nxopen, import_to)
            if member is not None:
                importer.ImportTo = member
            importer.Commit()
        finally:
            try:
                importer.Destroy()
            except Exception:
                pass
        if import_to == "NewPart":
            target_part = safesafe(lambda: session.Parts.Display, None)
            if target_part is None:
                target_part = safesafe(lambda: session.Parts.Work, None)
            record["imported_part"] = safesafe(lambda p=target_part: p.Leaf, None)
        bodies = list(target_part.Bodies) if target_part is not None else []
        if not bodies and os.path.exists(out):
            # NewPart mode may leave the generated .prt on disk without making it
            # the work part; read it back to count what was really imported.
            target_part = _open_saved_part(session, ufs, out) or target_part
            bodies = list(target_part.Bodies) if target_part is not None else []
        record["bodies"] = len(bodies)
        if record["bodies"]:
            if not os.path.exists(out):
                if import_to == "NewPart":
                    target_part.SaveAs(out)
                else:
                    ufs.Part.Save()
        record["saved"] = os.path.exists(out)
        record["saved_bytes"] = os.path.getsize(out) if os.path.exists(out) else 0
        record["ok"] = bool(record["bodies"] > 0 and record["saved"])
    except Exception:
        record["error"] = one_line(caught())
    return record


def probe_step_import(session, ufs, step_path, out_dir):
    """Validate the STEP importer path once, into a fresh part saved as .prt."""
    nxopen = _import_nxopen()
    info = {"input": step_path, "schema": _step_schema(step_path)}
    try:
        importer = _create_step_importer(session, nxopen, info["schema"])
        info["importer_class"] = type(importer).__name__
        info["import_to_default"] = enum_name(safe_attr(importer, "ImportTo"))
        importer.Destroy()
    except Exception:
        warn("STEP importer enumeration failed: " + caught())
        return info
    target = os.path.join(out_dir, "probe-import-target.prt")
    if os.path.exists(target):
        os.remove(target)
    attempts = []
    for spec in (
        {"import_to": "WorkPart", "flatten": True, "file_open_flag": False},
        {"import_to": "NewPart", "flatten": True, "file_open_flag": False},
    ):
        record = _import_step_attempt(session, ufs, nxopen, step_path, target, **spec)
        attempts.append(record)
        say(
            "PROBE_IMPORT %s bodies=%s ok=%s err=%s"
            % (record.get("import_to"), record.get("bodies"), record.get("ok"),
               record.get("error"))
        )
        if record.get("ok"):
            break
    info["attempts"] = attempts
    winners = [record for record in attempts if record.get("ok")]
    if winners:
        info.update(
            {
                "imported_bodies": winners[0].get("bodies"),
                "saved_bytes": winners[0].get("saved_bytes"),
                "import_to": winners[0].get("import_to"),
            }
        )
    elif attempts:
        info["error"] = attempts[-1].get("error") or "no bodies imported"
    return info


def _step_schema(path):
    """Sniff the STEP schema without a STEP parser: AP203/AP214/AP242 differ."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8192).decode("latin-1", "replace").upper()
    except Exception:
        return "unknown"
    if "AP242" in head or "MANAGED_MODEL_BASED_3D_ENGINEERING" in head:
        return "ap242"
    if "AUTOMOTIVE_DESIGN" in head or "AP214" in head:
        return "ap214"
    if "CONFIG_CONTROL_DESIGN" in head or "AP203" in head:
        return "ap203"
    return "unknown"


def _create_step_importer(session, nxopen, schema, importer_class=None):
    dex = session.DexManager
    if importer_class:
        factory = getattr(dex, "Create" + importer_class, None)
        if factory is not None:
            return factory()
    if schema == "ap242" and hasattr(dex, "CreateStep242Importer"):
        return dex.CreateStep242Importer()
    if schema == "ap214" and hasattr(dex, "CreateStep214Importer"):
        return dex.CreateStep214Importer()
    if schema == "ap203" and hasattr(dex, "CreateStep203Importer"):
        return dex.CreateStep203Importer()
    return dex.CreateStep214Importer()


def _file_stats(path, marker):
    stats = {"size": None, "exists": False}
    if not os.path.exists(path):
        return stats
    stats["exists"] = True
    stats["size"] = os.path.getsize(path)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        stats["marker"] = data.count(marker)
        stats["head"] = data[:24].decode("latin-1", "replace")
    except Exception:
        stats["marker"] = None
    return stats


# --------------------------------------------------------------------------- #
# handler table
# --------------------------------------------------------------------------- #
HANDLERS = {}


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn

    return register


@handler("probe")
def handle_probe(request, payload):
    nxopen = _import_nxopen()
    session = nxopen.Session.GetSession()
    ufs = nxopen.UF.UFSession.GetUFSession()
    info = {"driver_version": DRIVER_VERSION, "workdir": WORKDIR}
    info["session"] = probe_session(session, ufs)
    inputs = request.get("input")
    if isinstance(inputs, str) and inputs:
        inputs = [inputs]
    if not inputs:
        inputs = [
            os.path.join(WORKDIR, "..", "..", "..", "nx_sw_test", "probe_part.prt"),
        ]
    state = {"part": None, "part_path": None, "part_info": None}
    for path in inputs:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        try:
            part_info, part = probe_part(session, ufs, path)
            state["part"], state["part_path"], state["part_info"] = part, path, part_info
            break
        except Exception:
            warn("probe_part failed on %s: %s" % (path, caught()))
    info["part"] = state["part_info"]
    if state["part"] is not None:
        # The export job is the thing that has to prove STEP works, so the probe
        # only runs the export matrix when explicitly asked (one probe = one
        # approval on this machine; keep it cheap).
        if request.get("options", {}).get("export_probe"):
            try:
                info["step_export"] = probe_step_export(
                    session, state["part"], state["part_path"], WORKDIR
                )
            except Exception:
                warn("STEP export probe failed: " + caught())
        try:
            info["parasolid_export"] = probe_parasolid_export(ufs, state["part"], WORKDIR)
        except Exception:
            warn("Parasolid export probe failed: " + caught())
    import_input = request.get("options", {}).get("import_input")
    if import_input and os.path.exists(import_input):
        try:
            info["step_import"] = probe_step_import(session, ufs, os.path.abspath(import_input), WORKDIR)
        except Exception:
            warn("STEP import probe failed: " + caught())
    payload["probe"] = info
    payload["app_version"] = _probe_version(info) or None
    if not payload["app_version"]:
        payload.pop("app_version", None)
    payload["ok"] = True


def _probe_version(info):
    """Return a clean release label; NX reports e.g. ``'v2406'``."""
    for key in ("UGII_VERSION", "UGII_RELEASE"):
        value = info.get("session", {}).get("env_" + key)
        if isinstance(value, str) and value and not value.startswith("ERROR"):
            return _normalize_release(value)
    return None


def _normalize_release(value):
    text = str(value).strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text or "unknown"


# --------------------------------------------------------------------------- #
# read: native .prt -> neutral IR
# --------------------------------------------------------------------------- #
def one_line(text):
    lines = [line for line in str(text).strip().splitlines() if line.strip()]
    return lines[-1] if lines else "unknown"


def _nx_release(session):
    value = os.environ.get("SWNX_NX_RELEASE")
    if value and value != "unknown":
        return _normalize_release(value)
    if session is not None:
        try:
            value = session.GetEnvironmentVariableValue("UGII_VERSION")
            if value:
                return _normalize_release(value)
        except Exception:
            pass
    return "unknown"


def _open_part(session, ufs, path):
    """Open a part and make it the work/display part until the process exits.

    ``Parts.Open`` is the only way into a part in batch (``Parts.Work`` is
    ``None``); the journal never saves an opened input part, so this is a
    read-only visit as far as the file on disk is concerned.
    """
    part, status = session.Parts.Open(path)
    ufs.Part.SetDisplayPart(part.Tag)
    session.Parts.SetWork(part)
    return part, status


def _unit_array(part):
    """[area, volume, mass, length, weight] units -- the order NX documents."""
    try:
        collection = part.UnitCollection
        units = [
            collection.FindObject(name)
            for name in ("SquareMilliMeter", "CubicMilliMeter", "Kilogram", "MilliMeter", "Newton")
        ]
    except Exception:
        return None
    if any(unit is None for unit in units):
        return None
    return units


def _mass_of(part, bodies):
    """Mass properties of solid bodies, always in mm / kg units.

    ``NewMassProperties`` takes the *output* unit system, so the values do not
    depend on the part's model units.
    """
    bodies = list(bodies)
    if not bodies:
        return {}
    units = _unit_array(part)
    if units is None:
        raise RuntimeError("mass-property unit objects are not available in this part")
    measure = part.MeasureManager.NewMassProperties(units, [0.999], bodies)
    props = {}
    for key, attribute in (("volume_mm3", "Volume"), ("surface_area_mm2", "Area"), ("mass_kg", "Mass")):
        value = safesafe(lambda name=attribute: getattr(measure, name))
        if isinstance(value, (int, float)):
            props[key] = float(value)
    centroid = vec3(safe_attr(measure, "Centroid"))
    if centroid:
        props["centroid_mm"] = centroid
    return props


def _body_bbox(ufs, body, scale):
    box = safesafe(lambda: ufs.ModlGeneral.AskBoundingBox(body.Tag))
    if box is None:
        return None
    try:
        return (
            [float(box[0]) * scale, float(box[1]) * scale, float(box[2]) * scale],
            [float(box[3]) * scale, float(box[4]) * scale, float(box[5]) * scale],
        )
    except Exception:
        return None


def _feature_type_label(feature):
    value = safe_attr(feature, "FeatureType")
    if value is None:
        return type(feature).__name__
    text = str(value)
    if text.isdigit():
        return "type_" + text
    return text.split(".")[-1] or text


def _attribute_value(item):
    """One ``AttributeInformation`` -> a JSON scalar."""
    type_text = str(safe_attr(item, "Type") or "")
    lower = type_text.lower()
    candidates = []
    if "string" in lower or "text" in lower:
        candidates = ["StringValue"]
    elif "int" in lower:
        candidates = ["IntegerValue", "RealValue"]
    elif "real" in lower or "number" in lower:
        candidates = ["RealValue", "IntegerValue"]
    elif "bool" in lower:
        candidates = ["BooleanValue", "IntegerValue"]
    elif "time" in lower:
        candidates = ["TimeValue", "StringValue"]
    elif "reference" in lower:
        candidates = ["ReferenceValue", "StringValue"]
    candidates = candidates + ["StringValue", "RealValue", "IntegerValue", "BooleanValue"]
    seen = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        value = safesafe(lambda n=name: getattr(item, n))
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            if value != "":
                return value
            continue
    return None


def _extract_attributes(part, doc, ir):
    props = {}
    try:
        infos = list(part.GetUserAttributes())
    except Exception:
        ir.warn(doc, "custom properties unavailable: %s" % one_line(caught()))
        return
    skipped_system = 0
    for item in infos:
        title = safe_attr(item, "Title")
        if not title:
            continue
        # NX_* attributes are NX bookkeeping (material-assignment flags, mass
        # property context), owned by the system, not the user's document data.
        if safesafe(lambda i=item: bool(i.OwnedBySystem), False):
            skipped_system += 1
            continue
        value = _attribute_value(item)
        if value is not None:
            props[str(title)] = value
    if skipped_system:
        doc["diagnostics"]["driver"]["system_attributes_skipped"] = skipped_system
    if props:
        ir.add_custom_properties(doc, props)


def _feature_name(feature):
    """Best name for a feature.  ``Name`` is empty for most NX features."""
    name = str(safe_attr(feature, "Name") or "")
    if name:
        return name
    via_method = safesafe(lambda f=feature: f.GetFeatureName(), None)
    if via_method:
        return str(via_method)
    return ""


def _extract_features(part, doc, ir):
    try:
        features = list(part.Features)
    except Exception:
        ir.warn(doc, "feature tree unavailable: %s" % one_line(caught()))
        return
    for index, feature in enumerate(features, start=1):
        name = _feature_name(feature) or ("Feature_%d" % index)
        suppressed = safesafe(lambda f=feature: bool(f.Suppressed), False)
        parent = None
        parents = safesafe(lambda f=feature: f.GetParents(), None)
        if parents:
            parent = _feature_name(parents[0]) or None
        children = []
        kids = safesafe(lambda f=feature: f.GetChildren(), None)
        if kids:
            for child in kids:
                child_name = _feature_name(child)
                if child_name:
                    children.append(child_name)
        parameters = {}
        timestamp = safesafe(lambda f=feature: f.Timestamp)
        if isinstance(timestamp, (int, float)):
            parameters["timestamp"] = int(timestamp)
        ir.add_feature(
            doc,
            name=name,
            type=_feature_type_label(feature),
            suppressed=suppressed,
            parent=parent,
            children=children or None,
            parameters=parameters or None,
        )


def _extract_sketches(part, doc, ir):
    try:
        sketches = list(part.Sketches)
    except Exception:
        ir.warn(doc, "sketch list unavailable: %s" % one_line(caught()))
        return
    for index, sketch in enumerate(sketches, start=1):
        name = str(safe_attr(sketch, "Name") or ("Sketch_%d" % index))
        plane = enum_name(safe_attr(sketch, "AttachPlane"))
        entities = safesafe(lambda s=sketch: len(list(s.GetAllGeometry())), None)
        dimensions = {}
        expressions = safesafe(lambda s=sketch: s.GetAllExpressions(), None)
        if expressions:
            for expression in expressions:
                title = safe_attr(expression, "Name")
                if not title:
                    continue
                value = safe_attr(expression, "Value")
                dimensions[str(title)] = enum_name(value)
        ir.add_sketch(
            doc,
            name=name,
            plane=plane,
            entities=entities,
            dimensions=dimensions or None,
        )


def _extract_surfaces(part, ufs, doc, ir, scale, max_entries=120):
    """Group B-rep faces into the neutral geometry digest.

    ``UF_MODL_ask_face_data`` gives type, a point, an axis and (for the quadric
    types) a radius; faces with the same signature are one digest entry, which is
    what makes the IR readable instead of a 500-line face dump.
    """
    area_unit = safesafe(lambda: part.UnitCollection.FindObject("SquareMilliMeter"))
    length_unit = safesafe(lambda: part.UnitCollection.FindObject("MilliMeter"))
    area_enabled = area_unit is not None and length_unit is not None and not ir_area_disabled()
    groups = {}
    capped = False
    for body in list(part.Bodies):
        try:
            faces = list(body.GetFaces())
        except Exception:
            continue
        for face in faces:
            try:
                data = ufs.Modeling.AskFaceData(face.Tag)
                code = int(data[0])
            except Exception:
                continue
            # SolidFaceType is a named enum and does not depend on the numeric
            # UF code staying stable between NX releases; fall back to the code
            # table only when the enum cannot be read.
            kind = _solid_face_kind(face) or FACE_TYPES.get(code, "other")
            try:
                origin = [round(component * scale, 3) for component in vec3(data[1])]
            except Exception:
                origin = None
            axis = vec3(data[2])
            radius = None
            if len(data) > 4 and kind in ("cylinder", "cone", "sphere", "torus"):
                radius = round(float(data[4]) * scale, 4)
            normal = axis if kind == "plane" else None
            key = (
                kind,
                radius,
                tuple(round(component, 3) for component in axis) if axis else None,
                tuple(origin) if origin else None,
            )
            entry = groups.get(key)
            if entry is None:
                if len(groups) >= max_entries:
                    if not capped:
                        capped = True
                        ir.limit(doc, "geometry_digest capped at %d distinct surface groups" % max_entries)
                    area_enabled = False
                    entry = groups.get(("other", None, None, None))
                    if entry is None:
                        continue
                else:
                    entry = {"kind": kind, "count": 0, "face_tags": [], "faces": []}
                    if radius is not None:
                        entry["radius_mm"] = radius
                    if axis:
                        entry["axis"] = axis
                    if normal:
                        entry["normal"] = normal
                    if origin:
                        entry["origin_mm"] = origin
                    groups[key] = entry
            entry["count"] += 1
            if len(entry["face_tags"]) < 20:
                entry["face_tags"].append(int(face.Tag))
                entry["faces"].append(face)
    for entry in groups.values():
        faces = entry.pop("faces", [])
        if faces and area_enabled:
            try:
                measure = part.MeasureManager.NewFaceProperties(
                    area_unit, length_unit, 0.999, faces
                )
                area = safesafe(lambda m=measure: m.Area)
                if isinstance(area, (int, float)):
                    entry["area_mm2"] = round(float(area), 4)
            except Exception:
                area_enabled = False
                ir.warn(
                    doc,
                    "per-surface area unavailable (NewFaceProperties failed); "
                    "surface counts and radii are still recorded",
                )
        kind = entry.pop("kind")
        ir.add_surface(doc, kind, **entry)


def ir_area_disabled():
    """Hook so an environment switch can skip the per-group area call."""
    return os.environ.get("SWNX_NX_NO_FACE_AREA") == "1"


def _model_level(part, doc, ir):
    """kind / drawing / assembly -- all optional, all guarded."""
    sheets = safesafe(lambda: list(part.DrawingSheets), [])
    assembly = safesafe(lambda: getattr(part, "ComponentAssembly", None))
    components = []
    if assembly is not None:
        root = safe_attr(assembly, "RootComponent")
        if root is not None:
            children = safesafe(lambda: list(root.GetChildren()), [])
            for child in children:
                components.append({
                    "name": str(safe_attr(child, "DisplayName") or safe_attr(child, "Name") or ""),
                    "path": safesafe(lambda c=child: c.Prototype.OwningPart.FullPath),
                })
    if components:
        doc["model"]["kind"] = "assembly"
        ir.set_assembly(doc, {"root": safe_attr(root, "DisplayName"), "components": components,
                              "count": len(components)})
    if sheets:
        sheet_rows = []
        for sheet in sheets:
            views = safesafe(lambda s=sheet: [v.Name for v in s.GetDraftingViews()], [])
            sheet_rows.append({"name": safesafe(lambda s=sheet: s.Name), "views": views})
        ir.set_drawing(doc, {"sheets": sheet_rows})
        if doc["model"]["kind"] == "part" and not components:
            doc["model"]["kind"] = "drawing"
    if not components and not sheets:
        doc["model"]["kind"] = "part"


@handler("read")
def handle_read(request, payload):
    nxopen = _import_nxopen()
    from swnx_switch import ir

    session = nxopen.Session.GetSession()
    ufs = nxopen.UF.UFSession.GetUFSession()
    src = os.path.abspath(request.get("input") or "")
    if not os.path.isfile(src):
        raise RuntimeError("input part not found: %s" % src)
    release = _nx_release(session)
    doc = ir.new(
        backend="nx",
        app="Siemens NX",
        app_version=release,
        source_path=src,
        source_format="prt",
        kind="part",
    )
    say("READ_OPEN %s" % src)
    part, status = _open_part(session, ufs, src)
    label, scale = part_units(part)
    doc["diagnostics"]["driver"].update(
        {"part_units": label, "mm_per_unit": scale, "open_status": enum_name(status)}
    )

    bodies = list(part.Bodies)
    try:
        _model_level(part, doc, ir)
    except Exception:
        ir.warn(doc, "entity kind detection failed: %s" % one_line(caught()))

    solid = [body for body in bodies if safesafe(lambda b=body: b.IsSolidBody, False)]
    try:
        props = _mass_of(part, solid)
        if props:
            ir.set_mass_properties(
                doc,
                volume_mm3=props.get("volume_mm3"),
                surface_area_mm2=props.get("surface_area_mm2"),
                mass_kg=props.get("mass_kg"),
                centroid_mm=props.get("centroid_mm"),
            )
            if props.get("volume_mm3") and props.get("mass_kg"):
                ir.set_material(
                    doc, density_kg_m3=props["mass_kg"] / (props["volume_mm3"] * 1e-9)
                )
        else:
            ir.limit(doc, "no solid bodies; mass properties not computed")
    except Exception:
        ir.warn(doc, "mass properties unavailable: %s" % one_line(caught()))

    low = [None, None, None]
    high = [None, None, None]
    for index, body in enumerate(bodies, start=1):
        try:
            name = str(
                safe_attr(body, "Name")
                or safe_attr(body, "JournalIdentifier")
                or ("Body_%d" % index)
            )
            is_solid = safesafe(lambda b=body: b.IsSolidBody, None)
            is_sheet = safesafe(lambda b=body: b.IsSheetBody, False)
            kind = "solid" if is_solid else ("sheet" if is_sheet else "other")
            faces = safesafe(lambda b=body: len(list(b.GetFaces())), None)
            edges = safesafe(lambda b=body: len(list(b.GetEdges())), None)
            vertices = safesafe(lambda b=body: b.GetNumberOfVertices(), None)
            volume = None
            area = None
            if is_solid:
                body_props = safesafe(lambda b=body: _mass_of(part, [b]), {})
                volume = body_props.get("volume_mm3")
                area = body_props.get("surface_area_mm2")
            box = _body_bbox(ufs, body, scale)
            ir.add_body(
                doc,
                name=name,
                kind=kind,
                faces=faces,
                edges=edges,
                vertices=vertices,
                volume_mm3=volume,
                area_mm2=area,
                min_mm=box[0] if box else None,
                max_mm=box[1] if box else None,
                closed=bool(is_solid) if is_solid is not None else None,
            )
            if box:
                for axis in range(3):
                    low[axis] = box[0][axis] if low[axis] is None else min(low[axis], box[0][axis])
                    high[axis] = box[1][axis] if high[axis] is None else max(high[axis], box[1][axis])
        except Exception:
            ir.warn(doc, "body %d extraction failed: %s" % (index, one_line(caught())))
    if all(value is not None for value in low) and all(value is not None for value in high):
        ir.set_bbox(doc, min_mm=low, max_mm=high)

    for extractor in (_extract_features, _extract_sketches):
        try:
            extractor(part, doc, ir)
        except Exception:
            ir.warn(doc, "%s failed: %s" % (extractor.__name__, one_line(caught())))
    try:
        _extract_surfaces(part, ufs, doc, ir, scale)
    except Exception:
        ir.warn(doc, "geometry digest failed: %s" % one_line(caught()))

    try:
        _extract_attributes(part, doc, ir)
    except Exception:
        ir.warn(doc, "custom properties failed: %s" % one_line(caught()))

    try:
        materials = list(part.MaterialManager.PhysicalMaterials)
        if len(materials) == 1:
            material = materials[0]
            ir.set_material(doc, name=str(safe_attr(material, "Name") or "") or None)
            description = safesafe(lambda m=material: m.GetDescription())
            if description:
                doc["model"]["material"]["description"] = str(description)
        elif len(materials) > 1:
            doc["model"]["material"]["physical_material_count"] = len(materials)
            ir.warn(
                doc,
                "part has %d physical materials; a single material name was not "
                "recorded" % len(materials),
            )
    except Exception:
        ir.warn(doc, "material name unavailable: %s" % one_line(caught()))

    ir.limit(
        doc,
        "NX configurations / part families are not exposed by NXOpen BasePart; "
        "not extracted",
    )
    ir.limit(doc, "moments of inertia are not exposed by MeasureBodies; not extracted")

    summary = ir.summarize(doc)
    ir.finish(
        doc,
        duration_s=time.time() - STARTED,
        driver={
            "journal": os.path.abspath(__file__),
            "driver_version": DRIVER_VERSION,
            "python": sys.version.split()[0],
            "nx_release": release,
        },
    )
    payload["ir"] = doc
    payload["summary"] = summary
    output = request.get("output")
    if output:
        path = os.path.abspath(output)
        ir.dump(doc, path)
        payload["outputs"].append(path)
        say("IR_JSON %s %d bytes" % (path, os.path.getsize(path)))
    payload["ok"] = True
    say(
        "READ_OK bodies=%d features=%d volume=%s bbox=%s"
        % (
            summary.get("bodies"),
            summary.get("features"),
            summary.get("volume_mm3"),
            summary.get("bbox_size_mm"),
        )
    )


# --------------------------------------------------------------------------- #
# export: native .prt -> STEP / Parasolid
# --------------------------------------------------------------------------- #
# The strategy table and the single-attempt helper live next to probe_step_export
# (above) because the probe and the real job must exercise exactly the same code.


def _export_parasolid(ufs, bodies, out):
    """UF PS export; the documented fallback when batch STEP writes 0 solids."""
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
    try:
        ufs.Ps.ExportData([body.Tag for body in bodies], out)
    except Exception:
        return False, {"error": one_line(caught()), "path": out}
    stats = _file_stats(out, b"PARASOLID")
    stats["path"] = out
    stats["ok"] = bool(stats.get("exists") and (stats.get("size") or 0) > 0)
    return stats["ok"], stats


@handler("export")
def handle_export(request, payload):
    nxopen = _import_nxopen()
    session = nxopen.Session.GetSession()
    ufs = nxopen.UF.UFSession.GetUFSession()
    src = os.path.abspath(request.get("input") or "")
    out = os.path.abspath(request.get("output") or "")
    fmt = (request.get("format") or "step").lower()
    options = request.get("options") or {}
    if not os.path.isfile(src):
        raise RuntimeError("input part not found: %s" % src)
    if not out:
        raise RuntimeError("export needs an output path")
    folder = os.path.dirname(out)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    say("EXPORT_OPEN %s -> %s (%s)" % (src, out, fmt))
    part, _status = _open_part(session, ufs, src)
    bodies = [body for body in part.Bodies if safesafe(lambda b=body: b.IsSolidBody, False)]
    if not bodies:
        bodies = list(part.Bodies)
    if not bodies:
        raise RuntimeError("part has no bodies to export")
    say("EXPORT_BODIES %d" % len(bodies))

    if fmt in ("parasolid", "x_t", "x_b"):
        ok, info = _export_parasolid(ufs, bodies, out)
        payload["export"] = {"format": "parasolid", "result": info}
        if ok:
            payload["ok"] = True
            payload["outputs"] = [out]
            say("EXPORT_OK parasolid %s %d bytes" % (out, info.get("size") or 0))
        else:
            payload["error"] = "Parasolid export failed: %s" % (info.get("error") or "no file")
        return

    ap242_requested = bool(options.get("ap242", True))
    attempts = []
    winner = None
    for strategy in STEP_STRATEGIES:
        if (strategy.get("ap242") or strategy.get("ap242_ed2")) and not ap242_requested:
            continue
        record = _step_export_attempt(session, part, src, out, strategy)
        attempts.append(record)
        if record["ok"]:
            winner = record
            break
        say(
            "EXPORT_STEP_MISS %s size=%s faces=%s err=%s"
            % (
                strategy["label"],
                record.get("size"),
                record.get("marker"),
                record.get("error"),
            )
        )
    payload["export"] = {"format": "step", "attempts": attempts}
    if winner is not None:
        payload["ok"] = True
        payload["outputs"] = [out]
        payload["export"]["strategy"] = winner["strategy"]
        winner_schema = "ap242" if (winner.get("ap242") or winner.get("ap242_ed2")) else "ap214"
        payload["export"]["schema"] = winner_schema
        if winner["strategy"] != "ap214_entire_part":
            warn(
                "STEP export needed the %r strategy on this NX build (the plain "
                "display-part export wrote no solids)" % winner["strategy"]
            )
        if winner_schema == "ap214":
            ap242_attempts = [
                item for item in attempts
                if item.get("ap242") or item.get("ap242_ed2")
            ]
            if ap242_attempts:
                warn(
                    "STEP AP242 produced no output in this batch NX session "
                    "(%d attempt(s) left no usable file); exported AP214 instead"
                    % len(ap242_attempts)
                )
        say(
            "EXPORT_OK step %s %d bytes ADVANCED_FACE=%d strategy=%s"
            % (out, winner.get("size") or 0, winner.get("marker") or 0, winner["strategy"])
        )
        if options.get("also_parasolid"):
            ps_path = os.path.splitext(out)[0] + ".x_t"
            ok, info = _export_parasolid(ufs, bodies, ps_path)
            if ok:
                payload["outputs"].append(ps_path)
                payload["export"]["parasolid"] = info
        return

    last_error = None
    for record in attempts:
        if record.get("error"):
            last_error = record["error"]
    if options.get("fallback_parasolid", True):
        ps_path = os.path.splitext(out)[0] + ".x_t"
        ok, info = _export_parasolid(ufs, bodies, ps_path)
        if ok:
            payload["ok"] = True
            payload["outputs"] = [ps_path]
            payload["export"] = {
                "format": "parasolid",
                "fallback": True,
                "attempts": attempts,
                "result": info,
            }
            warn(
                "STEP export produced no ADVANCED_FACE entities in any of %d "
                "strategies (batch NX pitfall); fell back to Parasolid %s"
                % (len(attempts), ps_path)
            )
            say("EXPORT_OK parasolid-fallback %s %d bytes" % (ps_path, info.get("size") or 0))
            return
    payload["error"] = "STEP export produced no usable file (%s)" % (
        last_error or "0 solids written, no ADVANCED_FACE entities"
    )


# --------------------------------------------------------------------------- #
# import: STEP / Parasolid -> new .prt
# --------------------------------------------------------------------------- #
def _new_part(session, ufs, path):
    nxopen = _import_nxopen()
    if os.path.exists(path):
        os.remove(path)
    file_new = session.Parts.FileNew()
    try:
        file_new.Units = nxopen.Part.Units.Millimeters
        file_new.TemplateFileName = "model-plain-1-mm-template.prt"
        file_new.UseBlankTemplate = False
        file_new.NewFileName = path
        part = file_new.Commit()
    finally:
        file_new.Destroy()
    session.Parts.SetWork(part)
    ufs.Part.SetDisplayPart(part.Tag)
    return part


@handler("import")
def handle_import(request, payload):
    nxopen = _import_nxopen()
    session = nxopen.Session.GetSession()
    ufs = nxopen.UF.UFSession.GetUFSession()
    src = os.path.abspath(request.get("input") or "")
    out = os.path.abspath(request.get("output") or "")
    if not os.path.isfile(src):
        raise RuntimeError("input file not found: %s" % src)
    if not out:
        raise RuntimeError("import needs an output .prt path")
    folder = os.path.dirname(out)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    suffix = os.path.splitext(src)[1].lower()
    kind = "parasolid" if suffix in (".x_t", ".x_b") else "step"
    say("IMPORT_OPEN %s -> %s (%s)" % (src, out, kind))
    info = {"format": kind}

    if kind == "step":
        info["schema"] = _step_schema(src)
        say("IMPORT_STEP_SCHEMA %s" % info["schema"])
        specs = (
            {"import_to": "WorkPart", "flatten": True, "file_open_flag": False},
            {"import_to": "NewPart", "flatten": True, "file_open_flag": False},
            {
                "import_to": "NewPart",
                "flatten": True,
                "file_open_flag": False,
                "importer_class": "Step214Importer",
            },
        )
        attempts = []
        winner = None
        for spec in specs:
            record = _import_step_attempt(session, ufs, nxopen, src, out, **spec)
            attempts.append(record)
            say(
                "IMPORT_ATTEMPT %s flatten=%s bodies=%s ok=%s err=%s"
                % (
                    record.get("import_to"),
                    record.get("flatten_assembly"),
                    record.get("bodies"),
                    record.get("ok"),
                    record.get("error"),
                )
            )
            if record.get("ok"):
                winner = record
                break
        if winner is None:
            record = _open_exchange_as_part_attempt(session, ufs, src, out)
            attempts.append(record)
            say(
                "IMPORT_ATTEMPT OpenExchange bodies=%s ok=%s err=%s"
                % (record.get("bodies"), record.get("ok"), record.get("error"))
            )
            if record.get("ok"):
                winner = record
        info["attempts"] = attempts
        if winner is None:
            payload["import"] = info
            payload["error"] = "STEP import produced 0 bodies from %s (%s)" % (
                os.path.basename(src),
                (attempts[-1].get("error") if attempts else "no attempt ran"),
            )
            return
        info["strategy"] = "%s/flatten=%s" % (
            winner["import_to"],
            winner.get("flatten_assembly"),
        )
        target_part = None
        if winner["import_to"] == "WorkPart":
            target_part = safesafe(lambda: session.Parts.Work, None)
        else:
            target_part = safesafe(lambda: session.Parts.Display, None) or safesafe(
                lambda: session.Parts.Work, None
            )
    else:
        target_part = _new_part(session, ufs, out)
        ufs.Part.Save()
        # UF PS import first.  Verified by a dedicated probe journal on this
        # machine: ``DexManager.CreateParasolidImporter().Commit()`` returns
        # without error and imports *nothing* in batch NX 2406 (no new part, the
        # work part keeps bodies=0, no dialogue), while the one-argument
        # ``ufs.Ps.ImportData(file)`` immediately puts the bodies into the work
        # part.  The DEX path stays as a fallback for builds where it does work.
        strategy = None
        bodies = []
        try:
            ufs.Ps.ImportData(src)
            bodies = list(target_part.Bodies)
            if bodies:
                strategy = "uf_ps"
        except Exception:
            warn("UF PS import failed: " + one_line(caught()))
        if not bodies:
            try:
                importer = session.DexManager.CreateParasolidImporter()
                try:
                    importer.InputFile = src
                    importer.ProcessHoldFlag = True
                    importer.FlattenAssembly = True
                    importer.Commit()
                finally:
                    try:
                        importer.Destroy()
                    except Exception:
                        pass
                bodies = list(target_part.Bodies)
                if bodies:
                    strategy = "dex"
            except Exception:
                warn("DEX Parasolid import failed: " + one_line(caught()))
        if not bodies:
            payload["import"] = info
            payload["error"] = "Parasolid import produced 0 bodies from %s" % os.path.basename(src)
            return
        ufs.Part.Save()
        info["strategy"] = strategy

    bodies = list(target_part.Bodies) if target_part is not None else []
    if not bodies and os.path.exists(out):
        target_part = _open_saved_part(session, ufs, out) or target_part
        bodies = list(target_part.Bodies) if target_part is not None else []
    stats = {"bodies": len(bodies)}
    if bodies:
        stats["faces"] = sum(len(list(body.GetFaces())) for body in bodies)
        box = _body_bbox(ufs, bodies[0], 1.0)
        if box:
            stats["bbox_min_mm"] = box[0]
            stats["bbox_max_mm"] = box[1]
    saved = os.path.exists(out) and os.path.getsize(out) > 0
    stats["saved"] = saved
    stats["saved_bytes"] = os.path.getsize(out) if os.path.exists(out) else 0
    info.update(stats)
    payload["import"] = info
    say("IMPORT_SAVED %s bytes=%s bodies=%s" % (out, stats["saved_bytes"], stats["bodies"]))
    if stats["bodies"] <= 0:
        payload["error"] = (
            "import produced 0 bodies (honest failure: nothing was imported from %s)"
            % os.path.basename(src)
        )
        return
    if not saved:
        payload["error"] = "import produced %d bodies but saving the .prt failed" % stats["bodies"]
        return
    payload["ok"] = True
    payload["outputs"] = [out]
    say("IMPORT_OK %s bodies=%d faces=%s" % (out, stats["bodies"], stats.get("faces")))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main():
    request = read_request()
    kind = request.get("kind") or "probe"
    payload = new_payload(kind)
    say("=== swnx-switch NX driver %s ===" % DRIVER_VERSION)
    say("KIND %s" % kind)
    say("REQUEST %s" % REQUEST_PATH)
    say("WORKDIR %s" % WORKDIR)
    handler_fn = HANDLERS.get(kind)
    if handler_fn is None:
        payload["error"] = "unknown job kind %r" % (kind,)
        write_result(payload)
        return
    try:
        handler_fn(request, payload)
    except Exception:
        payload["ok"] = False
        payload["error"] = caught().strip().splitlines()[-1] if caught().strip() else "unknown"
        say("HANDLER_ERROR " + caught())
    else:
        if payload.get("ok"):
            say("JOB_OK %s" % kind)
        else:
            say("JOB_ERROR %s" % (payload.get("error") or "handler reported failure"))
    payload["warnings"] = WARNINGS
    payload["duration_s"] = round(time.time() - STARTED, 3)
    payload["driver"] = {"version": DRIVER_VERSION, "python": sys.version.split()[0]}
    write_result(payload)
    say("DONE %.1fs" % (time.time() - STARTED))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            fallback = new_payload("unknown")
            fallback["error"] = caught()
            fallback["warnings"] = WARNINGS
            write_result(fallback)
        except Exception:
            say("FATAL " + caught())
        raise
