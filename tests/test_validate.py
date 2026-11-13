"""Tests for the swnx-switch verification layer (``validate`` + ``report``).

These are behaviour tests, not smoke tests: every fixture encodes a conversion
outcome a real SW<->NX job can produce (lost body, 5% volume loss, missing mass
properties, STEP topology churn, feature-tree loss, a missing bbox axis, a
rigid placement offset), and each test asserts the exact status/verdict the
pipeline and the report depend on.

Runs two ways on purpose (the repo has no test dependency beyond optional
pytest)::

    python -m pytest tests/test_validate.py -q
    python tests/test_validate.py
"""

from __future__ import annotations

import copy
import json
import math
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from swnx_switch import ir  # noqa: E402
from swnx_switch import report as report_module  # noqa: E402
from swnx_switch.report import render_markdown, write_report  # noqa: E402
from swnx_switch.validate import (  # noqa: E402
    COMPARE_SCHEMA,
    DEFAULT_TOLERANCES,
    STATUSES,
    compare,
    verdict_of,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

EXPECTED_CHECK_IDS = (
    "ir_schema",
    "units",
    "body_count",
    "volume",
    "surface_area",
    "bbox_x",
    "bbox_y",
    "bbox_z",
    "bbox_corner",
    "centroid",
    "mass",
    "density",
    "mass_density",
    "topology",
    "feature_tree",
)

EXPECTED_CHECK_FIELDS = ("id", "label", "status", "detail", "expected", "actual", "delta", "tolerance")

META = {
    "source_path": r"D:\27painter\Documents\sw_job\build_final\叉形接头.SLDPRT",
    "target_path": r"D:\27painter\Documents\swnx-switch\out\叉形接头.prt",
    "source_sha256": "a3f1" + "0" * 60,
    "target_sha256": "b7c2" + "1" * 60,
    "mode": "sldprt -> STEP AP242 -> prt",
    "timings": {"read_source": 2.3, "convert": 41.2, "read_candidate": 4.1, "total": 47.6},
    "started_at": "2026-11-13T17:40:00+08:00",
    "finished_at": "2026-11-13T17:42:31+08:00",
    "json_path": r"D:\27painter\Documents\swnx-switch\out\conv-bracket.json",
}

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x1F1E6, 0x1F1FF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def reference() -> dict:
    return fixture("ref_part.json")


def candidate(name: str) -> dict:
    return fixture(name)


def check_of(result: dict, check_id: str) -> dict:
    for check in result["checks"]:
        if check["id"] == check_id:
            return check
    raise AssertionError(f"check {check_id!r} missing from {[c['id'] for c in result['checks']]}")


def statuses(result: dict) -> dict[str, str]:
    return {check["id"]: check["status"] for check in result["checks"]}


def has_emoji(text: str) -> bool:
    return any(any(low <= ord(char) <= high for low, high in _EMOJI_RANGES) for char in text)


def table_widths(markdown: str) -> list[int]:
    return [report_module._display_width(line) for line in markdown.splitlines() if line.startswith("|")]


def table_header_cells(markdown: str) -> list[str]:
    for line in markdown.splitlines():
        if line.startswith("| 检查项"):
            return [cell.strip() for cell in line.strip().strip("|").split("|")]
    raise AssertionError("check table header not found")


def workspace_temp_stem() -> str:
    """A unique file stem written directly into ``tests/``.

    The sandbox this repo is developed in grants write access to existing
    workspace directories but not to directories created later, so the report
    test writes two files next to itself and deletes them afterwards.
    """
    return f"_swnx-report-test-{uuid.uuid4().hex[:8]}"


def with_changes(doc: dict, **paths_and_values) -> dict:
    """Deep-copy ``doc`` and apply ``model.bodies.0.faces=44`` style overrides."""
    changed = copy.deepcopy(doc)
    for dotted, value in paths_and_values.items():
        node = changed
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[int(part)] if part.isdigit() else node[part]
        node[parts[-1]] = value
    return changed


# --------------------------------------------------------------------------- #
# fixtures themselves
# --------------------------------------------------------------------------- #
def test_every_fixture_is_a_valid_ir_document():
    for path in sorted(FIXTURES.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        problems = ir.validate(document)
        assert problems == [], f"{path.name} is not valid IR: {problems}"


# --------------------------------------------------------------------------- #
# pass case
# --------------------------------------------------------------------------- #
def test_identical_documents_pass():
    result = compare(reference(), candidate("candidate_identical.json"))
    assert result["schema"] == COMPARE_SCHEMA
    assert result["verdict"] == "pass"
    assert statuses(result) == {check_id: "pass" for check_id in EXPECTED_CHECK_IDS}
    assert result["summary"]["fail"] == 0
    assert result["summary"]["required_unavailable"] == []
    assert result["summary"]["checks"] == len(EXPECTED_CHECK_IDS)
    volume = check_of(result, "volume")
    assert volume["delta"] == 0.0
    expected_tolerance = max(
        DEFAULT_TOLERANCES["volume_abs_mm3"],
        DEFAULT_TOLERANCES["volume_rel"] * 125000.0,
    )
    assert volume["tolerance"] == expected_tolerance
    assert isinstance(volume["delta"], (int, float))
    assert isinstance(volume["tolerance"], (int, float))


def test_result_shape_is_stable_and_json_serialisable():
    result = compare(reference(), candidate("candidate_identical.json"))
    for key in ("schema", "verdict", "checks", "summary", "source", "candidate", "tolerances"):
        assert key in result, f"missing top-level key {key!r}"
    for check in result["checks"]:
        for field in EXPECTED_CHECK_FIELDS:
            assert field in check, f"check {check.get('id')} missing {field!r}"
        assert check["status"] in STATUSES
        assert isinstance(check["detail"], str) and check["detail"]
        assert isinstance(check["label"], str) and check["label"]
    assert [check["id"] for check in result["checks"]] == list(EXPECTED_CHECK_IDS)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert result["source"]["bodies"] == 1
    assert result["candidate"]["format"] == "prt"


def test_default_tolerances_are_defined_and_overrideable():
    for key in (
        "volume_rel",
        "volume_abs_mm3",
        "area_rel",
        "bbox_abs_mm",
        "bbox_rel",
        "bbox_corner_abs_mm",
        "centroid_abs_mm",
        "mass_rel",
        "density_rel",
    ):
        assert key in DEFAULT_TOLERANCES
        assert isinstance(DEFAULT_TOLERANCES[key], float)
        assert DEFAULT_TOLERANCES[key] > 0
    result = compare(reference(), candidate("candidate_identical.json"), {"volume_rel": 5e-3})
    assert result["tolerances"]["volume_rel"] == 5e-3
    assert result["tolerances"]["area_rel"] == DEFAULT_TOLERANCES["area_rel"]


# --------------------------------------------------------------------------- #
# fail cases
# --------------------------------------------------------------------------- #
def test_volume_five_percent_off_fails():
    result = compare(reference(), candidate("candidate_volume_5pct.json"))
    volume = check_of(result, "volume")
    assert volume["status"] == "fail"
    assert math.isclose(volume["delta"], 6250.0, rel_tol=1e-12)
    assert math.isclose(volume["delta_rel"], 0.05, rel_tol=1e-9)
    assert result["verdict"] == "fail"
    assert "volume" in result["summary"]["failed"]
    assert "volume" in result["summary"]["verdict_basis"]


def test_body_count_mismatch_fails():
    result = compare(reference(), candidate("candidate_body_mismatch.json"))
    bodies = check_of(result, "body_count")
    assert bodies["status"] == "fail"
    assert bodies["expected"] == 1
    assert bodies["actual"] == 2
    assert bodies["delta"] == 1.0
    assert result["verdict"] == "fail"


def test_units_mismatch_fails():
    reference_doc = reference()
    candidate_doc = with_changes(candidate("candidate_identical.json"), **{"model.units": "inch"})
    result = compare(reference_doc, candidate_doc)
    assert check_of(result, "units")["status"] == "fail"
    assert result["verdict"] == "fail"


def test_missing_mass_properties_is_unavailable_not_a_pass():
    result = compare(reference(), candidate("candidate_missing_mass.json"))
    for check_id in ("volume", "surface_area"):
        check = check_of(result, check_id)
        assert check["status"] == "unavailable", check_id
        assert check["delta"] is None
        assert check["tolerance"] is None
        assert "unavailable" in check["detail"].lower() or "无法" in check["detail"]
    # A required measurement that cannot be taken must never produce a pass.
    assert result["verdict"] == "fail"
    assert "volume" in result["summary"]["required_unavailable"]
    assert "surface_area" in result["summary"]["required_unavailable"]
    # Optional checks missing on one side do not crash the comparison.
    assert check_of(result, "mass")["status"] == "unavailable"


def test_feature_tree_loss_is_information_only():
    result = compare(reference(), candidate("candidate_feature_loss.json"))
    features = check_of(result, "feature_tree")
    assert features["status"] == "warn"
    assert features["severity"] == "info"
    assert features["delta"] == 6.0
    assert len(features["lost"]) == 6
    assert result["verdict"] == "pass"
    assert "feature_tree" in result["summary"]["informational"]
    assert "feature_tree" not in result["summary"]["warned"]


def test_missing_bbox_axis_is_unavailable():
    result = compare(reference(), candidate("candidate_missing_bbox_axes.json"))
    assert check_of(result, "bbox_x")["status"] == "pass"
    assert check_of(result, "bbox_y")["status"] == "pass"
    bbox_z = check_of(result, "bbox_z")
    assert bbox_z["status"] == "unavailable"
    assert "Z" in bbox_z["detail"]
    assert check_of(result, "bbox_corner")["status"] == "unavailable"
    assert result["verdict"] == "fail"
    assert "bbox_z" in result["summary"]["required_unavailable"]


def test_bbox_shift_warns_but_does_not_fail():
    result = compare(reference(), candidate("candidate_bbox_shift.json"))
    corner = check_of(result, "bbox_corner")
    assert corner["status"] == "warn"
    assert math.isclose(corner["actual"], math.sqrt(3 * 0.1**2), rel_tol=1e-6)
    assert result["verdict"] == "warn"
    for check_id in ("bbox_x", "bbox_y", "bbox_z", "volume", "surface_area"):
        assert check_of(result, check_id)["status"] == "pass", check_id


# --------------------------------------------------------------------------- #
# tolerance overrides
# --------------------------------------------------------------------------- #
def test_tolerance_override_can_absorb_a_placement_offset():
    strict = compare(reference(), candidate("candidate_bbox_shift.json"))
    assert strict["verdict"] == "warn"
    relaxed = compare(reference(), candidate("candidate_bbox_shift.json"), {"bbox_corner_abs_mm": 0.5})
    assert check_of(relaxed, "bbox_corner")["status"] == "pass"
    assert relaxed["tolerances"]["bbox_corner_abs_mm"] == 0.5
    assert relaxed["verdict"] == "pass"


def test_tolerance_override_changes_volume_verdict_and_ignores_junk():
    result = compare(
        reference(),
        candidate("candidate_volume_5pct.json"),
        {"volume_rel": 0.1, "not_a_tolerance": 3, "volume_abs_mm3": -1},
    )
    assert check_of(result, "volume")["status"] == "pass"
    assert result["tolerances"]["not_a_tolerance"] == 3.0
    assert result["tolerances"]["volume_abs_mm3"] == DEFAULT_TOLERANCES["volume_abs_mm3"]
    assert any("忽略非法覆盖" in warning for warning in result["warnings"])


# --------------------------------------------------------------------------- #
# robust numeric / malformed input handling
# --------------------------------------------------------------------------- #
def test_compare_never_crashes_on_empty_or_partial_documents():
    empty = compare({}, {})
    assert empty["verdict"] == "fail"
    assert all(check["status"] in STATUSES for check in empty["checks"])
    for check_id in ("volume", "surface_area", "body_count", "bbox_x"):
        assert check_of(empty, check_id)["status"] == "unavailable"
    assert empty["summary"]["required_unavailable"]

    assert compare(reference(), None)["verdict"] == "fail"
    assert compare(None, reference())["verdict"] == "fail"
    no_model = {"schema": "swnx.ir/1", "generated_at": "2026-11-13T00:00:00+08:00", "model": {}}
    partial = compare(reference(), no_model)
    assert partial["verdict"] == "fail"
    assert check_of(partial, "ir_schema")["status"] == "warn"


def test_zero_volume_does_not_divide_by_zero():
    reference_doc = reference()
    zero = copy.deepcopy(reference_doc)
    zero["model"]["mass_properties"]["volume_mm3"] = 0.0
    zero["model"]["mass_properties"]["mass_kg"] = 0.0
    same = compare(zero, zero)
    assert check_of(same, "volume")["status"] == "pass"
    assert check_of(same, "volume")["delta_rel"] is None

    one_side = copy.deepcopy(reference_doc)
    one_side["model"]["mass_properties"]["volume_mm3"] = 0.0
    result = compare(one_side, reference_doc)
    assert check_of(result, "volume")["status"] == "fail"


def test_bbox_is_derived_from_min_max_when_size_is_absent():
    candidate_doc = copy.deepcopy(candidate("candidate_identical.json"))
    candidate_doc["model"]["bounding_box"] = {
        "min_mm": [-5.0, -5.0, 0.0],
        "max_mm": [105.0, 55.0, 20.0],
    }
    result = compare(reference(), candidate_doc)
    for check_id in ("bbox_x", "bbox_y", "bbox_z"):
        check = check_of(result, check_id)
        assert check["status"] == "pass", check_id
        assert check["derived"] is True


def test_non_numeric_values_are_treated_as_missing():
    candidate_doc = copy.deepcopy(candidate("candidate_identical.json"))
    candidate_doc["model"]["mass_properties"]["volume_mm3"] = "not-a-number"
    candidate_doc["model"]["mass_properties"]["surface_area_mm2"] = None
    result = compare(reference(), candidate_doc)
    assert check_of(result, "volume")["status"] == "unavailable"
    assert check_of(result, "surface_area")["status"] == "unavailable"
    assert result["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# informational topology / mass-density checks
# --------------------------------------------------------------------------- #
def test_face_edge_delta_is_informational():
    candidate_doc = with_changes(
        candidate("candidate_identical.json"),
        **{"model.bodies.0.faces": 44, "model.bodies.0.edges": 100},
    )
    result = compare(reference(), candidate_doc)
    topology = check_of(result, "topology")
    assert topology["status"] == "warn"
    assert topology["severity"] == "info"
    assert topology["delta"] == 6.0
    assert result["verdict"] == "pass"


def test_mass_density_mismatch_is_a_warning():
    # Both sides agree on mass (so the mass check passes) but neither mass is
    # consistent with volume * density: the unit-consistency tripwire warns.
    reference_doc = with_changes(reference(), **{"model.mass_properties.mass_kg": 0.9})
    candidate_doc = with_changes(candidate("candidate_identical.json"), **{"model.mass_properties.mass_kg": 0.9})
    result = compare(reference_doc, candidate_doc)
    assert check_of(result, "mass")["status"] == "pass"
    assert check_of(result, "density")["status"] == "pass"
    assert check_of(result, "mass_density")["status"] == "warn"
    assert result["verdict"] == "warn"


def test_verdict_of_is_consistent_with_compare():
    for name in (
        "candidate_identical.json",
        "candidate_volume_5pct.json",
        "candidate_missing_mass.json",
        "candidate_body_mismatch.json",
        "candidate_feature_loss.json",
        "candidate_missing_bbox_axes.json",
        "candidate_bbox_shift.json",
    ):
        result = compare(reference(), candidate(name))
        assert verdict_of(result) == result["verdict"], name
    assert verdict_of({}) == "fail"
    assert verdict_of({"checks": []}) == "fail"
    assert verdict_of({"checks": [{"status": "warn", "severity": "info"}]}) == "pass"
    assert verdict_of({"checks": [{"status": "warn", "severity": "optional"}]}) == "warn"
    assert verdict_of({"checks": [{"status": "unavailable", "severity": "required"}]}) == "fail"


# --------------------------------------------------------------------------- #
# report renderer
# --------------------------------------------------------------------------- #
def test_report_renders_sections_paths_hashes_and_verdict():
    result = compare(reference(), candidate("candidate_volume_5pct.json"))
    markdown = render_markdown(result, META)
    for needle in (
        "# 转换验证报告",
        "结论 (Verdict)",
        "**失败 (FAIL)**",
        "## 概要 (Summary)",
        "参考件 (Reference/source)",
        "转换件 (Candidate/target)",
        META["source_path"],
        META["target_path"],
        META["source_sha256"],
        META["target_sha256"],
        "转换方式 (Conversion mode)",
        META["mode"],
        "耗时 (Timings)",
        "格式转换 41.2 s",
        "## 检查结果 (Checks)",
        "体积 (volume)",
        "失败 fail",
        "## 统计 (Statistics)",
        "## 发现详情 (Findings)",
        "## 警告 (Warnings)",
        "## 限制 (Limitations)",
        "## 机器可读结果 (Machine-readable)",
        META["json_path"],
    ):
        assert needle in markdown, f"report missing {needle!r}"
    assert table_header_cells(markdown) == ["检查项", "实测", "期望", "偏差", "容差", "结论"]
    assert has_emoji(markdown) is False, "report must not contain emojis"
    assert markdown.endswith("\n")
    widths = table_widths(markdown)
    assert widths, "expected at least one Markdown table"
    assert max(widths) <= 120, f"table too wide for a terminal: {max(widths)} columns"


def test_report_handles_missing_metadata_and_unavailable_checks():
    result = compare(reference(), candidate("candidate_missing_mass.json"))
    markdown = render_markdown(result)
    assert "**失败 (FAIL)**" in markdown
    assert "不可用 n/a" in markdown
    assert "无法确认的必需项 (required unavailable)" in markdown
    assert "未提供" in markdown  # paths/metadata absent
    assert "## 警告 (Warnings)" in markdown
    assert max(table_widths(markdown)) <= 120
    assert has_emoji(markdown) is False

    # A comparison with nothing to warn about says so instead of omitting the section.
    clean = render_markdown(compare(reference(), reference()))
    assert "无 (none)" in clean


def test_report_understands_pipeline_meta_aliases():
    # swnx_switch.pipeline builds meta as {"source", "target", "mode", "seconds"}.
    result = compare(reference(), candidate("candidate_identical.json"))
    pipeline_meta = {
        "source": r"D:\sw_job\build_final\叉形接头.SLDPRT",
        "target": r"D:\27painter\Documents\swnx-switch\out\叉形接头.prt",
        "mode": "sldprt -> prt",
        "seconds": 12.5,
        "warnings": ["driver note"],
    }
    markdown = render_markdown(result, pipeline_meta)
    assert pipeline_meta["source"] in markdown
    assert pipeline_meta["target"] in markdown
    assert "合计 12.5 s" in markdown
    assert "driver note" in markdown


def test_report_renders_informational_loss_without_overstating_verdict():
    result = compare(reference(), candidate("candidate_feature_loss.json"))
    markdown = render_markdown(result, {"mode": "sldprt -> step -> prt"})
    assert "**通过 (PASS)**" in markdown
    assert "特征树 (features)" in markdown
    assert "信息性差异 (informational): feature_tree" in markdown
    assert "不可用 n/a" not in markdown


def test_write_report_writes_markdown_and_json():
    result = compare(reference(), candidate("candidate_bbox_shift.json"))
    stem = workspace_temp_stem()
    directory = ROOT / "tests"
    try:
        paths = write_report(result, META, directory, stem=stem)
        assert set(paths) == {"markdown", "json"}
        markdown_path, json_path = paths["markdown"], paths["json"]
        assert markdown_path.name == f"{stem}.md"
        assert json_path.name == f"{stem}.json"
        assert markdown_path.exists() and json_path.exists()
        markdown = markdown_path.read_text(encoding="utf-8")
        assert "**警告 (WARN)**" in markdown
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["schema"] == "swnx.report/1"
        assert payload["comparison"]["verdict"] == "warn"
        assert payload["meta"]["target_path"] == META["target_path"]
        assert payload["meta"]["json_path"] == str(json_path)
        # A stem that already carries an extension must not double it.
        again = write_report(result, META, directory, stem=f"{stem}.md")
        assert again["markdown"].name == f"{stem}.md"
        assert again["json"].name == f"{stem}.json"
    finally:
        for leftover in directory.glob(f"{stem}*"):
            leftover.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# plain-python runner (pytest is optional in this repo)
# --------------------------------------------------------------------------- #
def _run_plain() -> int:
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - a runner must report every failure
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)
        else:
            print(f"PASS {name}")
    print(f"{len(tests) - failures} passed, {failures} failed, {len(tests)} total")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_plain())
