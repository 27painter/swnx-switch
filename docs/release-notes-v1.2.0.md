# swnx-switch v1.2.0

SolidWorks ⇄ NX part interchange with a neutral IR, tolerance validation and an audit-friendly report.
This release closes the two issues found in real-machine use on 2026-11-14.

## Fixed

- **`swnx_probe {deep:true}` no longer returns `invalid output: value is not lossless JSON`.**
  Root cause: the deep probe embeds raw NX driver JSON whose face axes contain `-0.0`. Negative zero is
  valid JSON *text* but the host rejects it as non-lossless, and the old `wire.dumps` "serialise first,
  sanitise on failure" fast path never ran. `wire.sanitize` now folds `-0.0` to `0.0`, turns integers
  beyond ±2^53 into decimal strings, and `wire.dumps` always sanitises.
  Reproduction before/after: [`docs/evidence/probe-lossless-scan.md`](docs/evidence/probe-lossless-scan.md).
- Conversion results expose the **on-disk file name** (`target_written`): SolidWorks may save `x.SLDPRT`
  when asked for `x.sldprt`, and a case-sensitive script previously failed for the wrong reason.
- A warning is emitted when the output directory already holds a **same-stem file with another CAD
  extension** (e.g. yesterday's `switch_test1.prt` beside today's `switch_test1.SLDPRT`).

## Added

- `swnx_job_status` stage entries carry **`delta_s`** (per-stage duration) plus `backend`/`exchange`
  labels, so the slow step is visible without arithmetic.
- `report_dir` parameter on the MCP convert tools (the CLI already had `--report-dir`).
- `wire.scan()` — one definition of the host's lossless-JSON rule, shared by tests and the evidence script.
- **Protocol-level regression test**: every tool response from a real MCP subprocess is scanned for
  NaN/Infinity/`-0.0`/out-of-range integers.

## Delivered earlier in the 0.2.0 line

Async jobs (`swnx_convert_start` + `swnx_job_status`), SolidWorks COM activation retry with 2/5/10 s
backoff and ROT polling, `--keep-app` instance reuse, `diagnose.md`, three-state `probe --deep`,
reports beside the target, millisecond request validation, and optional mass/density checks that
downgrade to `warn` instead of failing an exact geometry conversion.

## Verification

- 138 offline tests (`python scripts/run_tests.py`), no CAD required.
- Real machine: NX → SolidWorks 37.3 s, 14 pass / 1 informational warn / 0 fail; SLDPRT → PRT 35.4 s and
  25.6 s with instance reuse; `probe --deep` payload scans clean.
- Evidence: [`docs/evidence/`](docs/evidence/README.md), [`docs/reliability.md`](docs/reliability.md).

## Notes

- Requires locally licensed SolidWorks 2025 and/or NX 2406; no format reverse-engineering, no licence bypass.
- Cross-kernel exchange carries B-Rep geometry only: parametric feature trees are not preserved (reported
  as an informational `warn`, which is expected, not a failure).
- The version number was renumbered from the previous internal `0.2.1` to `1.2.0`; history is in
  [`CHANGELOG.md`](CHANGELOG.md).