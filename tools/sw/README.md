# `tools/sw` — SolidWorks COM driver

`driver.py` is the only process that touches SolidWorks: the converter
(`swnx_switch.backends.sw.SolidWorksBackend`) spawns it once per job with the
host interpreter, talks to it over JSON files, and never loads `pywin32` itself.
A locked-up or crashed SolidWorks therefore costs one driver process, not the
converter.

## Wire protocol

The caller (`Backend.run` in `src/swnx_switch/backends/base.py`) writes
`<workdir>/request-<kind>-<uuid>.json` and sets:

| variable | meaning |
| --- | --- |
| `SWNX_REQUEST` | absolute path of the request JSON |
| `SWNX_RESULT` | absolute path where the result JSON **must** be written |
| `SWNX_LOG` | log file (the process stdout/stderr are redirected to it as well) |
| `SWNX_WORKDIR` | job work directory |
| `SWNX_SW_EXE` | optional explicit `SLDWORKS.exe` (else registry / convention) |
| `SWNX_SW_VISIBLE` | `1` keeps the SolidWorks window visible (debugging) |

The driver always writes a result JSON, including on failure:

```json
{"ok": true, "kind": "read", "backend": "solidworks", "app": "SolidWorks 2025",
 "app_version": "33.5.0.53", "outputs": ["..."], "ir": {...}, "warnings": [],
 "error": null, "duration_s": 12.3, "driver_version": "0.1.0"}
```

## Job kinds

| kind | request fields | what the driver does |
| --- | --- | --- |
| `probe` | — | connects, reports revision/build numbers/executable, STEP+Parasolid translator files, the `swStepAP` preference, 3D Interconnect state, default templates, open documents |
| `read` | `input`, optional `output`, `options{full, max_features, max_sketches, max_digest_groups, connect_timeout}` | opens the document **read-only + silent**, writes the neutral IR (`volume/area/centroid/inertia` from `IModelDocExtension.CreateMassProperty`, `GetPartBox`, bodies + `surface.Identity()` face digests, feature tree with sub-features, sketch dims via the owning feature's display dimensions, custom properties, configurations, material) |
| `export` | `input`, `output`, `format` = `step`\|`parasolid`, `options{step_ap or step_variant}` | `SaveAs`, then verifies the output on disk (ISO-10303 header, `FILE_SCHEMA`, `ADVANCED_FACE`; for `.x_t` a non-empty `TRANSMIT FILE` header) |
| `import` | `input` (STEP/Parasolid), `output` (`.sldprt`), `options{collect_ir}` | Same route for both: `GetImportFileData` (or `GetImportFileData2`) + `LoadFile4(path,'r',import_data,errors)` with 3D Interconnect temporarily off; for Parasolid a `LoadFile4` with an empty dispatch variant is the documented fallback if no import-data object exists. `OpenDoc6` is deliberately **not** used on foreign files (it kills the COM server on 2025 SP05 in a headless session). Import verifies `bodies > 0` and mass-property volume > 0 before saving the native part |

All SolidWorks lengths are **metres**; the driver converts to the IR's
millimetres (`mm`, `mm2`, `mm3` helpers at the top of the file).

## Behaviour that matters in batch

* Documents are opened `swOpenDocOptions_Silent | swOpenDocOptions_ReadOnly`,
  so a batch run cannot raise a rebuild/reference dialog (read-only also means
  the input is never modified).
* A pre-existing SolidWorks session is attached via the COM ROT and **never
  quit**; only the documents this job opened are closed. A job that starts
  SolidWorks itself calls `CloseAllDocuments` + `ExitApp()` and, if the process
  is still alive 10 s later, terminates **its own** instance with
  `taskkill /PID <pid> /T /F` (an attached user session is never killed).
* If `CoCreateInstance` is refused (`CO_E_SERVER_EXEC_FAILURE`, e.g. a
  restricted token/sandbox) the driver starts `SLDWORKS.exe` directly and waits
  for it to register in the ROT before giving up after `connect_timeout`.
* The `swStepAP` user preference is restored after an export; templates are only
  written when they were empty, and only so a STEP import cannot pop the
  template-selection dialog.
* COM is called serially through pywin32 *dynamic* dispatch (a makepy proxy
  mis-marshals the `[out] long` parameters of `OpenDoc6`/`LoadFile4`).  The
  `member()` helper distinguishes property values from zero-argument methods:
  pywin32 resolves property reads at `getattr` time, and an object-valued
  property comes back as a callable `CDispatch`, so blindly calling it invokes
  the default member (`DISP_E_MEMBERNOTFOUND`).

## Running it manually

```powershell
$env:SWNX_REQUEST  = "D:\...\work\sw\request.json"
$env:SWNX_RESULT   = "D:\...\work\sw\result.json"
$env:SWNX_LOG      = "D:\...\work\sw\driver.log"
python tools\sw\driver.py            # or: python tools\sw\driver.py --request <file>
```

Acceptance evidence (probe → read → STEP/Parasolid export → STEP re-import →
read-back) is produced by `work\sw\acceptance.py`; on a normal desktop:

```powershell
python work\sw\acceptance.py
```

The DSH workspace file sandbox blocks the out-of-process COM launch, so the
acceptance run must be executed outside the sandbox (see `work/sw/run_elevated.bat`).

## Offline verification

```powershell
$env:PYTHONPATH = "<repo>\.tools"
python -m pytest tests\test_sw_backend.py -q          # 21 passed, 1 skipped (live)
$env:SWNX_LIVE = "1"
python -m pytest tests\test_sw_backend.py -q -k live  # real read of 叉形接头.SLDPRT
python scripts\run_tests.py test_sw_backend.py         # same tests without pytest
```

API-name facts verified offline against the installed type libraries live in
`work/sw/verify_api_names.txt`, `work/sw/introspect_consts.txt` (swconst enums)
and `work/sw/list_interface_members.txt`.
