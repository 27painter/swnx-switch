"""Pipeline tests that need no CAD application.

The conversion itself needs SolidWorks/NX, but the wiring around it — format
detection, route choice, tolerance aliases, and the report hand-off — is where a
silent integration bug would hide, and all of it is pure Python.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from swnx_switch import cli, ir  # noqa: E402
from swnx_switch.backends import detect_format, format_owner  # noqa: E402
from swnx_switch.errors import SwnxError, UnsupportedFormat  # noqa: E402
from swnx_switch.pipeline import ConversionAttempt, ConversionResult, Pipeline, choose_exchanges  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRATCH = REPO / "work" / "test-pipeline"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_detect_format_and_owner() -> None:
    assert detect_format("a.SLDPRT") == "sldprt"
    assert detect_format("b.prt") == "prt"
    assert detect_format("c.step") == "step"
    assert detect_format("d.x_t") == "parasolid"
    assert format_owner("sldprt") == "solidworks"
    assert format_owner("prt") == "nx"
    assert format_owner("step") is None
    try:
        detect_format("notes.txt")
    except UnsupportedFormat:
        pass
    else:
        raise AssertionError("txt must be rejected")


def test_choose_exchanges() -> None:
    # Any route through NX prefers Parasolid: NX's batch STEP translator is
    # unreliable (metadata-only export, 0-body import) while Parasolid needs no
    # translator. A non-NX pair still prefers STEP.
    assert choose_exchanges("auto", "sldprt", "prt") == ["parasolid", "step"]
    assert choose_exchanges("auto", "prt", "sldprt") == ["parasolid", "step"]
    assert choose_exchanges("auto", "sldprt", "step") == ["step", "parasolid"]
    assert choose_exchanges("step", "sldprt", "prt") == ["step"]
    assert choose_exchanges("parasolid", "prt", "sldprt") == ["parasolid"]
    try:
        choose_exchanges("dwg", "prt", "sldprt")
    except SwnxError:
        pass
    else:
        raise AssertionError("unknown mode must be rejected")


def test_tolerance_short_names_map_to_validator_keys() -> None:
    parsed = cli._parse_tolerances(["volume=2e-3", "bbox_abs=0.2", "centroid=0.01"])
    assert parsed == {"volume_rel": 2e-3, "bbox_abs_mm": 0.2, "centroid_abs_mm": 0.01}
    # canonical names pass through unchanged
    assert cli._parse_tolerances(["area_rel=5e-3"]) == {"area_rel": 5e-3}
    for bad in (["volume"], ["nope=1"], ["volume=abc"]):
        try:
            cli._parse_tolerances(bad)
        except SwnxError:
            continue
        raise AssertionError(f"{bad} should be rejected")


def test_verify_wires_validate_and_report() -> None:
    """The pipeline must turn two IR docs into a verdict AND two report files."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(workdir=SCRATCH / "work", outdir=SCRATCH / "out")
    run_dir = SCRATCH / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    result = ConversionResult(
        ok=True,
        source=Path("a.sldprt"),
        target=Path("a.prt"),
        source_format="sldprt",
        target_format="prt",
        run_dir=run_dir,
    )
    result.reference_ir = _fixture("ref_part.json")
    result.candidate_ir = _fixture("candidate_volume_5pct.json")
    result.attempts.append(ConversionAttempt(exchange="step", ok=True, export_ok=True, import_ok=True))

    pipeline._verify(result, tolerances=None, report=True, task="unit-test")

    assert result.comparison is not None
    assert result.verdict == "fail", "a 5% volume loss must fail"
    assert result.ok is False
    reports = sorted(path.name for path in run_dir.iterdir())
    assert "report.json" in reports, reports
    assert any(name.startswith("validation") for name in reports), reports
    payload = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "swnx.convert-report/1"
    assert payload["verdict"] == "fail"


def test_verify_passes_a_clean_conversion() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(workdir=SCRATCH / "work2", outdir=SCRATCH / "out2")
    run_dir = SCRATCH / "run-ok"
    run_dir.mkdir(parents=True, exist_ok=True)
    result = ConversionResult(
        ok=True,
        source=Path("a.sldprt"),
        target=Path("a.prt"),
        source_format="sldprt",
        target_format="prt",
        run_dir=run_dir,
    )
    result.reference_ir = _fixture("ref_part.json")
    result.candidate_ir = _fixture("candidate_identical.json")
    pipeline._verify(result, tolerances=None, report=True, task=None)
    assert result.verdict == "pass", result.comparison
    assert result.ok is True


def test_ir_roundtrip_through_pipeline_scratch() -> None:
    """IR JSON written by one stage must load in the next (schema check included)."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    doc = _fixture("ref_part.json")
    path = SCRATCH / "ref.ir.json"
    ir.dump(doc, path)
    loaded = ir.load(path)
    assert loaded["schema"] == ir.SCHEMA
    assert ir.summarize(loaded)["bodies"] >= 1


if __name__ == "__main__":  # pragma: no cover
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001 - plain runner
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
