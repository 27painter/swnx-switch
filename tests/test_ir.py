"""IR model tests — no CAD application required."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swnx_switch import ir  # noqa: E402


def _sample() -> dict:
    doc = ir.new(
        backend="nx",
        app="Siemens NX",
        app_version="2406",
        source_format="prt",
        name="bracket",
    )
    ir.set_material(doc, name="Steel", density_kg_m3=7850.0)
    ir.set_mass_properties(
        doc,
        volume_mm3=1234.5,
        surface_area_mm2=678.9,
        mass_kg=0.00969,
        centroid_mm=(1.0, 2.0, 3.0),
    )
    ir.set_bbox(doc, min_mm=(0, 0, 0), max_mm=(10, 20, 30))
    ir.add_body(doc, name="Body1", faces=42, edges=84, volume_mm3=1234.5, min_mm=(0, 0, 0), max_mm=(10, 20, 30))
    ir.add_feature(doc, name="Extrude1", type="Extrude", parameters={"depth_mm": 20.0})
    ir.add_surface(doc, "cylinder", radius_mm=5.0, axis=[0, 0, 1], origin_mm=[1, 2, 0], area_mm2=314.16)
    ir.add_custom_properties(doc, {"材料": "45钢", "图号": "SW-NX-001"})
    return ir.finish(doc, duration_s=1.5)


def test_new_document_is_valid_and_mm_only() -> None:
    doc = _sample()
    assert ir.validate(doc) == []
    assert doc["schema"] == ir.SCHEMA
    assert doc["model"]["units"] == "mm"
    assert doc["model"]["bounding_box"]["size_mm"] == [10.0, 20.0, 30.0]


def test_roundtrip_json() -> None:
    doc = _sample()
    # A workspace-local scratch dir rather than pytest's tmp_path: the DSH file
    # sandbox denies deletion in the system temp tree, and a test that cannot
    # clean up should not depend on being allowed to.
    scratch = Path(__file__).resolve().parents[1] / "work" / "test-ir"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "part.ir.json"
    ir.dump(doc, path)
    loaded = ir.load(path)
    assert loaded["model"]["name"] == "bracket"
    assert loaded["model"]["geometry_digest"]["cylinder"][0]["radius_mm"] == 5.0
    assert loaded["model"]["custom_properties"]["图号"] == "SW-NX-001"


def test_summary_is_agent_friendly() -> None:
    summary = ir.summarize(_sample())
    assert summary["bodies"] == 1
    assert summary["features"] == 1
    assert summary["volume_mm3"] == 1234.5
    assert summary["bbox_size_mm"] == [10.0, 20.0, 30.0]
    assert summary["surfaces"] == {"cylinder": 1}


def test_missing_spine_is_reported() -> None:
    doc = _sample()
    del doc["model"]["bodies"]
    problems = ir.validate(doc)
    assert any("model.bodies" in p for p in problems)

    broken = _sample()
    broken["schema"] = "something/else"
    assert any("schema" in p for p in ir.validate(broken))


def test_wrong_units_are_rejected() -> None:
    doc = _sample()
    doc["model"]["units"] = "m"
    assert any("units" in p for p in ir.validate(doc))


def test_surface_kind_falls_back_to_other() -> None:
    doc = _sample()
    ir.add_surface(doc, "nurbs", area_mm2=1.0)
    assert doc["model"]["geometry_digest"]["other"][0]["kind"] == "other"


if __name__ == "__main__":  # pragma: no cover - plain-run fallback
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                if "tmp_path" in func.__code__.co_varnames:
                    import tempfile

                    with tempfile.TemporaryDirectory() as tmp:
                        func(Path(tmp))
                else:
                    func()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
