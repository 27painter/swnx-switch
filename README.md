# swnx-switch

**[中文](README.zh.md) | English**

**SolidWorks `.sldprt` ⇄ Siemens NX `.prt` interoperability: read a part into a structure an AI can reason about, then convert between the two native formats with tolerance-verified validation.**


The project drives the installed **SolidWorks 2025** and **Siemens NX 2406** through their own
official APIs (COM / NXOpen), reads a part into a neutral JSON model, exchanges it through
**Parasolid `.x_t` (preferred) or STEP (fallback)**, and **compares source vs. result after every
conversion, returning a `pass` / `warn` / `fail` verdict**.

> **Scope statement.** This project does not parse the closed `.sldprt` / `.prt` binary formats and
> does no feature reverse-engineering. It exchanges **B-Rep geometry** (analytic surfaces, not
> meshes), so **the target receives a dumb solid / imported body**: parametric features, sketches and
> modelling history are not preserved. That is the industry ceiling for STEP/Parasolid exchange; see
> [docs/limitations.md](docs/limitations.md).

---

## Table of contents

1. [What it is / what it is not](#1-what-it-is--what-it-is-not)
2. [Requirements](#2-requirements)
3. [Install and quick start](#3-install-and-quick-start)
4. [CLI reference](#4-cli-reference)
5. [AI integration: MCP and skill](#5-ai-integration-mcp-and-skill)
6. [Data model (IR)](#6-data-model-ir)
7. [Conversion routes and rationale](#7-conversion-routes-and-rationale)
8. [Validation](#8-validation)
9. [Measured results](#9-measured-results)
10. [Known limitations](#10-known-limitations)
11. [Troubleshooting](#11-troubleshooting)
12. [Architecture and layout](#12-architecture-and-layout)
13. [Development and testing](#13-development-and-testing)
14. [Community research summary](#14-community-research-summary)
15. [Version and roadmap](#15-version-and-roadmap)
16. [License and compliance](#16-license-and-compliance)
17. [References](#17-references)

## 1. What it is / what it is not

| It can | It cannot |
|---|---|
| Read mass properties, bounding box, bodies, feature tree, sketches, surface digest and custom properties from `.sldprt` / `.prt` | Preserve the parametric feature tree, sketches or dimension relations (the target gets an imported body) |
| Convert `.sldprt` ⇄ `.prt` with geometry agreeing to ~1e-9 | Translate features (e.g. rebuild an Extrude as an Extrude) |
| Import/export `.step` / `.x_t` and turn them into native parts | Convert assemblies semantically; carry full drawing annotations/PMI |
| Run 15 tolerance checks and produce a bilingual report after every conversion | Parse native binary formats, bypass CAD licences, or work without the apps installed |
| Expose 8 tools over MCP to any AI client (including async jobs) | Act as a GUI modeller (it ships a double-click menu, not a CAD UI) |

**Design trade-off**: instead of guessing features (unreliable and unverifiable), move exact B-Rep and
report honestly what was lost.

## 2. Requirements

| Item | Requirement | Verified machine |
|---|---|---|
| OS | Windows 10/11 x64 | Windows 11 x64 |
| Python | 3.10+ (host) | 3.12.10 |
| pywin32 | Required by the SolidWorks driver | installed |
| SolidWorks | 2020+ with COM automation; tested on 2025 | 2025 SP05 (33.5.0.53) |
| Siemens NX | 12 / 2406 / 2506 (batch `run_journal.exe`); tested on 2406 | 2406 (`D:\UG NX2406`) |
| Licence | A working SW / NX licence on the machine | verified |
| Disk | 1–30 MB of temp files per conversion; `work/` is disposable | — |

No third-party CAD library is required on the Python side (no OCCT / pythonocc / FreeCAD).
`pytest` is optional: `scripts/run_tests.py` runs all 138 tests with plain Python.

## 3. Install and quick start

### Option A: run from the checkout (recommended)

```bat
git clone <this repo> swnx-switch
cd swnx-switch
swnx-switch.cmd probe
```

### Option B: install a global command

```bat
python -m pip install -e ".[windows]"
swnx-switch probe          :: available from any directory
```

### Option C: DSH plugin (hands the tools to an agent)

Follow [plugin/README.md](plugin/README.md): link `plugin/` into the DSH profile dependencies and
bundles, run `pnpm install`, restart DSH, and the agent can call `swnx_convert_part` and the other
five tools.

### Option D: skill (teaches the AI when to use it and how to read verdicts)

[skills/swnx-switch/SKILL.md](skills/swnx-switch/SKILL.md) is the operating manual (preflight,
workflow, verdict interpretation, known traps, failure handling, reporting template). Copy it into
the skill directory to have DSH load it on demand:

```bat
copy /Y skills\swnx-switch\SKILL.md D:\cad-agents\dsh-skills\swnx-switch\SKILL.md
```

### First run: double-click the menu

Double-click **`swnx-switch-menu.cmd`** in the repo root (a desktop shortcut named
"swnx-switch 转换器" points at it). The Chinese menu offers:

```
1 environment probe       2 read a part -> IR + brief
3 convert a part          4 compare two parts
5 view an existing brief  6 open the output folder        0 exit
```

Files are chosen through native Windows file dialogs and the console stays open afterwards.
(Double-clicking `swnx-switch.cmd` itself only prints usage and exits with code 2 — it is a
command-line entry point, not a double-click application.)

### Command line examples

```bat
:: environment probe
swnx-switch.cmd probe

:: read a SolidWorks part -> IR + brief (~8 s)
swnx-switch.cmd read "D:\path\part.SLDPRT" --out out\part.ir.json
swnx-switch.cmd brief out\part.ir.json

:: SolidWorks -> NX (34 s measured; validates and writes the report automatically)
swnx-switch.cmd convert "D:\path\part.SLDPRT" "D:\path\part.prt"

:: NX -> SolidWorks
swnx-switch.cmd convert "D:\path\part.prt" "D:\path\part.back.SLDPRT"
```

## 4. CLI reference

| Command | Purpose | Key options |
|---|---|---|
| `probe` | Inspect the environment and both backends | `--json`, `--deep` (really starts/attaches once, 5–20 s) |
| `read <file>` | Read a part into IR JSON | `--out`, `--via nx\|solidworks`, `--keep-app`, `--json` |
| `brief <ir.json>` | Render the IR as a part brief | `--out` |
| `convert <src> <dst>` | Convert, validate, report | `--mode`, `--step-variant`, `--force`, `--tolerance`, `--no-report`, `--report-dir`, `--keep-app`, `--preflight`, `--drop-intermediate`, `--json` |
| `compare <ref> <cand>` | Compare two IR docs or two native files | `--tolerance`, `--json` |
| `routes` | List supported conversion routes | `--json` |
| `mcp` | Run the MCP stdio server | `--workdir`, `--outdir` |

`convert` now validates the request **before** starting any CAD (source == target, same format,
target exists, `--mode` contradicts the target extension) and fails in milliseconds (0.2.0).

**Exit codes**: `0` success (or verdict `pass`/`warn`), `1` conversion/validation failure,
`2` environment error (CAD not found, dependency missing).

**Environment variables**:

| Variable | Purpose | Default |
|---|---|---|
| `NX_HOME` / `UGII_BASE_DIR` | NX install root | probed from the registry |
| `SWNX_SLDPRT_EXE` / `SOLIDWORKS_EXE` | SLDWORKS.exe path | probed from the registry |
| `SWNX_TIMEOUT` | Per-CAD-job timeout (seconds) | 900 |
| `SWNX_NX_JOURNAL` | Override the NX journal | `tools/nx/driver.py` |
| `SWNX_WORKDIR` / `SWNX_OUTDIR` | Run/output directories | `<repo>/work`, `<repo>/out` |
| `SWNX_LIVE=1` | Enable tests that need the real CAD apps | off |
| `SWNX_KEEP_APP=1` | Keep the CAD app running after a job for reuse | off |
| `SWNX_SW_ATTACH_TIMEOUT` | Seconds to wait for SolidWorks to enter the COM ROT after launch | 90 |
| `SWNX_SWITCH_HOME` / `SWNX_PYTHON` | Plugin: project root / interpreter | auto |
| `SWNX_ENABLE_MCP=0` | Load the plugin without registering tools | on |

## 5. AI integration: MCP and skill

MCP is the interface, the plugin is the delivery mechanism, the skill is the manual — each works
independently:

```bat
:: any MCP client (Claude Desktop / Codex / Cursor)
python -m swnx_switch.ai.mcp_server
```

| Tool | Arguments | Returns | Runtime |
|---|---|---|---|
| `swnx_probe` | `deep?` | environment + backend state; with `deep=true` also launchable/reachable/last_error | <1 s / 5–20 s |
| `swnx_read_part` | `path`, `via?`, `save_ir?`, `brief?`, `keep_app?` | summary + IR path + brief (no longer echoes the whole IR) | 8–60 s |
| `swnx_convert_part` | `source`, `target`, `mode?`, `step_variant?`, `force?`, `report?`, `tolerance?`, `keep_app?`, `preflight?`, `report_dir?` | verdict + key checks + target sha256 + written path + report path | 1–5 min |
| `swnx_convert_start` | same as above | returns a `job_id` immediately; runs in the background | milliseconds |
| `swnx_job_status` | `job_id` | state / stage / elapsed_s / per-stage `delta_s` with backend label / slim result | milliseconds |
| `swnx_compare_parts` | `reference`, `candidate`, `tolerance?` | comparison result | seconds |
| `swnx_part_brief` | `ir` | Markdown brief | <1 s |
| `swnx_list_routes` | — | route table | <1 s |

Typical conversation (with the DSH plugin installed):

> Convert `D:\...\fork_joint.SLDPRT` to NX `.prt` and tell me whether the geometry changed.

The agent calls `swnx_probe` → (optionally `swnx_read_part`) → `swnx_convert_start` →
`swnx_job_status` polling, then reports the verdict, the volume/bounding-box deviation, the report
path, and the fact that parametric features were not preserved.

## 6. Data model (IR)

`swnx.ir/1`, plain JSON, millimetres everywhere (SolidWorks uses metres; the driver multiplies by
1000, NX follows the part units). Real values from an actual `read` of the verified part, with
fields elided for brevity:

```jsonc
{
  "schema": "swnx.ir/1",
  "source": {
    "path": "D:\\...\\fork_joint.SLDPRT", "format": "sldprt", "size_bytes": 194118,
    "sha256": "9e2390bf…", "app": "SolidWorks 2025", "app_version": "33.5.0.53", "readonly": true
  },
  "model": {
    "name": "fork joint", "kind": "part", "units": "mm",
    "material": {},
    "mass_properties": {
      "volume_mm3": 461736.370946, "surface_area_mm2": 87883.276607,
      "mass_kg": 0.461736, "density_kg_m3": 1000.0,
      "centroid_mm": [26.304407, -0.193964, 0.0]
    },
    "bounding_box": { "min_mm": [-55, -40, -55], "max_mm": [133.5, 40, 55], "size_mm": [188.5, 80, 110] },
    "bodies": [{ "name": "切割-实体5", "type": "solid", "faces": 25, "edges": 45, "vertices": 20,
                 "volume_mm3": 461736.370946, "area_mm2": 87883.276607 }],
    "features": [{ "name": "Extrude1", "type": "Extrude", "suppressed": false }],   // 45 in this part, incl. sub-features/folders
    "sketches": [{ "name": "Sketch1", "plane": "Front Plane", "entities": 4 }],     // 8 in this part
    "geometry_digest": {
      "plane":    [{ "kind": "plane", "normal": [0, 0, 1], "area_mm2": 8304.7 }],   // 10
      "cylinder": [{ "kind": "cylinder", "radius_mm": 5, "axis": [0, 0, 1] }],      // 10: R5/R10/R20/R27.5/R30/R45/R55
      "torus":    [{ "kind": "torus", "area_mm2": 7397.9 }]                          // 5
    },
    "custom_properties": { "Material": "45 steel", "DrawingNo": "SW-NX-001" },      // 28 in this part
    "configurations": [{ "name": "Default", "active": true }]
  },
  "diagnostics": { "warnings": [], "limits": [] }
}
```

Field rules: `*_mm`, `*_mm2`, `*_mm3` carry their unit in the suffix; a value that could not be read
is **omitted**, with the reason recorded in `diagnostics.warnings` / `diagnostics.limits` — numbers
are never invented (e.g. NX does not expose moments of inertia, so the IR says so instead of
guessing).

`brief` renders the same IR as a Chinese part brief (key figures table, surface digest, body table,
feature tree, sketches, custom properties, conversion notes, warnings) that can be pasted straight
into a model context.

## 7. Conversion routes and rationale

```
source ──export──▶ .x_t / .step ──import──▶ target
   │                                          │
   └── read IR (source app API) ── compare (15 checks) ── read IR (target app API) ──┘
```

| Source | Target | Preferred | Fallback |
|---|---|---|---|
| `.sldprt` | `.prt` | SW exports Parasolid → NX `ufs.Ps.ImportData` → SaveAs | STEP (NX batch STEP is unavailable on this machine) |
| `.prt` | `.sldprt` | NX exports Parasolid → SW `LoadFile4` → SaveAs | STEP |
| native | `.step` / `.x_t` | source app exports directly | — |
| `.step` / `.x_t` | native | target app imports and saves | — |

**Why Parasolid first**: both applications are Parasolid-kernel, so `.x_t` moves B-Rep without
re-fitting surfaces — highest fidelity and **no STEP translator licence required**. The local NX 2406
batch STEP translator is unusable (export writes metadata only, import yields 0 bodies; see
[docs/limitations.md](docs/limitations.md) §3), so `--mode auto` routes through Parasolid whenever
NX is involved. `--mode step` forces STEP on a machine where the translator is licensed.

**Fallback policy**: `auto` tries the route table in order; an export with 0 solids or an import with
0 bodies fails that route and the next one is tried. If all fail the verdict is `fail`, with both
IRs, intermediate files and logs kept — never a silent downgrade to STL/mesh.

## 8. Validation

Every `convert` does the same thing: read source IR → export → import → read target IR → compare.
The two sides are **what the two CAD applications report**, not a text guess over the exchange file.

| Check | Default tolerance | Severity |
|---|---|---|
| IR schema / units | valid, mm | required |
| Body count | must match | required |
| Volume | rel 1e-3 (abs floor 1e-3 mm³) | required |
| Surface area | rel 2e-3 | required |
| Bounding box, per axis / corners | 0.05 mm / 0.1 mm (rel 1e-3) | required |
| Centroid | 0.05 mm | required (when both present) |
| Mass / density consistency | rel 1e-3 / 1e-6 | required (when material is present) |
| Face/edge counts | informational | optional |
| Feature-tree delta | informational | optional |

**Verdict semantics**: `pass` (all required checks pass) / `warn` (only optional/informational
anomalies, e.g. feature-tree loss; a **metadata** mismatch such as material density also lands here,
because density is not shape and every required geometry check still passes) / `fail` (any
**required** check fails **or cannot be confirmed**). `unavailable` is not a pass: a required check
without data fails, deliberately — silence must not look like agreement.

Since 0.2.0, `mass`/`density` are optional: their failure downgrades the verdict to `warn` with the
basis "metadata mismatch (geometry unaffected)". This came from a real conversion where the
SolidWorks export carried density 1 instead of 1000 while all 14 geometry checks passed.

Tolerances are overridable: `--tolerance volume=2e-3 --tolerance bbox_abs=0.1`. Short names map to
canonical keys (`volume_rel`, `area_rel`, `bbox_abs_mm`, `bbox_rel`, `bbox_corner_abs_mm`,
`centroid_abs_mm`, `mass_rel`, `density_rel`).

Reports: `work/.../validation.md` (Chinese table: check / actual / expected / delta / tolerance /
verdict) plus `report.json` for machines.

## 9. Measured results

Environment: SolidWorks 2025 SP05 (33.5.0.53) × NX 2406 (`D:\UG NX2406`) × Python 3.12.10 + pywin32.
Test part: the fork joint — 1 body / 25 faces / 45 features / 8 sketches; volume 461,736.370946 mm³;
bounding box 188.5 × 80 × 110 mm.

**SolidWorks → NX** (`scripts/verify_e2e.py --mode parasolid`):

| Check | Actual | Expected | Delta | Verdict |
|---|---|---|---|---|
| Bodies | 1 | 1 | 0 | pass |
| Volume mm³ | 461,736.3709456 | 461,736.370946 | −3.7e−07 | pass |
| Surface area mm² | 87,883.2766 | 87,883.2766 | −4.7e−07 | pass |
| Bounding box mm | 188.5 × 80 × 110 | same | 0 | pass |
| Centroid mm | 26.3044, −0.1940, 0.0 | same | 9.9e−08 | pass |
| Mass kg | 0.461736 | 0.461736 | 0 | pass |
| Faces/edges | F25 / E45 | F25 / E45 | 0 | pass |
| Feature tree | 2 (imported body) | 45 (parametric) | −43 | warn (expected) |

Runtime **34.2 s** (SW export 7 s + NX import 10 s + reads and validation); output
`out/e2e/fork_joint.prt` (106,616 B).

**NX → SolidWorks** (the return leg of the same script): volume delta +3.7e−07 mm³, area
+4.7e−07 mm², bounding box / centroid / faces / edges all pass; **35.2 s**; output
`out/e2e/fork_joint.roundtrip.SLDPRT` (160,391 B).

Raw evidence (auditable and reproducible): [docs/evidence/README.md](docs/evidence/README.md) —
full `e2e-evidence.json`, both validation reports, and each driver's acceptance record.

### Reliability work in 0.2.0 (from a real user report)

A caller ran two conversions back to back on a real machine and reported three P0s plus several
P1/P2s. 0.2.0 addresses each; the full phenomenon → root cause → fix → verification mapping is in
[docs/reliability.md](docs/reliability.md). Summary:

| Problem | How 0.2.0 handles it |
|---|---|
| Conversion succeeded but the tool returned `invalid output: value is not lossless JSON` | every exit goes through [wire.py](src/swnx_switch/ai/wire.py): `allow_nan=False` plus non-finite → `null`; `data` carries verdict/key checks/sha256/report path instead of the full comparison |
| The second conversion silently waited 429 s and failed | COM activation retries with 2/5/10 s backoff, then ROT polling every 2 s (90 s default, `SWNX_SW_ATTACH_TIMEOUT`); failure hints include the log tail and decoded error codes |
| SolidWorks would not start after repeated COM start/stop | `--keep-app` / `keep_app=true` reuses the running instance; after exiting, wait for the process to disappear and settle 1.5 s; write `diagnose.md` on connect failure |
| Long conversion had no progress | `swnx_convert_start` + `swnx_job_status` (and CLI stage lines on stderr): `source_read → export → import → target_read → validate → report` |
| `probe`'s `available` was not honest | split into `installed` / `launchable` / `reachable` / `last_error`; `probe --deep` actually starts/attaches once |
| Overweight return values | `data` minimised; full IR only at `ir_path`, full comparison only in the report file |
| Report buried under `work/` | a `<target>.validation.md` / `.json` copy is written beside the produced part and returned as an absolute path |
| Extension/mode conflicts discovered too late | millisecond-level request validation before any CAD starts |
| Expected difference not up front | first line of every conversion result is "geometry 1:1, parametric features not preserved" |

## 10. Known limitations

1. **No parametric features**: the target is a dumb solid — no sketches, parameters or parent/child
   relations (industry ceiling).
2. **Assemblies / drawings / PMI are pilot**: readable and exportable, but semantic conversion is
   `warn`-only; PMI, tolerances and 2D annotations are not promised.
3. **Face/edge IDs change**: downstream scripts that select `Face37` must re-select.
4. **Requires locally licensed SolidWorks / NX**: no format reverse-engineering, no licence bypass,
   no "works without the apps installed".
5. **Slow**: 30 s – 2 min per conversion (NX batch and SW COM start-up dominate); jobs queue per backend.
6. **Large assemblies unverified**: >1000 bodies is outside this release's scope.

Full list and the verified/unverified table: [docs/limitations.md](docs/limitations.md).

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `environment error` / backend unavailable | NX/SW not found or pywin32 missing | set `NX_HOME` / `SWNX_SLDPRT_EXE`; `pip install pywin32` |
| `pywintypes.com_error -2146959355 CO_E_SERVER_EXEC_FAILURE` | the DSH sandbox blocks out-of-process COM launch | run with `danger-full-access`, or run the CLI in a normal terminal |
| NX log line `failed to initialize UFUN 948792` | NX cannot read its licence/user profile dirs | same as above |
| `no exchange route succeeded` | both routes failed | read the per-attempt export/import logs |
| `STEP import produced 0 bodies` | local NX STEP translator unusable | use `--mode parasolid` (auto already prefers it) |
| `target already exists` | destination exists | add `--force` |
| Conversion succeeds with `feature_tree: warn` | parametric features are not preserved | expected, not a failure |
| Volume/bbox deviation >1e-3 | lost body, unit error, or a different model | read the report; check the source sha256 |
| The second conversion hung for ~7 minutes before `CO_E_SERVER_EXEC_FAILURE` | old behaviour: reactivating SW right after exit, with a single 420 s wait | 0.2.0 retries with backoff and polls the ROT for 90 s; see `diagnose.md` in that job's directory if it still fails |
| `SLDWORKS.exe` exits immediately after several conversions | repeated COM start/stop racing the single-instance/licence teardown | pass `--keep-app` (or `keep_app=true`) to reuse one instance; start SW manually once if needed |
| `probe` says available but SW cannot start | the old `available` only proved exe/ProgID existed | run `probe --deep`: it reports launchable/reachable/last_error |
| `probe --deep` fails with `value is not lossless JSON` | 0.2.0 sanitised NaN but not `-0.0`, which NX face axes contain | fixed in 0.2.1: `-0.0` -> `0.0`, integers beyond 2^53 -> strings, `wire.scan()` guards it |

**Three driver traps (already worked around — do not undo)**:
SolidWorks must not `OpenDoc6` a foreign `.prt`/`.x_t` (it kills the COM server; use
`GetImportFileData` + `LoadFile4`); NX batch `DexManager.CreateParasolidImporter().Commit()` is a
silent no-op (use `ufs.Ps.ImportData`); the NX batch STEP translator is unusable on this machine.

## 12. Architecture and layout

```
CLI / MCP / DSH plugin / skill
        │
   pipeline.py   orchestration: read -> export -> import -> read back -> validate -> report
   validate.py   15 tolerance checks (pure stdlib, no CAD needed)
   report.py     Chinese Markdown + JSON reports
   ir.py         neutral model, builders, schema validation
   jobs.py       background job table (convert_start / job_status)
   ai/wire.py    strict JSON at the tool boundary (no NaN/Infinity)
        │  Job(JSON) -> subprocess -> JobResult(JSON)
   backends/base.py   subprocess protocol, timeout, logging, cross-process lock per backend
   backends/nx.py  -> tools/nx/driver.py   NXOpen journal (NX embedded Python 3.10)
   backends/sw.py  -> tools/sw/driver.py   pywin32 COM (host Python 3.12)
```

| Path | Purpose |
|---|---|
| `src/swnx_switch/` | ir / pipeline / validate / report / cli / lock / paths / errors / jobs |
| `src/swnx_switch/ai/` | brief / tools (registry) / mcp_server (stdio) / wire (strict JSON) |
| `src/swnx_switch/backends/` | base (protocol + lock) / nx.py / sw.py |
| `tools/nx/driver.py` | NX batch journal (pure ASCII, 3.10-compatible) |
| `tools/sw/driver.py` | SolidWorks COM driver (separate process, always writes a result file) |
| `plugin/` | DSH plugin package (one MCP row; peer resolution for linked installs) |
| `skills/swnx-switch/` | operating manual for the agent |
| `scripts/` | `verify_e2e.py` / `run_tests.py` / `menu.ps1` |
| `tests/` | IR, MCP protocol, validator, strict-JSON and job tests (138 tests, no CAD needed) |
| `docs/` | architecture / feasibility / research / limitations / evidence |

Adding another CAD app: subclass `Backend` (`available` / `app_version` / `_command`), write a driver
that reads `SWNX_REQUEST` and writes `SWNX_RESULT`, register the format in `format_owner()` /
`BackendRegistry`, add a `--live` test.

## 13. Development and testing

```bat
:: full suite (no third-party dependencies, 138 tests)
python scripts\run_tests.py

:: when pytest is available
python -m pytest tests -q

:: real-machine end-to-end (starts SW and NX, ~70 s; needs danger-full-access inside DSH)
python scripts\verify_e2e.py --mode parasolid --force
```

Coverage: IR model, MCP protocol (a real subprocess driven over JSON-RPC), validator (fixtures for
pass/warn/fail/unavailable), both drivers' offline contracts (command line, environment, result
parsing), and `SWNX_LIVE=1` read tests on the real applications. The end-to-end script writes
environment, timings, file sizes and per-check results to `out/e2e/evidence.json`; the numbers in
this README come from there.

## 14. Community research summary

Full survey (248 lines, cited per claim): [docs/research-community.md](docs/research-community.md);
machine-specific decision memo: [docs/feasibility.md](docs/feasibility.md). Five findings shaped the
design:

1. **No open-source project reliably converts `.sldprt` ⇄ `.prt`.** Open-source work on `.sldprt`
   stops at container/metadata level (cadmpeg, openswx, sldkit) — no usable B-Rep and no writer.
2. **Commercial converters do not write native formats either.** CAD Exchanger lists both SOLIDWORKS
   and NX as read-only (R); writable formats are STEP/Parasolid/JT/ACIS. Datakit's mapping table has
   no feature/parameter/sketch layer.
3. **The vendors' own route is a neutral format.** Siemens help states outright that part features and
   parameters cannot be converted and the result contains parameterless bodies. SolidWorks 3D
   Interconnect can open an NX 2406 `.prt`, but extracts Parasolid information only — a read-only
   derived body, not a conversion.
4. **Parasolid is the shortest path between these two applications.** Both are Parasolid-kernel; the
   open-source stack (OCCT) can import but not export `.x_t`, so the file must come from the vendors'
   own exporters — which is exactly what this project does.
5. **The realistic ceiling of "AI reads CAD features".** Projects such as nxlib, solidworks-mcp and
   FreeCAD MCP all wrap the vendor API and emit structured JSON; ML work (BRepNet/UV-Net/DeepCAD)
   does B-Rep segmentation/generation, not editable feature recognition. Hence "official API + neutral
   IR" instead of feature guessing.

## 15. Version and roadmap

**0.2.1 (current)**: fixes the last `probe --deep` wire failure (sanitiser skipped `-0.0`), reports per-stage `delta_s`, the written file name and same-name siblings, and adds `report_dir`; 138 tests.\n\n**0.2.0**: reliability release on top of 0.1.0 — strict JSON tool boundary and slim
payloads, async jobs (`convert_start` + `job_status`), SolidWorks COM retry and ROT polling,
`--keep-app` instance reuse, `diagnose.md`, three-state `probe --deep`, reports beside the target,
request validation. 131 automated tests.

**Planned**: assembly BOM/instance mapping; `--mode step` verification on a machine with a
licensed STEP translator; read-only drawing view/annotation extraction; directory-level batch
conversion with incremental re-runs; job cancellation.

**Explicitly out of scope**: feature-level reconstruction (no verifiable method, and commercial tools
do not promise it); reverse-engineering native formats; licence bypass; treating STL/mesh as an
exchange format.

## 16. License and compliance

MIT (see [LICENSE](LICENSE)). The tool runs on locally licensed SolidWorks / NX installations, does
not bypass licensing, and does not reverse-engineer native file formats. Ownership and compliance of
converted artefacts remain with the user. NX batch sessions occupy one licence seat.

## 17. References

- Research with sources: [docs/research-community.md](docs/research-community.md)
- Machine-specific decision memo: [docs/feasibility.md](docs/feasibility.md)
- Architecture: [docs/architecture.md](docs/architecture.md) · Limitations: [docs/limitations.md](docs/limitations.md)
- Evidence: [docs/evidence/](docs/evidence/README.md)
- Similar approaches: nxlib (NXOpen wrapper), solidworks-mcp (COM → MCP), FreeCAD MCP Bridge,
  CAD Exchanger / Datakit (what commercial converters actually promise)
