#!/usr/bin/env python
"""Standalone pywin32 driver for SolidWorks 2025 (the *only* process that touches COM).

Wire protocol
-------------
``swnx_switch.backends.base.Backend.run()`` spawns this file with the host
interpreter, writes ``<workdir>/request-<kind>-<uuid>.json`` and exports::

    SWNX_REQUEST  absolute path of the request JSON
    SWNX_RESULT   absolute path the result JSON must be written to
    SWNX_LOG      absolute path of the process log (stdout is redirected there too)
    SWNX_WORKDIR  the job work directory
    SWNX_SW_EXE   optional explicit SLDWORKS.exe path

The request is ``{"kind": probe|read|export|import, "input", "output",
"format", "options": {...}}``.  This file **always** writes a result JSON, even on
failure, so the caller never has to parse a traceback out of stdout.

Job kinds
---------
* ``probe``   connect, report the SolidWorks build, COM/exchange capability and
              the preferences that affect STEP import/export (no file needed).
* ``read``    open a native SolidWorks document read-only + silent and write the
              neutral IR (``swnx_switch.ir``) with mass properties, bounding box,
              bodies and B-rep face digests, feature tree, sketches, custom
              properties and configurations.  All SolidWorks lengths are metres;
              this driver converts to millimetres.
* ``export``  open a native part and save it as STEP (AP242 when requested) or
              Parasolid ``.x_t``.  The output is verified on disk (size > 0,
              STEP schema header, ``ADVANCED_FACE`` present).
* ``import``  open a STEP / Parasolid file into SolidWorks, verify solid bodies
              exist and save the result as a native ``.sldprt``.

Deliberate rules
----------------
* One job == one process.  If no SolidWorks instance answers in the COM ROT the
  driver starts ``SLDWORKS.exe`` itself, waits for it to register, and calls
  ``ExitApp()`` on the way out.  An instance that was already running (a user's
  session) is never quit: only the documents this job opened are closed.
* Documents are opened read-only, silent, and never rebuilt in batch, so the
  driver cannot pop a rebuild/reference dialog and hang a pipeline.
* Every numeric value in the emitted IR is read from the API.  When an API call
  yields nothing the driver records ``diagnostics.warnings`` / ``limits``
  instead of inventing a value.
* COM calls are serial; pywin32 dynamic dispatch is used throughout because a
  makepy-generated proxy mis-marshals the ``[out] long`` parameters of
  ``OpenDoc6``/``LoadFile4``.

Run manually::

    python tools/sw/driver.py --request work/sw/request.json

(``SWNX_REQUEST``/``SWNX_RESULT``/``SWNX_LOG`` are normally set by the caller.)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # the repo is used from a source checkout, not installed
    sys.path.insert(0, str(_SRC))

from swnx_switch import ir  # noqa: E402  (path setup must run first)

DRIVER_VERSION = "0.1.0"

# --------------------------------------------------------------------------- #
# SolidWorks constants (verified against swconst.tlb on 2025 SP05, see
# work/sw/introspect_consts.txt; values are stable across releases)
# --------------------------------------------------------------------------- #
PROGID = "SldWorks.Application"

SW_DOC_TYPE = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}  # swDocumentTypes_e
SW_DOC_PART = 1
SW_OPEN_SILENT = 1  # swOpenDocOptions_e.swOpenDocOptions_Silent
SW_OPEN_READONLY = 2  # swOpenDocOptions_e.swOpenDocOptions_ReadOnly
SW_BODY_ALL = -1  # swBodyType_e.swAllBodies
SW_BODY_SOLID = 0  # swBodyType_e.swSolidBody
SW_EXPORT_PDF_DATA = 1
SW_SAVE_AS_SILENT = 1  # swSaveAsOptions_e.swSaveAsOptions_Silent
SW_SAVE_AS_CURRENT_VERSION = 0

# swUserPreferenceIntegerValue_e
SW_PREF_STEP_AP = 75  # swStepAP (the AP number itself: 203/214/242 on 2025 SP05)
SW_PREF_STEP_EXPORT = 142  # swStepExportPreference (tried when swStepAP is ignored)
# swUserPreferenceStringValue_e
SW_PREF_TEMPLATE = {8: ("part", "*.prtdot"), 9: ("assembly", "*.asmdot"), 10: ("drawing", "*.drwdot")}
# swUserPreferenceToggle_e
SW_TOGGLE_ALWAYS_USE_DEFAULT_TEMPLATES = 111
SW_TOGGLE_3D_INTERCONNECT = 691  # swMultiCAD_Enable3DInterconnect

# swUserPreferenceIntegerValue_e.swStepAP holds the *AP number itself* (203, 214,
# 242), not an enum index: on 2025 SP05 the preference reads back 203 by default
# and setting 2 leaves the exported FILE_SCHEMA at CONFIG_CONTROL_DESIGN (AP203).
# Verified from the acceptance run: work/sw/out/acceptance_summary.json.
STEP_AP_VALUES = {"ap203": 203, "ap214": 214, "ap242": 242}
STEP_AP_NAMES = {value: name for name, value in STEP_AP_VALUES.items()}

# swSurfaceTypes_e from swconst.tlb
SURFACE_KINDS = {
    4001: "plane",  # PLANE_TYPE
    4002: "cylinder",  # CYLINDER_TYPE
    4003: "cone",  # CONE_TYPE
    4004: "sphere",  # SPHERE_TYPE
    4005: "torus",  # TORUS_TYPE
    4006: "other",  # BSURF_TYPE
    4007: "other",  # BLEND_TYPE
    4008: "other",  # OFFSET_TYPE
    4009: "other",  # EXTRU_TYPE
    4010: "other",  # SREV_TYPE
}

# swDimensionType_e -> how to interpret IDimension.SystemValue (SI units)
DIMENSION_LINEAR = {2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15}
DIMENSION_ANGULAR = {3, 16}

HARD_FAILURE_TOKENS = ()  # the caller (base.HARD_FAILURES) does the judging

_MISSING = object()
_LOG_PATH = os.environ.get("SWNX_LOG")


# --------------------------------------------------------------------------- #
# errors / logging
# --------------------------------------------------------------------------- #
class DriverError(RuntimeError):
    """A job step failed with a message meant for the caller's result JSON."""

    def __init__(self, message: str, *, hint: str | None = None, detail: Any = None):
        super().__init__(message)
        self.hint = hint
        self.detail = detail


def log(message: str, level: str = "INFO") -> None:
    """Write one log line to SWNX_LOG; also print when stdout is a console.

    ``Backend.run`` redirects stdout+stderr to the same ``SWNX_LOG`` file, so
    writing through this helper and printing would duplicate every line.
    """
    line = f"{time.strftime('%H:%M:%S')} [{level}] {message}"
    wrote = False
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(line + "\n")
            wrote = True
        except OSError:
            pass
    if not wrote or sys.stdout.isatty():
        print(line, flush=True)


