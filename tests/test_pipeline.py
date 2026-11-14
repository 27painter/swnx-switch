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
        source=run_dir / "a.sldprt",
        target=run_dir / "a.prt",
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
        source=run_dir / "a.sldprt",
        target=run_dir / "a.prt",
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


def test_request_validation_rejects_impossible_requests() -> None:
    """Bad requests must fail before any CAD process starts (report P2-2)."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(workdir=SCRATCH / "work-validate", outdir=SCRATCH / "out-validate")
    src = SCRATCH / "source.SLDPRT"
    src.write_bytes(b"placeholder")
    other_sldprt = SCRATCH / "other.SLDPRT"
    existing = SCRATCH / "already.prt"
    existing.write_bytes(b"x")
    a_step = SCRATCH / "a.step"
    a_step.write_bytes(b"ISO-10303-21;")
    b_step = SCRATCH / "b.step"

    cases = [
        ((src, src, "sldprt", "sldprt", "auto", False), "same file"),
        ((src, existing, "sldprt", "prt", "auto", False), "already exists"),
        ((src, other_sldprt, "sldprt", "sldprt", "auto", False), "both sldprt"),
        ((a_step, b_step, "step", "step", "auto", False), "both step"),
        ((a_step, Path("c.x_t"), "step", "parasolid", "auto", False), "both exchange formats"),
        ((src, Path("c.x_t"), "sldprt", "parasolid", "step", False), "cannot produce"),
        ((src, Path("c.prt"), "sldprt", "prt", "dwg", False), "unknown conversion mode"),
    ]
    for args, needle in cases:
        try:
            pipeline._validate_request(*args)
        except SwnxError as exc:
            assert needle in exc.message, (needle, exc.message)
        else:
            raise AssertionError(f"{args} should have been rejected with {needle!r}")


def test_report_is_copied_next_to_the_target() -> None:
    """The user must find validation.md beside the part, not only under work/ (P2-1)."""
    from swnx_switch.validate import compare

    SCRATCH.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(workdir=SCRATCH / "work-report", outdir=SCRATCH / "out-report")
    run_dir = SCRATCH / "run-report"
    run_dir.mkdir(parents=True, exist_ok=True)
    deliver = SCRATCH / "deliver"
    target = deliver / "switch_test.prt"

    reference = _fixture("ref_part.json")
    comparison = compare(reference, _fixture("candidate_identical.json"))
    result = ConversionResult(
        ok=True,
        source=SCRATCH / "source.SLDPRT",
        target=target,
        source_format="sldprt",
        target_format="prt",
        run_dir=run_dir,
        comparison=comparison,
        verdict=comparison["verdict"],
    )
    pipeline._write_report(result, task="unit-test")

    assert (deliver / "switch_test.validation.md").exists(), "Markdown copy missing beside target"
    assert (deliver / "switch_test.validation.json").exists(), "JSON copy missing beside target"
    assert result.paths["report_md_near"].endswith("switch_test.validation.md")
    assert Path(result.paths["report_md"]).exists()


def test_progress_callback_and_keep_app_options() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(workdir=SCRATCH / "work-progress", outdir=SCRATCH / "out-progress")
    events: list[tuple[str, dict]] = []
    pipeline._progress = lambda stage, info: events.append((stage, info))
    pipeline._emit("source_read", app="solidworks")
    pipeline._emit("export", exchange="parasolid", ok=True)
    assert [stage for stage, _ in events] == ["source_read", "export"]
    assert events[0][1] == {"app": "solidworks"}

    pipeline._progress = None
    pipeline._emit("ignored")
    assert len(events) == 2, "no callback means no work"

    assert pipeline._options() == {}
    pipeline._keep_app = True
    assert pipeline._options({"step_variant": "ap242"}) == {"step_variant": "ap242", "keep_app": True}


def test_probe_environment_is_shallow_by_default() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    env = __import__("swnx_switch.pipeline", fromlist=["probe_environment"]).probe_environment(deep=False)
    assert env["deep"] is False
    assert isinstance(env["backends"], list) and env["backends"]


def test_metadata_only_mismatch_is_warn_not_fail() -> None:
    """Density is material metadata: a mismatch must not fail an exact B-Rep.

    Real case (2026-11-14): a SolidWorks session exported density 1 instead of
    1000, so NX mass came out 1000x smaller while every geometry check passed.
    Reporting that as ``fail`` would have hidden a perfect geometry conversion.
    """
    from swnx_switch.validate import compare

    reference = _fixture("ref_part.json")
    candidate = _fixture("candidate_identical.json")
    candidate["model"]["mass_properties"]["mass_kg"] = 0.00046173637094563085
    candidate["model"]["mass_properties"]["density_kg_m3"] = 1.0
    comparison = compare(reference, candidate)
    assert comparison["verdict"] == "warn", comparison["verdict_basis"]
    checks = {check["id"]: check for check in comparison["checks"]}
    assert checks["volume"]["status"] == "pass"
    assert checks["mass"]["status"] == "fail"
    assert comparison["summary"]["failed_optional"]


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
