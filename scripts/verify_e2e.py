"""End-to-end conversion on this machine, with evidence.

This is the script that produces the numbers quoted in the README.  It converts a
real SolidWorks part to NX, then the NX result back to SolidWorks, and writes
``my/evidence.json`` + per-run reports so a reader can check the claim instead of
trusting it.

    python scripts/verify_e2e.py
    python scripts/verify_e2e.py --sw-part <part.sldprt> --nx-part <part.prt>

Exit code 0 only when every requested direction produced a file whose tolerance
comparison is pass or warn.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from swnx_switch import ir  # noqa: E402
from swnx_switch.pipeline import Pipeline, probe_environment  # noqa: E402

DEFAULT_SW_PART = Path(r"D:\27painter\Documents\sw_job\build_final\叉形接头.SLDPRT")


def _convert(pipeline: Pipeline, source: Path, target: Path, *, mode: str, force: bool) -> dict:
    print(f"\n>>> {source.name} -> {target.name} (mode={mode})")
    started = time.time()
    result = pipeline.convert(source, target, mode=mode, force=force)
    entry = result.summary()
    entry["wall_seconds"] = round(time.time() - started, 1)
    print(f"    verdict={result.verdict} ok={result.ok} {entry['wall_seconds']}s")
    for attempt in result.attempts:
        print(
            f"    route {attempt.exchange}: "
            f"export={'ok' if attempt.export_ok else 'FAIL'} "
            f"import={'ok' if attempt.import_ok else 'FAIL'} "
            f"({attempt.export_seconds:.0f}s + {attempt.import_seconds:.0f}s)"
        )
    if result.comparison:
        for check in result.comparison.get("checks", []):
            print(f"      {check.get('id'):<18} {check.get('status'):<12} {check.get('detail', '')}")
    elif result.error:
        print(f"    error: {result.error}")
    for warning in result.warnings[:6]:
        print(f"    warning: {warning}")
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sw-part", default=str(DEFAULT_SW_PART), help="SolidWorks 源零件")
    parser.add_argument("--nx-part", default=None, help="NX 源零件（默认用第一步的产物）")
    parser.add_argument("--out", default=str(REPO / "out" / "e2e"), help="产物目录")
    parser.add_argument("--mode", choices=["auto", "step", "parasolid"], default="auto")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的产物")
    parser.add_argument("--skip-roundtrip", action="store_true", help="只做 SW -> NX")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    evidence: dict = {
        "schema": "swnx.e2e/1",
        "started_at": stamp,
        "environment": probe_environment(),
        "steps": [],
        "ok": False,
    }

    env = evidence["environment"]
    print("环境 / environment")
    print(f"  NX         {(env.get('nx') or {}).get('root') or 'NOT FOUND'}")
    print(f"  SolidWorks {(env.get('solidworks') or {}).get('version') or 'NOT FOUND'}")
    for backend in env.get("backends", []):
        print(f"  backend {backend['backend']:<12} available={backend.get('available')}")
        if not backend.get("available"):
            print(f"    {backend.get('error') or ''}")

    pipeline = Pipeline(workdir=REPO / "work" / "e2e", outdir=out)
    sw_part = Path(args.sw_part)
    nx_target = out / f"{sw_part.stem}.prt"
    sw_back = out / f"{sw_part.stem}.roundtrip.SLDPRT"

    try:
        if not sw_part.exists():
            raise SystemExit(f"source part not found: {sw_part}")
        step_one = _convert(pipeline, sw_part, nx_target, mode=args.mode, force=args.force)
        evidence["steps"].append({"name": "solidworks_to_nx", **step_one})
        if not nx_target.exists():
            raise SystemExit("SW -> NX produced no .prt; stopping before the round trip")

        nx_source = Path(args.nx_part) if args.nx_part else nx_target
        if not args.skip_roundtrip:
            step_two = _convert(pipeline, nx_source, sw_back, mode=args.mode, force=args.force)
            evidence["steps"].append({"name": "nx_to_solidworks", **step_two})
    finally:
        evidence["finished_at"] = time.strftime("%Y%m%d-%H%M%S")

    verdicts = [step.get("verdict") for step in evidence["steps"]]
    evidence["ok"] = bool(evidence["steps"]) and all(v in ("pass", "warn") for v in verdicts)
    evidence_path = out / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n结论 verdicts: {verdicts}")
    print(f"证据 evidence: {evidence_path}")
    for step in evidence["steps"]:
        if step.get("report_md"):
            print(f"  报告 report: {step['report_md']}")
        if step.get("target"):
            target = Path(step["target"])
            size = target.stat().st_size if target.exists() else 0
            print(f"  产物 target: {target} ({size:,} bytes)")
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