def _load_request() -> dict:
    path = os.environ.get("SWNX_REQUEST")
    if not path:
        raise DriverError("SWNX_REQUEST is not set; the driver is spawned by swnx-switch, see tools/sw/README.md")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DriverError(f"cannot read the request JSON: {path}", detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise DriverError(f"request JSON is invalid: {path}", detail=str(exc)) from exc


def _result_path() -> Path | None:
    value = os.environ.get("SWNX_RESULT")
    return Path(value) if value else None


def _write_result(payload: dict, path: Path | None = None) -> Path:
    target = path or _result_path()
    if target is None:
        raise DriverError("SWNX_RESULT is not set; cannot hand the result back to the caller")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _pywin32_method_wrapper_types() -> tuple:
    """Type of pywin32's late-bound *method* wrapper (property reads bypass it)."""
    try:
        from win32com.client.dynamic import _dynamic_Method_  # noqa: PLC0415

        return (_dynamic_Method_,)
    except Exception:  # noqa: BLE001 - only used to disambiguate property vs method
        return ()


def _is_com_object(value: Any) -> bool:
    """True for a COM object returned by a *property* (Extension, FirstFeature, ...)."""
    if not hasattr(value, "_oleobj_"):
        return False
    wrappers = _pywin32_method_wrapper_types()
    return not (wrappers and isinstance(value, wrappers))


def member(obj: Any, name: str, *args: Any, default: Any = _MISSING) -> Any:
    """Read a COM member that may be exposed as a property, a method, or both.

    pywin32 resolves property reads at ``getattr`` time and hands back the *value*;
    object-valued properties therefore come back as a ``CDispatch``, which is
    callable (it has a default member).  Calling such a value invokes the default
    member instead of the property and raises ``DISP_E_MEMBERNOTFOUND`` -- the
    bug that broke ``model.Extension``.  So:

    * explicit arguments -> it is a method, call it;
    * pywin32 method wrapper -> it is a zero-argument method, call it;
    * an object with ``_oleobj_`` that is not a method wrapper -> it is a
      property value (a COM object), return it as-is;
    * any other callable (a makepy bound method) -> call it;
    * everything else -> the property value, return it.

    ``default`` makes an absent member non-fatal; genuine COM failures still
    raise, so a broken call is never silently reported as ``None``.
    """
    try:
        value = getattr(obj, name)
    except AttributeError:
        if default is not _MISSING:
            return default
        raise
    if args:
        return value(*args)
    wrappers = _pywin32_method_wrapper_types()
    if wrappers and isinstance(value, wrappers):
        return value()
    if _is_com_object(value):
        return value
    if callable(value):
        return value()
    return value


def try_member(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
    """Best-effort member read used only for optional evidence."""
    try:
        return member(obj, name, *args, default=default)
    except Exception:  # noqa: BLE001 - optional evidence
        return default


def as_list(value: Any) -> list:
    """Normalise COM arrays/None/COM tuples into a plain list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mm_from_m(value: Any) -> float | None:
    raw = _f(value)
    return None if raw is None else round(raw * 1000.0, 6)


def mm2_from_m2(value: Any) -> float | None:
    raw = _f(value)
    return None if raw is None else round(raw * 1_000_000.0, 6)


def mm3_from_m3(value: Any) -> float | None:
    raw = _f(value)
    return None if raw is None else round(raw * 1_000_000_000.0, 6)


def _round_vec(values: Any, digits: int = 6) -> list[float] | None:
    if not isinstance(values, (list, tuple)) or len(values) < 3:
        return None
    return [round(float(value), digits) for value in values[:3]]


def _source_format(path: str | os.PathLike) -> str:
    suffix = Path(path).suffix.lower()
    return {".sldprt": "sldprt", ".sldasm": "sldasm", ".slddrw": "slddrw"}.get(suffix, suffix.lstrip(".") or "unknown")


def _sldworks_pids() -> set[int] | None:
    """PIDs of running SLDWORKS.exe processes, or ``None`` when it cannot be read.

    ``tasklist`` is the only listing mechanism that works both under a restricted
    token and in a normal desktop session; when it is blocked the caller falls
    back to "ownership by launch pid" and stays conservative.
    """
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stdout or ""
    if completed.returncode != 0 or "Access denied" in text or "拒绝访问" in text:
        return None
    pids: set[int] = set()
    for line in text.splitlines():
        cells = [cell.strip().strip('"') for cell in line.split('","')]
        if len(cells) >= 2 and cells[0].strip('"').lower() == "sldworks.exe":
            try:
                pids.add(int(cells[1].strip('"')))
            except ValueError:
                continue
    return pids


def _process_alive(pid: int) -> bool | None:
    """True/False when it can be told, ``None`` when the process state is unknown."""
    if not pid:
        return False
    try:
        import win32api  # noqa: PLC0415 - Windows-only, pywin32
        import win32con  # noqa: PLC0415
        import win32process  # noqa: PLC0415

        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception as exc:  # noqa: BLE001 - 87 == ERROR_INVALID_PARAMETER: no such pid
        text = str(exc).lower()
        if getattr(exc, "winerror", None) == 87 or "参数错误" in str(exc) or "invalid parameter" in text:
            return False
        return None
    try:
        return win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            win32api.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    """Wait until ``pid`` is gone, with ``tasklist`` as a second opinion.

    ``_process_alive`` can be inconclusive right after a process exits (the handle
    cannot always be opened), which previously reported "survived taskkill" for a
    process that was already gone.  The live PID list from ``tasklist`` decides.
    """
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        if _process_alive(pid) is False:
            return True
        pids = _sldworks_pids()
        if pids is not None and pid not in pids:
            return True
        time.sleep(0.5)
    return False


def _empty_dispatch_variant() -> Any:
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def _byref_int() -> Any:
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def _byref_bstr() -> Any:
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")


# --------------------------------------------------------------------------- #
# COM session
# --------------------------------------------------------------------------- #
class SolidWorksSession:
    """Owns the COM connection, the documents a job opened, and the app lifetime."""

    def __init__(self, options: dict | None = None):
        self.options = dict(options or {})
        self.sw: Any = None
        self.pid = 0
        self.owned = False
        self.attached = False
        #: documents this job opened (IModelDoc2 objects, resolved to a title at close time)
        self.opened: list[Any] = []
        self._launch_pid: int | None = None
        self._dispatch_created = False
        self._pythoncom: Any = None
        self.revision = ""
        self.build_numbers: list[int] = []
        self._restore: list[tuple[str, Any]] = []
        self._templates: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------- #
    def __enter__(self) -> "SolidWorksSession":
        import pythoncom  # noqa: PLC0415
        import win32com.client  # noqa: PLC0415

        self._pythoncom = pythoncom
        pythoncom.CoInitialize()
        before = _sldworks_pids()
        log(f"SolidWorks processes before connect: {sorted(before) if before is not None else 'unavailable'}")

        raw: Any = None
        try:
            raw = pythoncom.GetActiveObject(PROGID)
            self.attached = True
            log("attached to the running SolidWorks instance (COM ROT)")
        except Exception as exc:  # noqa: BLE001 - no instance is a normal case
            log(f"no reachable SolidWorks instance ({exc})")
            try:
                raw = win32com.client.DispatchEx(PROGID)
                self._dispatch_created = True
                log("created a new SolidWorks instance via DispatchEx")
            except Exception as dispatch_error:  # noqa: BLE001 - restricted token
                log(f"DispatchEx failed ({dispatch_error}); starting SLDWORKS.exe directly")
                raw, self._launch_pid, self.attached = self._launch(before)

        self.sw = _wrap_dynamic(raw)
        self.pid = int(_f(member(self.sw, "GetProcessID", default=0)) or 0)
        if self._launch_pid is not None:
            # The launcher is ours; if COM answered with a different pid it handed
            # us the pre-existing single instance, which must not be quit.
            self.owned = self.pid == self._launch_pid
        elif self._dispatch_created:
            # DispatchEx created the server; if it reported a pid that was already
            # running the server was single-instance and belongs to the user.
            self.owned = bool(before is None or (self.pid and self.pid not in before))
        else:
            self.owned = False
        self.revision = str(try_member(self.sw, "RevisionNumber", default="") or "")
        self.build_numbers = [int(value) for value in as_list(try_member(self.sw, "GetBuildNumbers2")) if isinstance(value, (int, float))]
        if not self.visible():
            try:
                self.sw.Visible = False
                log("SolidWorks window hidden (batch mode)")
            except Exception as exc:  # noqa: BLE001 - hiding is cosmetic
                log(f"could not hide the SolidWorks window: {exc}", "WARN")
        log(
            f"connected pid={self.pid} owned={self.owned} attached={self.attached} "
            f"revision={self.revision!r} build_numbers={self.build_numbers}"
        )
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        for name, args in reversed(self._restore):
            try:
                getattr(self.sw, name)(*args)
                log(f"restored preference via {name}{args}")
            except Exception as exc:  # noqa: BLE001 - restoring is best effort
                log(f"restoring preference {name}{args} failed: {exc}", "WARN")
        self._close_documents()
        if self.owned:
            self._quit_owned_instance()
        else:
            log("SolidWorks session was not started by this job; left open (only this job's documents were closed)")
        if self._pythoncom is not None:
            try:
                self._pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    def track(self, document: Any) -> None:
        """Remember a document this job opened so the session can close it."""
        if document is not None:
            self.opened.append(document)

    def _document_title(self, document: Any) -> str:
        if isinstance(document, str):
            return document
        return str(try_member(document, "GetTitle", default="") or "")

    def _close_documents(self) -> None:
        if self.owned:
            # This instance is ours: nothing open can be a user's document, so one
            # CloseAllDocuments is the reliable way to clear a stuck document that
            # made the per-title CloseDoc return False.
            try:
                if self.sw.CloseAllDocuments(True):
                    log("CloseAllDocuments(True) closed this job's documents")
                else:
                    log("CloseAllDocuments(True) returned False", "WARN")
            except Exception as exc:  # noqa: BLE001 - fall through to CloseDoc
                log(f"CloseAllDocuments failed: {exc}", "WARN")
        seen: set[str] = set()
        for document in self.opened:
            title = self._document_title(document)
            if not title or title in seen:
                continue
            seen.add(title)
            try:
                closed = bool(self.sw.CloseDoc(title))
            except Exception as exc:  # noqa: BLE001
                log(f"CloseDoc({title!r}) failed: {exc}", "WARN")
                continue
            if closed:
                log(f"closed document {title!r}")
            else:
                # Already closed by CloseAllDocuments, or a save prompt is pending;
                # only a remaining open document is a real problem (logged below).
                log(f"CloseDoc({title!r}) returned False (already closed)", "INFO")
        remaining = self._open_document_count()
        if remaining:
            log(f"{remaining} document(s) still open after cleanup", "WARN")
        elif remaining == 0:
            log("no documents left open")

    def _open_document_count(self) -> int | None:
        count = try_member(self.sw, "GetDocumentCount")
        if isinstance(count, (int, float)):
            return int(count)
        documents = try_member(self.sw, "GetDocuments")
        return len(as_list(documents)) if documents is not None else None

    def _quit_owned_instance(self) -> None:
        log(f"ExitApp on the SolidWorks instance this job started (pid {self.pid})")
        try:
            self.sw.ExitApp()
        except Exception as exc:  # noqa: BLE001 - fall through to the wait/kill below
            log(f"ExitApp raised: {exc}", "WARN")
        if _wait_for_process_exit(self.pid, 10.0):
            log("SolidWorks process exited after ExitApp")
            return
        # A batch-owned instance must not leak a licence seat or block the next job;
        # killing *our own* process is safe (an attached user session is never killed).
        log(f"SolidWorks pid {self.pid} still alive 10s after ExitApp; terminating this job's own instance", "WARN")
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"taskkill failed: {exc}", "WARN")
            return
        detail = (completed.stdout or completed.stderr or "").strip().splitlines()
        log(f"taskkill exit={completed.returncode} output={detail[:2]}")
        if _wait_for_process_exit(self.pid, 10.0):
            log("SolidWorks process terminated with taskkill")
            return
        log(f"SolidWorks pid {self.pid} still listed after taskkill", "WARN")

    def visible(self) -> bool:
        if "visible" in self.options:
            return bool(self.options["visible"])
        return os.environ.get("SWNX_SW_VISIBLE") == "1"

    # -- launch ------------------------------------------------------------ #
    def _launch(self, before: set[int] | None) -> tuple[Any, int | None, bool]:
        """Start SLDWORKS.exe and wait for it to register in the ROT.

        ``CoCreateInstance`` of the SolidWorks local server can be refused by a
        restricted token/AppContainer (CO_E_SERVER_EXEC_FAILURE) even though the
        executable runs fine, so the executable is started directly and the
        driver polls for its COM registration.
        """
        import win32com.client  # noqa: PLC0415

        exe = self._exe_path()
        log(f"starting {exe}")
        try:
            process = subprocess.Popen(
                [str(exe)],
                cwd=str(Path(exe).parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise DriverError(
                f"cannot start SolidWorks: {exe}",
                detail=str(exc),
                hint="set SWNX_SW_EXE to the full path of SLDWORKS.exe",
            ) from exc
        self._launch_pid = process.pid
        self._log_prelaunch_baseline(before)
        timeout = float(self.options.get("connect_timeout", 420.0))
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                raw = self._pythoncom.GetActiveObject(PROGID)
            except Exception as exc:  # noqa: BLE001 - not registered yet
                last_error = exc
                time.sleep(1.0)
                continue
            # GetActiveObject hands back a raw PyIDispatch: wrap it before reading
            # members (raw PyIDispatch has no pywin32 property/method semantics).
            wrapped = _wrap_dynamic(raw)
            pid = int(_f(try_member(wrapped, "GetProcessID", default=0)) or 0)
            log(f"SolidWorks registered in the ROT after {time.monotonic() - started:.1f}s (pid {pid})")
            return wrapped, process.pid, pid == process.pid
        raise DriverError(
            f"SolidWorks did not register in the COM ROT within {timeout:.0f}s (launcher pid {process.pid})",
            detail=str(last_error) if last_error else None,
            hint="check the SolidWorks licence/login dialogs, or start SolidWorks manually first",
        )

    def _log_prelaunch_baseline(self, before: set[int] | None) -> None:
        if before:
            log(f"pre-existing SolidWorks pids: {sorted(before)} (will not be quit)")

    def _exe_path(self) -> Path:
        explicit = (
            self.options.get("exe")
            or os.environ.get("SWNX_SW_EXE")
            or os.environ.get("SWNX_SLDPRT_EXE")
            or os.environ.get("SOLIDWORKS_EXE")
        )
        if explicit:
            return Path(explicit)
        try:
            from swnx_switch.paths import find_solidworks  # noqa: PLC0415

            install = find_solidworks()
            if install is not None:
                return install.exe
        except Exception:  # noqa: BLE001 - the explicit/convention paths below still work
            pass
        for candidate in (
            Path(r"D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe"),
            Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe"),
        ):
            if candidate.exists():
                return candidate
        raise DriverError(
            "SLDWORKS.exe not found",
            hint="set SWNX_SW_EXE to the full path of SLDWORKS.exe",
        )

    # -- app info ---------------------------------------------------------- #
    def app_version(self) -> str:
        return self.revision or "unknown"

    def app_year(self) -> int | None:
        """SolidWorks year from the major revision digit (33 -> 2025)."""
        try:
            major = int(self.revision.split(".")[0])
        except (ValueError, AttributeError):
            return None
        year = major - 8 + 2000
        return year if 2000 < year < 2100 else None

    def app_label(self) -> str:
        year = self.app_year()
        return f"SolidWorks {year}" if year else "SolidWorks"

    # -- documents --------------------------------------------------------- #
    def open_native(self, path: str | os.PathLike, *, read_only: bool = True) -> Any:
        """Open a native document read-only + silent; returns IModelDoc2."""
        target = str(Path(path).resolve())
        if not Path(target).exists():
            raise DriverError(f"input file does not exist: {target}")
        suffix = Path(target).suffix.lower()
        doc_type = SW_DOC_TYPE.get(suffix)
        if doc_type is None:
            raise DriverError(f"not a native SolidWorks document: {target}")
        existing = try_member(self.sw, "GetOpenDocumentByName", target)
        if existing is not None:
            log(f"reusing the document already open for {target}")
            return existing
        errors = _byref_int()
        warnings = _byref_int()
        options = SW_OPEN_SILENT | (SW_OPEN_READONLY if read_only else 0)
        log(f"OpenDoc6({target!r}, type={doc_type}, options={options} silent|readonly)")
        started = time.monotonic()
        try:
            model = self.sw.OpenDoc6(target, doc_type, options, "", errors, warnings)
            model, errors, warnings = _unpack_open_result(model, errors, warnings)
        except TypeError:
            model = self._dynamic().OpenDoc6(target, doc_type, options, "", errors, warnings)
            model, errors, warnings = _unpack_open_result(model, errors, warnings)
        if model is None:
            raise DriverError(
                f"SolidWorks could not open {target}",
                detail=f"OpenDoc6 error code {errors.value}, warning code {warnings.value} (swFileLoadError_e)",
            )
        log(f"opened {Path(target).name!r} in {time.monotonic() - started:.1f}s title={try_member(model, 'GetTitle')!r}")
        return model

    def load_foreign(self, path: str | os.PathLike) -> Any:
        """Import a STEP/Parasolid file as a new SolidWorks part; returns IModelDoc2."""
        target = str(Path(path).resolve())
        if not Path(target).exists():
            raise DriverError(f"input file does not exist: {target}")
        self.ensure_default_templates()
        import_data = try_member(self.sw, "GetImportFileData", target)
        if import_data is None:
            raise DriverError(
                f"GetImportFileData returned nothing for {target}",
                hint="the STEP/Parasolid translator may not be installed for this SolidWorks seat",
            )
        errors = _byref_int()
        interconnect = self._get_toggle(SW_TOGGLE_3D_INTERCONNECT)
        if interconnect:
            # 3D Interconnect keeps a link to the exchange file instead of building
            # a normal imported part; disable it for a deterministic import-to-part.
            self._set_toggle(SW_TOGGLE_3D_INTERCONNECT, False)
            log("3D Interconnect temporarily disabled for this import")
        try:
            log(f"LoadFile4({Path(target).name!r}, 'r')")
            started = time.monotonic()
            try:
                model = self.sw.LoadFile4(target, "r", import_data, errors)
            except TypeError:
                model = self._dynamic().LoadFile4(target, "r", import_data, errors)
            if isinstance(model, (tuple, list)):
                model = model[0] if model else None
            if model is None:
                raise DriverError(
                    f"SolidWorks could not import {target}",
                    detail=f"LoadFile4 error code {errors.value}",
                )
            log(f"imported {Path(target).name!r} in {time.monotonic() - started:.1f}s")
            return model
        finally:
            if interconnect:
                try:
                    self._set_toggle(SW_TOGGLE_3D_INTERCONNECT, True)
                    log("3D Interconnect re-enabled")
                except Exception as exc:  # noqa: BLE001 - restore best effort
                    log(f"re-enabling 3D Interconnect failed: {exc}", "WARN")

    def open_parasolid(self, path: str | os.PathLike) -> Any:
        """Import a Parasolid ``.x_t``/``.x_b`` with the *working STEP import pattern*.

        DO NOT "simplify" this by opening the file with ``OpenDoc6``:
        **OpenDoc6 on a foreign .prt/.x_t kills the SolidWorks COM server
        (-2147023170 RPC failed, then "RPC server unavailable" for every later
        call)** on SolidWorks 2025 SP05 in this headless session - reproduced on
        three fresh instances per file, Visible on and off.  ``GetImportFileData``
        -> ``LoadFile4(path, "r", import_data, errors)`` is the only working
        channel, and it is the same channel that imports STEP.

        The exact sequence proven by STEP imports is used with 3D Interconnect
        temporarily off.  ``GetImportFileData`` can legitimately return nothing for
        Parasolid; then ``GetImportFileData2``, then a blank part + retry, then the
        documented mesh/foreign import call (LoadFile4 with an EMPTY dispatch
        variant) is used - never OpenDoc6.
        """
        target = str(Path(path).resolve())
        if not Path(target).exists():
            raise DriverError(f"input file does not exist: {target}")
        self.ensure_default_templates()
        errors = _byref_int()
        interconnect = self._get_toggle(SW_TOGGLE_3D_INTERCONNECT)
        if interconnect:
            self._set_toggle(SW_TOGGLE_3D_INTERCONNECT, False)
            log("3D Interconnect temporarily disabled for this Parasolid import")
        started = time.monotonic()
        blank_title: str | None = None
        try:
            import_data = try_member(self.sw, "GetImportFileData", target)
            api = "GetImportFileData"
            if import_data is None:
                import_data = try_member(self.sw, "GetImportFileData2", target)
                api = "GetImportFileData2"
            if import_data is None:
                # Some builds only build the import options with an active part.
                blank_title = self._new_blank_part()
                if blank_title:
                    import_data = try_member(self.sw, "GetImportFileData", target)
                    api = "GetImportFileData (after NewDocument)"
            if import_data is None:
                log(
                    f"{Path(target).name}: no import-data object from GetImportFileData/2; "
                    "using LoadFile4 with an empty dispatch variant (foreign-file pattern)",
                    "WARN",
                )
                import_data = _empty_dispatch_variant()
                api = "LoadFile4(empty variant)"
            log(f"LoadFile4({Path(target).name!r}, 'r', {api})")
            try:
                model = self.sw.LoadFile4(target, "r", import_data, errors)
            except TypeError:
                model = self._dynamic().LoadFile4(target, "r", import_data, errors)
            if isinstance(model, (tuple, list)):
                model = model[0] if model else None
            if model is None:
                raise DriverError(
                    f"SolidWorks could not import the Parasolid file {target}",
                    detail=f"{api}; LoadFile4 error code {errors.value}",
                )
            log(f"imported Parasolid {Path(target).name!r} in {time.monotonic() - started:.1f}s via {api}")
            return model
        finally:
            if blank_title:
                try:
                    self.sw.CloseDoc(blank_title)
                    log(f"closed the temporary blank part {blank_title!r}")
                except Exception as exc:  # noqa: BLE001 - cosmetic
                    log(f"closing the temporary blank part failed: {exc}", "WARN")
            if interconnect:
                try:
                    self._set_toggle(SW_TOGGLE_3D_INTERCONNECT, True)
                    log("3D Interconnect re-enabled")
                except Exception as exc:  # noqa: BLE001 - restore best effort
                    log(f"re-enabling 3D Interconnect failed: {exc}", "WARN")

    def _new_blank_part(self) -> str | None:
        """Open a blank part (needed by some import-option paths); returns its title."""
        template = str(self._templates.get("part", "") or "")
        if not template:
            log("no part template resolved; cannot open a blank part for the import", "WARN")
            return None
        try:
            model = self.sw.NewDocument(template, 0, 0, 0)
        except Exception as exc:  # noqa: BLE001 - fall through without a blank part
            log(f"NewDocument failed before the import: {exc}", "WARN")
            return None
        if model is None:
            log("NewDocument returned nothing before the import", "WARN")
            return None
        self.track(model)
        title = str(try_member(model, "GetTitle", default="") or "")
        log(f"opened a temporary blank part {title!r} before the import")
        return title


    def mass_property_volume(self, model: Any) -> float | None:
        """Cheap volume in m^3 for an import sanity check."""
        extension = member(model, "Extension")
        mass_property = try_member(extension, "CreateMassProperty")
        if mass_property is None:
            return None
        return _f(try_member(mass_property, "Volume"))

    def save_as(self, model: Any, output: str | os.PathLike, *, silent: bool = True) -> tuple[bool, int, int]:
        target = str(Path(output).resolve())
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        errors = _byref_int()
        warnings = _byref_int()
        options = SW_SAVE_AS_SILENT if silent else 0
        log(f"SaveAs({target!r}, version={SW_SAVE_AS_CURRENT_VERSION}, options={options})")
        started = time.monotonic()
        try:
            ok = model.Extension.SaveAs(
                target, SW_SAVE_AS_CURRENT_VERSION, options, _empty_dispatch_variant(), errors, warnings
            )
        except TypeError:
            ok = self._dynamic().Extension.SaveAs(
                target, SW_SAVE_AS_CURRENT_VERSION, options, _empty_dispatch_variant(), errors, warnings
            )
        log(
            f"SaveAs finished in {time.monotonic() - started:.1f}s ok={bool(ok)} "
            f"error={errors.value} warning={warnings.value}"
        )
        return bool(ok), int(errors.value), int(warnings.value)

    def bodies(self, model: Any, doc: dict | None = None) -> tuple[list, str]:
        """Solid bodies of a part, with the API path that produced them."""
        raw = try_member(model, "GetBodies2", SW_BODY_ALL, False)
        path = "GetBodies2(swAllBodies, False)"
        bodies = as_list(raw)
        if not bodies:
            log("GetBodies2(swAllBodies) returned no bodies; retrying with swSolidBody")
            bodies = as_list(try_member(model, "GetBodies2", SW_BODY_SOLID, False))
            path = "GetBodies2(swSolidBody, False)"
        return bodies, path

    # -- preferences ------------------------------------------------------- #
    def ensure_default_templates(self) -> dict[str, str]:
        """Point the template preferences at real files so no template dialog opens."""
        resolved: dict[str, str] = {}
        for preference, (label, pattern) in SW_PREF_TEMPLATE.items():
            current = str(try_member(self.sw, "GetUserPreferenceStringValue", preference, default="") or "")
            if current and Path(current).exists():
                resolved[label] = current
                continue
            found = self._find_template(pattern)
            if found is None:
                raise DriverError(
                    f"no {label} template ({pattern}) was found; SolidWorks would show "
                    "a template dialog and block a batch import",
                    hint="set a default template once in SolidWorks, or provide options.template_part",
                )
            self.sw.SetUserPreferenceStringValue(preference, str(found))
            resolved[label] = str(found)
            log(f"set default {label} template to {found}")
        try:
            self.sw.SetUserPreferenceToggle(SW_TOGGLE_ALWAYS_USE_DEFAULT_TEMPLATES, True)
        except Exception as exc:  # noqa: BLE001 - cosmetic, the paths above are what matter
            log(f"could not set swAlwaysUseDefaultTemplates: {exc}", "WARN")
        self._templates = resolved
        return resolved

    def _find_template(self, pattern: str) -> Path | None:
        exe_root = self._exe_path().parent
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        app_data = Path(os.environ.get("APPDATA", ""))
        roots = [
            exe_root / "data" / "templates",
            exe_root / "templates",
            program_data / "SOLIDWORKS",
            program_data / "SolidWorks",
            app_data / "SOLIDWORKS",
            app_data / "SolidWorks",
        ]
        for root in roots:
            if not root.exists():
                continue
            matches = sorted(root.rglob(pattern))
            if matches:
                return matches[0]
        return None

    def _get_toggle(self, preference: int) -> bool | None:
        try:
            return bool(self.sw.GetUserPreferenceToggle(preference))
        except Exception:  # noqa: BLE001 - preference may not exist on old builds
            return None

    def _set_toggle(self, preference: int, value: bool) -> None:
        self.sw.SetUserPreferenceToggle(preference, bool(value))

    def step_ap(self) -> dict:
        current = try_member(self.sw, "GetUserPreferenceIntegerValue", SW_PREF_STEP_AP)
        return {
            "preference_id": SW_PREF_STEP_AP,
            "value": int(current) if current is not None else None,
            "schema": STEP_AP_NAMES.get(int(current), "unknown") if current is not None else None,
        }

    def _pref_int(self, preference: int) -> int | None:
        value = try_member(self.sw, "GetUserPreferenceIntegerValue", preference)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def export_step(self, model: Any, target: Path, requested: str) -> dict:
        """Export STEP once, validate the B-rep, and only retry when the file is *invalid*.

        AP242 is preferred, not required: on 2025 SP05 every AP preference tried so
        far yields AP203 (``CONFIG_CONTROL_DESIGN``), so an AP mismatch must never
        trigger a rewrite -- that only risks clobbering a good file.  A retry is
        made only when the produced file fails validation (missing ISO-10303 header
        or no ``ADVANCED_FACE``), and every attempt writes a *distinct* file so a
        failed attempt can never overwrite a file that already validated.  The
        winner is moved onto ``target`` exactly once at the end.
        """
        requested = (requested or "ap242").lower()
        if requested not in STEP_AP_VALUES:
            raise DriverError(f"unsupported STEP AP {requested!r}; choose one of {sorted(STEP_AP_VALUES)}")
        prior_ap = self._pref_int(SW_PREF_STEP_AP)
        prior_export = self._pref_int(SW_PREF_STEP_EXPORT)
        attempts: list[dict] = []
        temporaries: list[Path] = []
        winner: Path | None = None
        winner_attempt: dict | None = None

        def attempt_export(preference: str, preference_id: int, value: int, path: Path) -> tuple[dict, bool]:
            try:
                self.sw.SetUserPreferenceIntegerValue(preference_id, value)
            except Exception as exc:  # noqa: BLE001 - report, the export may still work
                log(f"setting {preference}({preference_id})={value} failed: {exc}", "WARN")
            readback = self._pref_int(preference_id)
            ok, errors, warnings = self.save_as(model, path)
            if not ok:
                raise DriverError(
                    f"SolidWorks SaveAs({path.name}) failed",
                    detail=f"error code {errors}, warning code {warnings}",
                )
            report, problem = _wait_step_valid(path)
            schema, ap = (report.get("schema"), (report.get("step_ap") or "").lower()) if report else (None, None)
            entry = {
                "preference": preference,
                "preference_id": preference_id,
                "value": value,
                "readback": readback,
                "path": str(path),
                "schema": schema,
                "step_ap": ap or None,
                "size_bytes": path.stat().st_size if path.exists() else None,
                "valid": report is not None,
                "validation_error": problem,
                "save_as_errors": errors,
                "save_as_warnings": warnings,
            }
            attempts.append(entry)
            log(
                f"STEP export attempt: {preference}={value} readback={readback} -> "
                f"FILE_SCHEMA={schema!r} AP={ap or 'unknown'} valid={report is not None}"
                + (f" problem={problem!r}" if problem else "")
            )
            return entry, report is not None

        try:
            # 1) documented preference, straight to the final path
            entry, valid = attempt_export(
                "swStepAP", SW_PREF_STEP_AP, STEP_AP_VALUES[requested], target
            )
            if valid:
                winner, winner_attempt = target, entry
            else:
                # 2) only an *invalid* export justifies other preferences; each
                #    attempt goes to its own file, first valid one wins.
                candidates = {"ap203": [0, 1, 2], "ap214": [1, 2, 0], "ap242": [2, 0, 1]}[requested]
                for index, candidate in enumerate(candidates, start=1):
                    temporary = target.with_name(f"{target.stem}.try{index}{target.suffix}")
                    temporaries.append(temporary)
                    entry, valid = attempt_export(
                        "swStepExportPreference", SW_PREF_STEP_EXPORT, candidate, temporary
                    )
                    if valid:
                        winner, winner_attempt = temporary, entry
                        break
        finally:
            # Never leave the user's STEP preferences changed by a batch export.
            for preference, prior in ((SW_PREF_STEP_AP, prior_ap), (SW_PREF_STEP_EXPORT, prior_export)):
                if prior is None:
                    continue
                try:
                    self.sw.SetUserPreferenceIntegerValue(preference, prior)
                except Exception as exc:  # noqa: BLE001
                    log(f"restoring STEP preference {preference} failed: {exc}", "WARN")

        for temporary in temporaries:
            if winner is None or temporary != winner:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        if winner is None or not winner.exists():
            raise DriverError(
                f"no valid STEP file could be exported for {target.name}",
                detail=f"{len(attempts)} attempts; last problem: {attempts[-1]['validation_error'] if attempts else 'none'}",
            )
        if winner != target:
            try:
                winner.replace(target)
                log(f"kept the valid STEP from {winner_attempt['preference']}={winner_attempt['value']} as {target.name}")
            finally:
                winner.unlink(missing_ok=True)

        achieved = winner_attempt.get("step_ap") if winner_attempt else None
        matched = achieved == requested
        result = {
            "requested": requested,
            "achieved": achieved,
            "matched": matched,
            "schema": winner_attempt.get("schema") if winner_attempt else None,
            "validated": True,
            "prior": {"swStepAP": prior_ap, "swStepExportPreference": prior_export},
            "attempts": attempts,
        }
        if matched:
            log(f"STEP AP verified: requested={requested} schema={result['schema']!r}")
        else:
            log(
                f"requested STEP {requested} but the installed exporter produced {achieved} "
                f"(FILE_SCHEMA={result['schema']!r}); reporting the real AP instead of the request",
                "WARN",
            )
        return result

    def _dynamic(self) -> Any:
        """pywin32 dynamic wrapper for calls where a typed proxy mangles [out] args."""
        from win32com.client import dynamic  # noqa: PLC0415

        ole = getattr(self.sw, "_oleobj_", self.sw)
        return dynamic.DumbDispatch(ole, PROGID)


def _wrap_dynamic(raw: Any) -> Any:
    """Wrap whatever COM handed us so properties and methods read uniformly."""
    from win32com.client import dynamic  # noqa: PLC0415

    ole = getattr(raw, "_oleobj_", raw)
    try:
        return dynamic.DumbDispatch(ole, PROGID)
    except Exception as exc:  # noqa: BLE001 - the native wrapper also works with member()
        log(f"could not build a pywin32 dynamic wrapper ({exc}); using the native dispatch object", "WARN")
        return raw


def _unpack_open_result(model: Any, errors: Any, warnings: Any) -> tuple[Any, Any, Any]:
    """Normalise the different shapes ``OpenDoc6`` comes back in."""
    if isinstance(model, (tuple, list)):
        values = list(model)
        model = values[0] if values else None
        if len(values) > 1 and values[1] is not None:
            try:
                errors.value = int(values[1])
            except (TypeError, ValueError):
                pass
        if len(values) > 2 and values[2] is not None:
            try:
                warnings.value = int(values[2])
            except (TypeError, ValueError):
                pass
    return model, errors, warnings


# --------------------------------------------------------------------------- #
# IR extraction
# --------------------------------------------------------------------------- #
def extract_ir(
    session: SolidWorksSession,
    model: Any,
    source_path: str,
    options: dict,
    *,
    kind: str = "part",
    full: bool = True,
) -> dict:
    """Build the neutral IR document from a SolidWorks part."""
    started = time.monotonic()
    app, version = session.app_label(), session.app_version()
    doc = ir.new(
        backend="solidworks",
        app=app,
        app_version=version,
        source_path=source_path,
        source_format=_source_format(source_path),
        kind=kind,
        name=str(try_member(model, "GetTitle", default=Path(source_path).stem) or Path(source_path).stem),
    )
    model_data = doc["model"]
    diagnostics: dict[str, Any] = {
        "driver_version": DRIVER_VERSION,
        "progid": PROGID,
        "revision": version,
        "build_numbers": session.build_numbers,
        "year": session.app_year(),
        "process_id": session.pid,
        "connected_via": "attach" if session.attached else "launch",
        "document_title": try_member(model, "GetTitle"),
        "document_type": try_member(model, "GetType"),
        "read_only_open": bool(options.get("read_only", True)),
    }

    diagnostics["mass_properties"] = _read_mass_properties(doc, model)
    diagnostics["bounding_box_source"] = _read_bbox(doc, model)
    body_rows, bodies_path = _read_bodies(doc, model, options, digest=full)
    diagnostics["bodies_api"] = bodies_path
    diagnostics["bodies_read"] = body_rows
    diagnostics["features"] = _read_features(doc, model, options)
    if full:
        diagnostics["sketches"] = _read_sketches(doc, model, options)
    diagnostics["custom_properties"] = _read_custom_properties(doc, model)
    diagnostics["configurations"] = _read_configurations(doc, model)
    diagnostics["material"] = _read_material(doc, model)

    # cross-check the document mass properties against the per-body volumes
    volume = (model_data.get("mass_properties") or {}).get("volume_mm3")
    body_volume = sum(row["volume_mm3"] for row in body_rows if row.get("volume_mm3") is not None) if body_rows else None
    if volume is not None and body_volume is not None and body_volume > 0:
        deviation = abs(volume - body_volume) / body_volume
        diagnostics["volume_cross_check"] = {
            "document_volume_mm3": volume,
            "sum_body_volume_mm3": round(body_volume, 6),
            "relative_deviation": round(deviation, 9),
        }
        if deviation > 1e-3:
            ir.warn(doc, f"document volume {volume} mm^3 differs from the sum of body volumes {body_volume} mm^3")

    model_data["units"] = "mm"
    ir.finish(
        doc,
        duration_s=time.monotonic() - started,
        driver=diagnostics,
    )
    return doc


def _read_mass_properties(doc: dict, model: Any) -> dict:
    r"""IModelDocExtension.CreateMassProperty: SI units, so m\ :sup:`3`, m\ :sup:`2`, kg."""
    extension = member(model, "Extension")
    mass_property = try_member(extension, "CreateMassProperty")
    if mass_property is None:
        ir.warn(doc, "IModelDocExtension.CreateMassProperty returned nothing; mass properties are unavailable")
        return {"api": None}

    def value(name: str) -> float | None:
        return _f(try_member(mass_property, name))

    mass = value("Mass")
    volume = value("Volume")
    area = value("SurfaceArea")
    density = value("Density")
    centroid = as_list(try_member(mass_property, "CenterOfMass"))
    inertia = as_list(try_member(mass_property, "GetMomentOfInertia", 0))

    ir.set_mass_properties(
        doc,
        volume_mm3=mm3_from_m3(volume),
        surface_area_mm2=mm2_from_m2(area),
        mass_kg=mass,
        density_kg_m3=density,
        centroid_mm=_round_vec([mm_from_m(value) for value in centroid]) if len(centroid) >= 3 else None,
        inertia_kg_mm2=_inertia_mm2(inertia),
    )
    if volume is None:
        ir.warn(doc, "IMassProperty.Volume returned nothing")
    if area is None:
        ir.warn(doc, "IMassProperty.SurfaceArea returned nothing")
    if mass is None:
        ir.warn(doc, "IMassProperty.Mass returned nothing")
    return {
        "api": "IModelDocExtension.CreateMassProperty",
        "mass_kg": mass,
        "volume_m3": volume,
        "surface_area_m2": area,
        "density_kg_m3": density,
        "centroid_count": len(centroid),
        "inertia_count": len(inertia),
    }


def _inertia_mm2(values: list) -> list[list[float]] | None:
    if len(values) < 9:
        return None
    try:
        return [
            [round(float(values[row * 3 + column]) * 1_000_000.0, 6) for column in range(3)]
            for row in range(3)
        ]
    except (TypeError, ValueError):
        return None


def _read_bbox(doc: dict, model: Any) -> str | None:
    source: str | None = None
    raw = try_member(model, "GetPartBox", True)
    box = as_list(raw)
    if len(box) >= 6:
        lower = [mm_from_m(box[index]) for index in range(3)]
        upper = [mm_from_m(box[index + 3]) for index in range(3)]
        if all(value is not None for value in lower + upper):
            ir.set_bbox(doc, min_mm=lower, max_mm=upper)
            source = "model.GetPartBox(True)"
            log(f"bounding box from GetPartBox: min={lower} max={upper}")
            return source
    lower = [math.inf] * 3
    upper = [-math.inf] * 3
    for body in as_list(try_member(model, "GetBodies2", SW_BODY_ALL, False)):
        body_box = as_list(try_member(body, "GetBodyBox"))
        if len(body_box) < 6:
            continue
        for index in range(3):
            low, high = _f(body_box[index]), _f(body_box[index + 3])
            if low is None or high is None:
                continue
            lower[index] = min(lower[index], low)
            upper[index] = max(upper[index], high)
    if all(math.isfinite(value) for value in lower + upper):
        ir.set_bbox(doc, min_mm=[value * 1000.0 for value in lower], max_mm=[value * 1000.0 for value in upper])
        source = "union(IBody2.GetBodyBox)"
        log(f"bounding box from body boxes: min={lower} max={upper}")
        return source
    ir.warn(doc, "bounding box unavailable: GetPartBox and the body boxes returned nothing usable")
    return None


def _read_bodies(doc: dict, model: Any, options: dict, *, digest: bool) -> tuple[list[dict], str]:
    bodies, path = _session_bodies(model)
    if not bodies:
        ir.warn(doc, "no solid bodies were returned by GetBodies2 (part may be empty or not rebuilt)")
        return [], path
    log(f"bodies: {len(bodies)} via {path}")
    max_groups = int(options.get("max_digest_groups", 120))
    rows: list[dict] = []
    digest_acc: dict[str, dict] = {kind: {"faces": 0, "area_mm2": 0.0, "groups": {}} for kind in ir.SURFACE_KINDS}
    for index, body in enumerate(bodies):
        name = str(try_member(body, "Name", default=f"body-{index + 1}") or f"body-{index + 1}")
        faces = as_list(try_member(body, "GetFaces"))
        edges = as_list(try_member(body, "GetEdges"))
        vertices = as_list(try_member(body, "GetVertices"))
        area_m2 = 0.0
        for face in faces:
            area_m2 += _f(try_member(face, "GetArea", default=0.0)) or 0.0
        mass = as_list(try_member(body, "GetMassProperties", 1.0))
        volume_m3 = _f(mass[3]) if len(mass) >= 4 else None
        body_box = as_list(try_member(body, "GetBodyBox"))
        min_mm = max_mm = None
        if len(body_box) >= 6:
            min_mm = [mm_from_m(body_box[index]) for index in range(3)]
            max_mm = [mm_from_m(body_box[index + 3]) for index in range(3)]
            if any(value is None for value in min_mm + max_mm):
                min_mm = max_mm = None
        closed = None  # IBody2 has no IsClosed in the 2025 type library (verified)
        row = ir.add_body(
            doc,
            name=name,
            kind=_body_kind(body),
            faces=len(faces),
            edges=len(edges),
            vertices=len(vertices),
            volume_mm3=mm3_from_m3(volume_m3),
            area_mm2=mm2_from_m2(area_m2),
            min_mm=min_mm,
            max_mm=max_mm,
            closed=bool(closed) if isinstance(closed, bool) else None,
        )
        rows.append(
            {
                "name": name,
                "faces": len(faces),
                "edges": len(edges),
                "vertices": len(vertices),
                "area_mm2": row.get("area_mm2"),
                "volume_mm3": row.get("volume_mm3"),
            }
        )
        if digest:
            _digest_faces(doc, digest_acc, faces, max_groups)
    return rows, path


def _session_bodies(model: Any) -> tuple[list, str]:
    raw = try_member(model, "GetBodies2", SW_BODY_ALL, False)
    path = "GetBodies2(swAllBodies=-1, False)"
    bodies = as_list(raw)
    if not bodies:
        log("GetBodies2(swAllBodies) returned no bodies; retrying with swSolidBody")
        bodies = as_list(try_member(model, "GetBodies2", SW_BODY_SOLID, False))
        path = "GetBodies2(swSolidBody=0, False) [swAllBodies returned none]"
    return bodies, path


def _body_kind(body: Any) -> str:
    body_type = try_member(body, "GetType")
    return {0: "solid", 1: "sheet", 2: "wire", 4: "general", 6: "mesh"}.get(
        int(body_type) if isinstance(body_type, (int, float)) else -99, "solid"
    )


def _digest_faces(doc: dict, acc: dict, faces: list, max_groups: int) -> None:
    for face in faces:
        surface = try_member(face, "GetSurface")
        if surface is None:
            continue
        identity = try_member(surface, "Identity")
        try:
            code = int(identity)
        except (TypeError, ValueError):
            code = None
        kind = SURFACE_KINDS.get(code, "other")
        area_mm2 = mm2_from_m2(_f(try_member(face, "GetArea")) or 0.0) or 0.0
        bucket = acc[kind]
        bucket["faces"] += 1
        bucket["area_mm2"] += area_mm2
        fields, signature = _surface_fields(surface, kind, code, face)
        groups = bucket["groups"]
        if signature in groups:
            groups[signature]["count"] += 1
            groups[signature]["area_mm2"] += area_mm2
        elif len(groups) < max_groups:
            entry = {"count": 1, "area_mm2": area_mm2}
            entry.update(fields)
            groups[signature] = entry
        else:
            bucket["overflow"] = int(bucket.get("overflow", 0)) + 1
    covered = {"plane", "cylinder", "cone", "sphere", "torus", "other"}
    if not covered.issuperset(acc):
        return
    for kind, bucket in acc.items():
        if bucket["faces"] == 0:
            continue
        ir.add_surface(
            doc,
            kind,
            role="summary",
            faces=bucket["faces"],
            area_mm2=round(bucket["area_mm2"], 6),
            groups=len(bucket["groups"]),
        )
        for entry in bucket["groups"].values():
            ir.add_surface(
                doc,
                kind,
                count=entry["count"],
                area_mm2=round(entry["area_mm2"], 6),
                **{key: value for key, value in entry.items() if key not in ("count", "area_mm2")},
            )
        if bucket.get("overflow"):
            ir.limit(
                doc,
                f"geometry_digest.{kind}: {bucket['overflow']} faces beyond max_digest_groups="
                f"{max_groups} were counted in the summary but not listed individually",
            )
    log("surface face counts: " + json.dumps({kind: bucket["faces"] for kind, bucket in acc.items() if bucket["faces"]}))


def _surface_fields(surface: Any, kind: str, code: int | None, face: Any) -> tuple[dict, tuple]:
    """Return (IR fields, grouping signature) for one face's analytic surface.

    Plane and cylinder parameters are verified against the skill's working code
    (PlaneParams = normal + root point, CylinderParams = origin + axis + radius,
    all in metres).  The remaining analytic types are recorded raw so the digest
    never claims more precision than was verified.
    """
    if kind == "plane":
        params = [_f(value) for value in as_list(try_member(surface, "PlaneParams"))]
        normal = _round_vec(params[:3], 9) if len(params) >= 3 else None
        point = _round_vec([value * 1000.0 for value in params[3:6]], 6) if len(params) >= 6 else None
        signature = (kind, tuple(normal or ()), tuple(point or ()))
        return {"normal": normal, "point_mm": point}, signature
    if kind == "cylinder":
        params = [_f(value) for value in as_list(try_member(surface, "CylinderParams"))]
        origin = _round_vec([value * 1000.0 for value in params[0:3]], 6) if len(params) >= 3 else None
        axis = _round_vec(params[3:6], 9) if len(params) >= 6 else None
        radius = round(params[6] * 1000.0, 6) if len(params) >= 7 and params[6] is not None else None
        internal = try_member(face, "FaceInSurfaceSense")
        internal = bool(internal) if isinstance(internal, bool) else None
        signature = (kind, tuple(axis or ()), radius, internal)
        return {
            "origin_mm": origin,
            "axis": axis,
            "radius_mm": radius,
            "diameter_mm": round(radius * 2.0, 6) if radius is not None else None,
            "internal": internal,
        }, signature
    params = [_f(value) for value in as_list(try_member(surface, f"{kind.capitalize()}Params"))]
    if not params:
        params = []
    signature = (kind, code, tuple(round(value, 9) if value is not None else None for value in params))
    return {"surface_code": code, "params_si": params}, signature


def _iter_feature_tree(model: Any):
    """Yield ``(feature, depth, parent_name)`` for the whole FeatureManager tree.

    ``IFeature.GetNextFeature`` walks only the top level; sketches and other
    consumed features are sub-features, so a real tree walk needs
    ``GetFirstSubFeature`` / ``GetNextSubFeature`` (verified against the 2025
    type library in work/sw/verify_api_names.txt).
    """

    def walk(feature: Any, depth: int, parent: str | None, budget: list[int]):
        if depth > 8 or budget[0] <= 0:
            return
        budget[0] -= 1
        yield feature, depth, parent
        name = str(try_member(feature, "Name", default="") or "")
        sub = try_member(feature, "GetFirstSubFeature")
        while sub is not None and budget[0] > 0:
            yield from walk(sub, depth + 1, name or parent, budget)
            sub = try_member(sub, "GetNextSubFeature")

    budget = [5000]  # hard stop so a pathological tree cannot hang a batch job
    feature = try_member(model, "FirstFeature")
    while feature is not None:
        yield from walk(feature, 0, None, budget)
        if budget[0] <= 0:
            return
        feature = try_member(feature, "GetNextFeature")


def _read_features(doc: dict, model: Any, options: dict) -> dict:
    max_features = int(options.get("max_features", 500))
    # A consumed sketch is reachable both from GetNextFeature and as a sub-feature
    # of its consumer; keying by name (SolidWorks feature names are unique) keeps
    # one entry and prefers the occurrence that knows its parent.
    records: dict[str, dict] = {}
    order: list[str] = []
    index = 0
    for feature, depth, parent in _iter_feature_tree(model):
        if index >= max_features:
            ir.limit(doc, f"feature tree truncated at max_features={max_features} entries")
            break
        index += 1
        name = str(try_member(feature, "Name", default=f"feature-{index}") or f"feature-{index}")
        type_name = str(try_member(feature, "GetTypeName2", default="unknown") or "unknown")
        children = _feature_names(feature, "GetChildren")
        if parent is None:
            parents = _feature_names(feature, "GetParents")
            parent = parents[0] if parents else None
        record = {
            "name": name,
            "type": type_name,
            "suppressed": _feature_suppressed(feature),
            "parent": parent,
            "children": children,
            "depth": depth,
        }
        previous = records.get(name)
        if previous is None:
            records[name] = record
            order.append(name)
        elif previous["parent"] is None and record["parent"] is not None:
            records[name] = record
    for name in order:
        record = records[name]
        ir.add_feature(
            doc,
            name=record["name"],
            type=record["type"],
            suppressed=record["suppressed"],
            parent=record["parent"],
            children=record["children"],
        )
    log(f"feature tree: {len(order)} unique features (including sub-features)")
    return {"count": len(order), "truncated": len(order) >= max_features}


def _feature_suppressed(feature: Any) -> bool:
    value = try_member(feature, "IsSuppressed")
    if value is None:
        value = try_member(feature, "IsSuppressed2", "")
    if isinstance(value, (tuple, list)):
        value = value[0] if value else False
    return bool(value) if isinstance(value, bool) else False


def _feature_names(feature: Any, method: str) -> list[str]:
    names: list[str] = []
    for child in as_list(try_member(feature, method)):
        name = try_member(child, "Name")
        if name:
            names.append(str(name))
    return [name for name in names if name]


def _read_sketches(doc: dict, model: Any, options: dict) -> dict:
    """Sketches are consumed features, so they are found through the feature tree.

    The 2025 type library has no ``IModelDoc2.GetSketchCount`` / ``GetSketch``
    and no dimension enumeration on ``ISketch`` (verified offline, see
    work/sw/verify_api_names.txt); dimensions are read from the owning feature's
    display dimensions instead.
    """
    max_sketches = int(options.get("max_sketches", 100))
    entries: list[tuple[str, Any, Any]] = []
    seen: set[str] = set()
    source = "feature tree ProfileFeature/3DProfileFeature (sub-features included)"
    for feature, _depth, _parent in _iter_feature_tree(model):
        if str(try_member(feature, "GetTypeName2", default="")) not in ("ProfileFeature", "3DProfileFeature"):
            continue
        name = str(try_member(feature, "Name", default=f"sketch-{len(entries) + 1}") or f"sketch-{len(entries) + 1}")
        if name in seen:
            # A consumed sketch is reachable twice (top level and as a sub-feature).
            continue
        sketch = try_member(feature, "GetSpecificFeature2")
        if sketch is None:
            continue
        seen.add(name)
        entries.append((name, sketch, feature))
        if len(entries) >= max_sketches:
            ir.limit(doc, f"sketch list truncated at max_sketches={max_sketches}")
            break
    if not entries:
        log("no ProfileFeature sketches were found in the feature tree")
        return {"source": None, "count": 0}
    dimension_types: dict[str, int] = {}
    for name, sketch, feature in entries:
        segments = as_list(try_member(sketch, "GetSketchSegments"))
        points = as_list(try_member(sketch, "GetSketchPoints2"))
        dimensions, types = _feature_dimensions(feature)
        dimension_types.update(types)
        plane = _sketch_plane(sketch)
        ir.add_sketch(
            doc,
            name=name,
            plane=plane,
            entities=len(segments),
            dimensions=dimensions or None,
        )
        log(
            f"sketch {name!r}: plane={plane!r} {len(segments)} segments, {len(points)} points, "
            f"{len(dimensions)} dimensions"
        )
    return {"source": source, "count": len(entries), "dimension_types": dimension_types}


def _sketch_plane(sketch: Any) -> str | None:
    """Name of the face/plane the sketch is attached to, when the API exposes it."""
    entity = try_member(sketch, "GetReferenceEntity")
    if entity is None:
        return None
    name = try_member(entity, "Name")
    if name:
        return str(name)
    surface = try_member(entity, "GetSurface")
    identity = try_member(surface, "Identity") if surface is not None else None
    if identity is not None:
        kind = SURFACE_KINDS.get(int(identity), "other") if isinstance(identity, (int, float)) else "other"
        return f"face({kind})"
    return None


def _feature_dimensions(feature: Any) -> tuple[dict, dict]:
    """Display dimensions owned by a feature (the verified route for sketch dims).

    ``IFeature.GetFirstDisplayDimension`` is a property returning one
    IDisplayDimension, ``GetNextDisplayDimension`` takes the previous one, and
    ``EnumDisplayDimensions`` returns the whole array; the array is used as a
    fallback because a consumed sketch sometimes reports no "first" dimension.
    """
    dimensions: dict[str, dict] = {}
    types: dict[str, int] = {}
    displays: list[Any] = []
    display = try_member(feature, "GetFirstDisplayDimension")
    guard = 0
    while display is not None and guard < 200:
        guard += 1
        displays.append(display)
        display = try_member(feature, "GetNextDisplayDimension", display)
    if not displays:
        displays = as_list(try_member(feature, "EnumDisplayDimensions"))
    for display in displays[:200]:
        try:
            dimension = try_member(display, "GetDimension2", 0) or try_member(display, "GetDimension")
            full_name = str(try_member(dimension, "FullName", default="") or "")
            # IDisplayDimension.Type2 is swDimensionType_e; it decides the unit of
            # IDimension.SystemValue (metres for linear, radians for angular).
            type_code = try_member(display, "Type2")
            try:
                type_code = int(type_code) if type_code is not None else 0
            except (TypeError, ValueError):
                type_code = 0
            system_value = _f(try_member(dimension, "SystemValue"))
            if not full_name or system_value is None:
                continue
            types[full_name] = type_code
            entry: dict[str, Any] = {"type": type_code}
            if type_code in DIMENSION_LINEAR:
                entry["value_mm"] = round(system_value * 1000.0, 6)
            elif type_code in DIMENSION_ANGULAR:
                entry["value_deg"] = round(math.degrees(system_value), 6)
            else:
                entry["system_value"] = system_value
            dimensions[full_name] = entry
        except Exception as exc:  # noqa: BLE001 - dimensions are optional evidence
            log(f"dimension read failed: {exc}", "WARN")
    return dimensions, types


def _read_custom_properties(doc: dict, model: Any) -> dict:
    extension = member(model, "Extension")
    manager = try_member(extension, "CustomPropertyManager", "")
    if manager is None:
        ir.warn(doc, "IModelDocExtension.CustomPropertyManager returned nothing; custom properties unavailable")
        return {"count": 0}
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    names = _property_names(manager)
    properties: dict[str, str] = {}
    for name in names:
        raw_value = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
        resolved_value = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
        was_resolved = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
        is_linked = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
        try:
            status = int(manager.Get6(name, False, raw_value, resolved_value, was_resolved, is_linked))
        except Exception as exc:  # noqa: BLE001 - fall back to the older Get4 shape
            log(f"CustomPropertyManager.Get6({name!r}) failed: {exc}; trying Get4", "WARN")
            try:
                status = int(manager.Get4(name, False, raw_value, resolved_value))
            except Exception as exc2:  # noqa: BLE001
                log(f"CustomPropertyManager.Get4({name!r}) failed: {exc2}", "WARN")
                continue
        value = str(resolved_value.value or raw_value.value or "") if status != 1 else ""
        if status != 1:
            properties[name] = value
    if properties:
        ir.add_custom_properties(doc, properties)
    log(f"custom properties: {len(properties)}")
    return {"count": len(properties), "names": names}


def _property_names(manager: Any) -> list[str]:
    raw = try_member(manager, "GetNames")
    names: list[str] = []
    if isinstance(raw, (tuple, list)):
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, (tuple, list)):
                names.extend(str(entry) for entry in item if isinstance(entry, str))
    elif isinstance(raw, str):
        names.append(raw)
    return names


def _read_configurations(doc: dict, model: Any) -> dict:
    raw = try_member(model, "GetConfigurationNames")
    names = [str(name) for name in as_list(raw) if name]
    active = try_member(
        try_member(try_member(model, "ConfigurationManager"), "ActiveConfiguration"), "Name"
    )
    active = str(active) if active else None
    for name in names:
        ir.add_configuration(doc, name=name, active=(active == name))
    log(f"configurations: {names} (active={active!r})")
    return {"names": names, "active": active}


def _read_material(doc: dict, model: Any) -> dict:
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    database = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, "")
    material = None
    try:
        material = model.GetMaterialPropertyName2("", database)
    except Exception as exc:  # noqa: BLE001 - no material is a normal state
        log(f"GetMaterialPropertyName2 failed: {exc}", "WARN")
    material_id = try_member(model, "MaterialIdName")
    if material:
        ir.set_material(doc, name=str(material))
    elif material_id:
        ir.set_material(doc, name=str(material_id))
    density = (doc["model"]["mass_properties"] or {}).get("density_kg_m3")
    if density is not None:
        ir.set_material(doc, density_kg_m3=density)
    elif material is None:
        ir.warn(doc, "no material is assigned to the document; density is SolidWorks' default")
    log(f"material: name={material!r} database={database.value!r} id={material_id!r} density={density!r}")
    return {"name": material, "database": str(database.value or ""), "material_id": material_id}


# --------------------------------------------------------------------------- #
# output verification
# --------------------------------------------------------------------------- #
STEP_SCHEMA_RE = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", re.IGNORECASE)

#: STEP application-protocol schemas seen from the SolidWorks exporter.
STEP_SCHEMAS = {
    "CONFIG_CONTROL_DESIGN": "ap203",
    "AUTOMOTIVE_DESIGN": "ap214",
    "AUTOMOTIVE_DESIGN_CC2": "ap214",
    "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF": "ap242",
}


def _step_schema_of(path: Path) -> tuple[str | None, str | None, str]:
    """Return ``(schema, ap, head_text)`` for an ISO-10303 file; never raises.

    ``ap`` is the requested-style label (ap203/ap214/ap242) when the schema name
    or the header text identifies it; the raw ``schema`` name is always reported.
    """
    try:
        with open(path, "rb") as handle:
            head_bytes = handle.read(1 << 14)
    except OSError:
        return None, None, ""
    head = head_bytes.decode("latin-1", errors="replace")
    if "ISO-10303" not in head:
        return None, None, head
    match = STEP_SCHEMA_RE.search(head)
    schema = match.group(1) if match else None
    ap = STEP_SCHEMAS.get((schema or "").upper())
    if ap is None:
        upper = head.upper()
        for candidate in ("AP242", "AP214", "AP203"):
            if candidate in upper:
                ap = candidate.lower()
                break
    return schema, ap, head


def verify_step(path: Path) -> dict:
    if not path.exists():
        raise DriverError(f"STEP export did not create {path}")
    size = path.stat().st_size
    if size <= 0:
        raise DriverError(f"STEP export created an empty file: {path}")
    schema, ap, head = _step_schema_of(path)
    if "ISO-10303" not in head:
        raise DriverError(f"{path.name} is not an ISO-10303 STEP file (header: {head[:120]!r})")
    advanced_face = False
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            if b"ADVANCED_FACE" in chunk:
                advanced_face = True
                break
    if not advanced_face:
        raise DriverError(f"STEP export {path.name} contains no ADVANCED_FACE entity (not a B-rep export)")
    return {
        "size_bytes": size,
        "schema": schema,
        "step_ap": ap.upper() if ap else None,
        "has_advanced_face": advanced_face,
        "header_application": _step_header_value(head, "FILE_NAME"),
    }


def _wait_step_valid(path: Path, timeout: float = 3.0) -> tuple[dict | None, str | None]:
    """``verify_step`` with a short settle/retry window.

    SaveAs returns before the file handle is necessarily readable on every build;
    declaring "no ADVANCED_FACE" on a file the exporter is still closing would fail
    a good export, so keep re-reading for a couple of seconds.
    """
    started = time.monotonic()
    last_error: str | None = None
    while True:
        time.sleep(0.1)
        try:
            return verify_step(path), None
        except DriverError as exc:
            last_error = str(exc)
            if time.monotonic() - started >= timeout:
                return None, last_error
            time.sleep(0.2)


def _step_header_value(head: str, key: str) -> str | None:
    match = re.search(rf"{key}\s*\(([^;]*)\)\s*;", head, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1)[:300].replace("\n", " ").strip()


def verify_parasolid(path: Path) -> dict:
    if not path.exists():
        raise DriverError(f"Parasolid export did not create {path}")
    size = path.stat().st_size
    if size <= 0:
        raise DriverError(f"Parasolid export created an empty file: {path}")
    head = path.read_bytes()[:4096].decode("latin-1", errors="replace")
    transmit = "TRANSMIT FILE" in head.upper()
    if not transmit:
        ir_note = head[:80].replace("\r", " ").replace("\n", " ")
        log(f"Parasolid header does not contain 'TRANSMIT FILE': {ir_note!r}", "WARN")
    return {"size_bytes": size, "has_transmit_file": transmit, "header": head[:120].replace("\n", " ")}


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def _exe_file_version(exe: Path) -> str | None:
    """Windows file version of SLDWORKS.exe (the SP number lives here)."""
    try:
        from win32api import GetFileVersionInfo  # noqa: PLC0415 - pywin32, Windows only

        data = GetFileVersionInfo(str(exe), "\\")
        ms, ls = data.get("FileVersionMS"), data.get("FileVersionLS")
        if ms is None or ls is None:
            return None
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001 - version metadata is optional evidence
        return None


def _read_build_numbers(sw: Any) -> dict:
    """Best-effort ``GetBuildNumbers2`` read.

    The 2025 type library declares ``GetBuildNumbers2`` with three ``[out]``
    parameters (unlike the documented zero-argument helper), so the values are
    read through by-ref VARIANTs; if the shape is different the error is reported
    instead of being hidden.
    """
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    for name, count in (("GetBuildNumbers2", 3), ("GetBuildNumbers", 2)):
        try:
            method = getattr(sw, name)
            slots = [win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0) for _ in range(count)]
            returned = method(*slots)
            return {
                "api": f"{name}({', '.join(['byref int'] * count)})",
                "values": [slot.value for slot in slots],
                "return": returned if isinstance(returned, (int, float, str)) else None,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - report the shape mismatch
            last = f"{name}: {type(exc).__name__}: {exc}"
    return {"api": None, "values": None, "return": None, "error": last}


def job_probe(request: dict) -> dict:
    """Report the SolidWorks build and the exchange/import capabilities."""
    options = request.get("options") or {}
    with SolidWorksSession(options) as session:
        sw = session.sw
        build = _read_build_numbers(sw)
        raw_executable = str(try_member(sw, "GetExecutablePath", default="") or "")
        executable = Path(raw_executable) if raw_executable else None
        # GetExecutablePath returns the install *directory* on 2025 SP05; accept
        # both shapes so the report is right on either.
        install_root = None
        if executable is not None:
            install_root = executable if executable.is_dir() else executable.parent
        exe = (install_root / "SLDWORKS.exe") if install_root else None
        translator_modules = {
            name: bool(install_root and (install_root / name).exists())
            for name in ("sldstepu.dll", "sldxtkitu.dll", "sldxtoolkitu.dll", "sldigesu.dll")
        }
        info: dict[str, Any] = {
            "revision": session.revision,
            "build_numbers": build["values"],
            "build_numbers_read": build,
            "year": session.app_year(),
            "app_label": session.app_label(),
            "executable_path": str(exe) if exe is not None and exe.exists() else raw_executable,
            "executable_file_version": _exe_file_version(exe) if exe is not None and exe.exists() else None,
            "install_root": str(install_root) if install_root is not None else None,
            "process_id": session.pid,
            "connected_via": "attach" if session.attached else "launch",
            "process_owned": session.owned,
            "3d_interconnect_enabled": session._get_toggle(SW_TOGGLE_3D_INTERCONNECT),
            "step_ap": session.step_ap(),
            "default_templates": {
                label: try_member(sw, "GetUserPreferenceStringValue", preference, default="")
                for preference, (label, _pattern) in SW_PREF_TEMPLATE.items()
            },
            "open_documents": [
                str(try_member(document, "GetTitle", default=""))
                for document in as_list(try_member(sw, "GetDocuments"))
            ],
            "translator_modules": translator_modules,
            # Module presence is informational; the export/import jobs are what
            # actually verify STEP (ISO-10303 + ADVANCED_FACE) and Parasolid
            # (TRANSMIT FILE) output on this seat.
            "capabilities": {
                "step_translator_module": translator_modules["sldstepu.dll"],
                "parasolid_module_candidates": [
                    name for name in ("sldxtkitu.dll", "sldxtoolkitu.dll") if translator_modules[name]
                ],
                "iges_module": translator_modules["sldigesu.dll"],
                "verified_by": "export/import jobs (file header + entity checks on disk)",
            },
        }
        log(
            f"probe: revision={session.revision} file_version={info['executable_file_version']} "
            f"build_numbers={build['values']} modules={translator_modules}"
        )
        log("TOKEN PROBE_OK")
        return {
            "ok": True,
            "kind": "probe",
            "app": session.app_label(),
            "app_version": session.revision,
            "outputs": [],
            "warnings": [],
            "error": None,
            "probe": info,
        }


def job_read(request: dict) -> dict:
    """Open a native document and write the neutral IR."""
    started = time.monotonic()
    source = request.get("input")
    if not source:
        raise DriverError("read job needs 'input'")
    options = dict(request.get("options") or {})
    full = bool(options.get("full", True))
    with SolidWorksSession(options) as session:
        model = session.open_native(source, read_only=bool(options.get("read_only", True)))
        session.track(model)
        doc = extract_ir(session, model, str(source), options, kind="part", full=full)
    outputs: list[str] = []
    output = request.get("output")
    if output:
        written = ir.dump(doc, output)
        outputs.append(str(written.resolve()))
        log(f"IR written to {written} ({written.stat().st_size} bytes)")
    summary = ir.summarize(doc)
    log(
        "IR summary: bodies={bodies} features={features} faces={faces} volume_mm3={volume_mm3} "
        "bbox={bbox_size_mm} warnings={warnings}".format(**summary)
    )
    log("TOKEN READ_OK")
    return {
        "ok": True,
        "kind": "read",
        "app": doc["source"]["app"],
        "app_version": doc["source"]["app_version"],
        "outputs": outputs,
        "warnings": list(doc["diagnostics"]["warnings"]),
        "error": None,
        "ir": doc,
        "summary": summary,
        "duration_s": round(time.monotonic() - started, 3),
    }


def job_export(request: dict) -> dict:
    """Export a native part to STEP or Parasolid and verify the output on disk."""
    started = time.monotonic()
    source = request.get("input")
    output = request.get("output")
    if not source:
        raise DriverError("export job needs 'input'")
    if not output:
        raise DriverError("export job needs 'output'")
    options = dict(request.get("options") or {})
    fmt = str(request.get("format") or Path(output).suffix.lstrip(".")).lower()
    if fmt in ("stp",):
        fmt = "step"
    if fmt in ("xt", "x_t", "x_b"):
        fmt = "parasolid"
    if fmt not in ("step", "parasolid"):
        raise DriverError(f"unsupported export format {request.get('format')!r}; use step or parasolid")
    target = Path(output).resolve()
    with SolidWorksSession(options) as session:
        model = session.open_native(source, read_only=True)
        session.track(model)
        details: dict[str, Any] = {"format": fmt, "target": str(target)}
        provenance: dict[str, Any] = {
            "target": str(target),
            "format": fmt,
            "duration_s": None,
        }
        if fmt == "step":
            # The pipeline passes ``step_variant`` (base.STEP_VARIANTS); the
            # public driver option is ``step_ap``.  Both name ap203/ap214/ap242.
            wanted_ap = str(options.get("step_ap") or options.get("step_variant") or "ap242").lower()
            details["step_ap"] = session.export_step(model, target, wanted_ap)
            provenance["step_ap_preference"] = details["step_ap"]
        else:
            ok, errors, warnings = session.save_as(model, target)
            if not ok:
                raise DriverError(
                    f"SolidWorks SaveAs({target.name}) failed",
                    detail=f"error code {errors}, warning code {warnings}",
                )
            provenance.update(save_as_errors=errors, save_as_warnings=warnings)
        session.track(model)
        provenance["duration_s"] = round(time.monotonic() - started, 3)
        provenance["size_bytes"] = target.stat().st_size if target.exists() else None
        if not target.exists():
            raise DriverError(f"SaveAs reported success but {target} does not exist")
        if fmt == "step":
            provenance.update(verify_step(target))
            log(f"STEP verified: {provenance['size_bytes']} bytes schema={provenance.get('schema')!r} "
                f"AP={provenance.get('step_ap')} ADVANCED_FACE={provenance.get('has_advanced_face')}")
            log("TOKEN STEP_EXPORTED")
        else:
            provenance.update(verify_parasolid(target))
            log(f"Parasolid verified: {provenance['size_bytes']} bytes header={provenance.get('header')!r}")
            log("TOKEN PARASOLID_EXPORTED")
        ir_doc = None
        if options.get("collect_ir"):
            ir_doc = extract_ir(session, model, source, options, kind="part", full=False)
    return {
        "ok": True,
        "kind": "export",
        "app": f"SolidWorks {session.app_year() or ''}".strip(),
        "app_version": session.revision,
        "outputs": [str(target)],
        "warnings": [],
        "error": None,
        "export": provenance,
        "ir": ir_doc,
        "duration_s": round(time.monotonic() - started, 3),
    }


def job_import(request: dict) -> dict:
    """Import STEP/Parasolid into SolidWorks and save it as a native part."""
    started = time.monotonic()
    source = request.get("input")
    output = request.get("output")
    if not source:
        raise DriverError("import job needs 'input' (a STEP or Parasolid file)")
    if not output:
        raise DriverError("import job needs 'output' (the .sldprt to create)")
    options = dict(request.get("options") or {})
    target = Path(output).resolve()
    if target.suffix.lower() != ".sldprt":
        raise DriverError(f"import output must be a .sldprt, got {target.suffix!r}")
    with SolidWorksSession(options) as session:
        suffix = Path(source).suffix.lower()
        if suffix in (".x_t", ".x_b"):
            model = session.open_parasolid(source)
            import_route = "GetImportFileData->LoadFile4 r (empty-variant fallback; OpenDoc6 not used)"
        else:
            model = session.load_foreign(source)
            import_route = "GetImportFileData + LoadFile4"
        title = str(try_member(model, "GetTitle", default="") or "")
        session.track(model)
        bodies, bodies_path = _session_bodies(model)
        volume_m3 = session.mass_property_volume(model)
        volume_mm3 = mm3_from_m3(volume_m3)
        log(
            f"imported document {title!r}: {len(bodies)} bodies via {bodies_path}, "
            f"volume={volume_mm3} mm^3, route={import_route}"
        )
        if not bodies:
            raise DriverError(
                f"import of {Path(source).name} produced no solid bodies",
                detail=f"{bodies_path} returned nothing; 3D Interconnect={session._get_toggle(SW_TOGGLE_3D_INTERCONNECT)}",
            )
        if volume_m3 is None:
            log("mass properties returned no volume for the imported part (IR will report it)", "WARN")
        elif volume_m3 <= 0:
            raise DriverError(
                f"import of {Path(source).name} produced a zero-volume part",
                detail=f"IMassProperty.Volume={volume_m3} m^3 with {len(bodies)} bodies",
            )
        ok, errors, warnings = session.save_as(model, target)
        if not ok:
            raise DriverError(
                f"SaveAs({target.name}) after import failed",
                detail=f"error code {errors}, warning code {warnings}",
            )
        if not target.exists() or target.stat().st_size <= 0:
            raise DriverError(f"import saved no usable file: {target}")
        session.track(model)
        doc = extract_ir(session, model, str(source), options, kind="part", full=bool(options.get("collect_ir", False)))
        summary = ir.summarize(doc)
        provenance = {
            "source": str(Path(source).resolve()),
            "target": str(target),
            "size_bytes": target.stat().st_size,
            "bodies": len(bodies),
            "bodies_api": bodies_path,
            "volume_mm3": volume_mm3,
            "import_route": import_route,
            "summary": summary,
            "duration_s": round(time.monotonic() - started, 3),
        }
        log(f"native part saved: {target} ({provenance['size_bytes']} bytes)")
        token = "PARASOLID_IMPORTED" if suffix in (".x_t", ".x_b") else "STEP_IMPORTED"
        log(f"TOKEN {token} bodies={len(bodies)} volume_mm3={volume_mm3}")
    return {
        "ok": True,
        "kind": "import",
        "app": doc["source"]["app"],
        "app_version": doc["source"]["app_version"],
        "outputs": [str(target)],
        "warnings": list(doc["diagnostics"]["warnings"]),
        "error": None,
        "import": provenance,
        "ir": doc,
        "duration_s": round(time.monotonic() - started, 3),
    }


JOB_KINDS = {"probe": job_probe, "read": job_read, "export": job_export, "import": job_import}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def dispatch(request: dict) -> dict:
    kind = str(request.get("kind") or "").strip()
    handler = JOB_KINDS.get(kind)
    if handler is None:
        raise DriverError(
            f"unknown job kind {kind!r}",
            hint=f"supported kinds: {', '.join(sorted(JOB_KINDS))}",
        )
    log(f"===== {kind} job start (backend=solidworks, driver={DRIVER_VERSION}) =====")
    payload = handler(request)
    payload.setdefault("backend", "solidworks")
    payload.setdefault("app", "SolidWorks")
    payload.setdefault("app_version", "unknown")
    payload.setdefault("outputs", [])
    payload.setdefault("warnings", [])
    payload.setdefault("error", None)
    return payload


def _entry(argv: list[str] | None = None) -> int:
    started = time.time()
    parser = argparse.ArgumentParser(description="swnx-switch SolidWorks COM driver")
    parser.add_argument("--request", help="path to the request JSON (default: $SWNX_REQUEST)")
    args = parser.parse_args(argv)
    if args.request:
        os.environ.setdefault("SWNX_REQUEST", args.request)

    payload: dict[str, Any] = {
        "ok": False,
        "kind": "unknown",
        "backend": "solidworks",
        "app": "SolidWorks",
        "app_version": "unknown",
        "outputs": [],
        "warnings": [],
        "error": None,
    }
    exit_code = 1
    try:
        request = _load_request()
        payload["kind"] = str(request.get("kind") or "unknown")
        result = dispatch(request)
        payload.update(result)
        if payload.get("ok"):
            exit_code = 0
    except DriverError as exc:
        payload["error"] = str(exc)
        if exc.hint:
            payload["hint"] = exc.hint
        if exc.detail is not None:
            payload["detail"] = exc.detail
        log(f"ERROR {type(exc).__name__}: {exc}", "ERROR")
        if exc.hint:
            log(f"HINT {exc.hint}", "ERROR")
    except Exception as exc:  # noqa: BLE001 - last resort, still write a result
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["traceback"] = traceback.format_exc()
        log(traceback.format_exc(), "ERROR")
    finally:
        payload["duration_s"] = round(time.time() - started, 3)
        payload.setdefault("backend", "solidworks")
        payload.setdefault("app", "SolidWorks")
        payload.setdefault("app_version", "unknown")
        payload.setdefault("outputs", [])
        payload.setdefault("warnings", [])
        payload.setdefault("error", None)
        payload["driver_version"] = DRIVER_VERSION
        log(f"RESULT ok={payload.get('ok')} kind={payload.get('kind')} duration_s={payload['duration_s']} "
            f"error={payload.get('error')!r}")
        try:
            written = _write_result(payload)
            log(f"result JSON written to {written}")
        except Exception as exc:  # noqa: BLE001 - cannot do anything but complain
            log(f"could not write the result JSON: {exc}", "ERROR")
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
            return 2
    return exit_code


if __name__ == "__main__":
    sys.exit(_entry())
