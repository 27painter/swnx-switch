"""SolidWorks backend: one pywin32 COM driver process per job.

Why a subprocess
----------------
SolidWorks is a single-instance COM server that can pop a modal dialog, hold a
licence seat, or hang on a stale document.  Doing that in the converter's own
process would take the whole tool down; here a crashed COM call only costs one
driver process.  The driver is also the only place that needs ``pywin32`` and the
host interpreter, while the converter itself stays plain Python.

``Backend.run()`` (see :mod:`swnx_switch.backends.base`) owns the wire protocol:
it writes the request JSON, exports ``SWNX_REQUEST``/``SWNX_RESULT``/``SWNX_LOG``
and judges the run by the result file plus the log.  This class supplies the
command line, the environment and the honest availability/version answers, and
mirrors the convenience API of :class:`~swnx_switch.backends.nx.NXBackend`
(``probe`` / ``read`` / ``export`` / ``import_``) so the pipeline can treat both
CAD applications the same way.

Honest availability (report P1-3)
---------------------------------
``available()`` / ``installed()`` are the cheap static check (executable, driver,
HKCR ProgID) and deliberately do **not** mean "SolidWorks can start right now".
:meth:`SolidWorksBackend.probe_state` adds the tri-state
``installed`` / ``reachable`` (a running instance answers COM) /
``launchable`` (a COM activation or direct launch produced a server), filled by
a deep probe only; ``last_error`` carries the decoded COM failure and ``note``
says what was and was not verified.  A deep probe is opt-in (``deep=True`` or
``$SWNX_SW_PROBE_DEEP=1``) because it can start SolidWorks once.

Reusing the instance (report P0-3)
----------------------------------
A job option ``keep_app: true`` (or ``$SWNX_KEEP_APP=1``) tells the driver not to
``ExitApp`` an instance the job itself started; the next job's
``GetActiveObject`` attaches to it instead of cold-starting SolidWorks again.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from .base import Backend, Job, JobResult
from ..errors import EnvironmentError_, SwnxError
from ..paths import DEFAULT_WORKDIR, REPO_ROOT, find_solidworks, host_python

#: The driver lives in the source tree, not inside the package, so it can be
#: run (and read) without an install: tools/sw/driver.py next to src/.
DRIVER_PATH = REPO_ROOT / "tools" / "sw" / "driver.py"

#: Registry ProgID registered by the SolidWorks installer.
PROGID = "SldWorks.Application"

#: Exchange formats the driver can import.  STEP and Parasolid are the two the
#: project converts through; IGES is deliberately not advertised.
IMPORT_FORMATS = ("step", "parasolid")

#: SolidWorks starts in 20-90 s and a read job may then walk a large B-rep, so
#: the default is generous on purpose (same value as the NX backend).
DEFAULT_TIMEOUT = 900.0

#: Deep probe (``probe --deep``) actually touches COM once; keep it short.
DEEP_PROBE_TIMEOUT_ENV = "SWNX_SW_DEEP_PROBE_TIMEOUT"
DEFAULT_DEEP_PROBE_TIMEOUT = 20.0
#: Make the fast probe deep by default (opt-in through the environment).
DEEP_PROBE_ENV = "SWNX_SW_PROBE_DEEP"

_UNSET = object()

#: Human-readable probe note for the fast (static) path.  This is the part that
#: stops ``available: true`` from reading as "SolidWorks is startable right now"
#: (the report saw ``available: true`` while the executable died on startup).
FAST_PROBE_NOTE = "installed, not verified runnable; use probe --deep"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return bool(default)
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


class SolidWorksBackend(Backend):
    """Drive SolidWorks through ``tools/sw/driver.py`` (pywin32 COM)."""

    name = "solidworks"
    app = "SOLIDWORKS"
    #: Native documents a SolidWorks seat owns.
    supports_native = ("sldprt", "slddrw", "sldasm")
    #: Exchange formats the driver imports into a new ``.sldprt``.
    supports_import = IMPORT_FORMATS

    def __init__(
        self,
        exe: str | os.PathLike | None = None,
        python: str | os.PathLike | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._exe = str(exe) if exe else None
        self._python = str(python) if python else None
        self.timeout = float(timeout)
        self._install_cache: object = _UNSET

    # -- environment ------------------------------------------------------- #
    def driver_path(self) -> Path:
        override = os.environ.get("SWNX_SW_DRIVER")
        if override:
            return Path(override)
        return DRIVER_PATH

    @property
    def python_executable(self) -> str:
        """The interpreter that runs the driver (pywin32 must be importable).

        ``paths.host_python()`` returns the *calling* interpreter, which is the
        documented contract; ``$SWNX_SW_PYTHON`` lets a launcher point the COM
        driver at a different one when the converter runs under an interpreter
        without pywin32.
        """
        return self._python or os.environ.get("SWNX_SW_PYTHON") or str(host_python())

    @property
    def install(self):
        """Resolved SolidWorks installation (cached after the first lookup)."""
        if self._install_cache is _UNSET:
            try:
                self._install_cache = find_solidworks(self._exe)
            except OSError:
                self._install_cache = None
        return self._install_cache

    # -- Backend contract -------------------------------------------------- #
    def installed(self) -> bool:
        """Static install check: SLDWORKS.exe + driver + HKCR ProgID; no COM.

        This is what ``available()`` used to mean.  It says *installed*, not
        *runnable*: the report saw ``available: true`` while ``SLDWORKS.exe``
        exited immediately on startup (P1-3), so callers must read
        :meth:`probe_state` to learn whether the app can actually be reached or
        started right now.
        """
        install = self.install
        if install is None or not Path(install.exe).exists():
            return False
        if not self.driver_path().exists():
            return False
        return _progid_registered(PROGID)

    def available(self) -> bool:
        """Backend contract: the application is *installed* (see ``installed``)."""
        return self.installed()

    def app_version(self) -> str:
        """File version of SLDWORKS.exe, without starting SolidWorks."""
        install = self.install
        return install.version if install is not None else "unknown"

    def probe_state(
        self,
        deep: bool | None = None,
        *,
        prober: Callable[[], dict] | None = None,
        workdir: str | os.PathLike | None = None,
    ) -> dict:
        """Honest availability: ``installed`` / ``reachable`` / ``launchable``.

        ``installed`` is cheap and static.  ``reachable`` (an already-running
        instance answers COM) and ``launchable`` (a COM activation or a direct
        launch produced a server) are only filled by a *deep* probe, because
        answering them means touching CAD.  The fast default therefore reports
        ``None`` -- "not verified", never a guess (report P1-3).

        ``deep=None`` means "use ``$SWNX_SW_PROBE_DEEP``"; ``prober`` is an
        injection point for tests (it must return a connectivity dict).
        """
        install = self.install
        exe_ok = install is not None and Path(install.exe).exists()
        driver_ok = self.driver_path().exists()
        progid_ok = _progid_registered(PROGID)
        state: dict[str, Any] = {
            "installed": bool(exe_ok and driver_ok and progid_ok),
            "reachable": None,
            "launchable": None,
            "last_error": None,
            "note": "",
            "install_detail": {
                "exe": str(install.exe) if install is not None else None,
                "exe_exists": exe_ok,
                "driver": str(self.driver_path()),
                "driver_exists": driver_ok,
                "progid_registered": progid_ok,
            },
        }
        if not state["installed"]:
            missing = [
                name
                for name, ok in (
                    ("SLDWORKS.exe", exe_ok),
                    ("driver", driver_ok),
                    (f"ProgID {PROGID}", progid_ok),
                )
                if not ok
            ]
            state["note"] = (
                "not installed: "
                + ", ".join(missing)
                + " (set SWNX_SW_EXE, restore tools/sw/driver.py, or repair SolidWorks)"
            )
            return state
        want_deep = _env_flag(DEEP_PROBE_ENV, False) if deep is None else bool(deep)
        if not want_deep:
            state["note"] = FAST_PROBE_NOTE
            return state
        try:
            connectivity = prober() if prober is not None else self._deep_probe(workdir=workdir)
        except Exception as exc:  # noqa: BLE001 - probe must report, not raise
            state["last_error"] = f"deep probe failed: {type(exc).__name__}: {exc}"
            state["note"] = "installed; deep probe could not run (see last_error)"
            return state
        state["deep_probe"] = connectivity
        state["reachable"] = connectivity.get("reachable")
        state["launchable"] = connectivity.get("launchable")
        state["last_error"] = connectivity.get("last_error")
        state["note"] = (
            f"deep probe: reachable={state['reachable']} launchable={state['launchable']}"
            + (f" last_error={state['last_error']}" if state["last_error"] else "")
        )
        return state

    def _deep_probe(self, workdir: str | os.PathLike | None = None) -> dict:
        """Run one short driver ``probe`` job and return its connectivity block."""
        budget = _env_float(DEEP_PROBE_TIMEOUT_ENV, DEFAULT_DEEP_PROBE_TIMEOUT)
        options = {"deep": True, "connect_timeout": budget}
        timeout = max(60.0, budget * 4.0)  # a cold start can take a while; the
        # driver itself fails fast when the executable dies or times out.
        try:
            result = self.probe(
                options=options,
                workdir=self.workdir(workdir),
                timeout=timeout,
            )
        except SwnxError as exc:
            return {
                "reachable": None,
                "launchable": None,
                "last_error": getattr(exc, "message", None) or str(exc),
                "deep_probe": {"ok": False, "error": str(exc)},
            }
        raw = result.raw if isinstance(result.raw, dict) else {}
        probe_info = raw.get("probe") if isinstance(raw.get("probe"), dict) else {}
        connectivity = dict(probe_info.get("connectivity") or {})
        connectivity.setdefault("reachable", None)
        connectivity.setdefault("launchable", None)
        connectivity["deep_probe"] = {
            "ok": result.ok,
            "seconds": round(result.duration_s, 2),
            "error": result.error,
            "log": result.log,
        }
        if not result.ok:
            connectivity["last_error"] = connectivity.get("last_error") or result.error
        return connectivity

    def capabilities(self, deep: bool | None = None) -> dict:
        """Backend capabilities plus the honest installed/reachable tri-state.

        ``deep=None`` keeps the fast path unless ``$SWNX_SW_PROBE_DEEP=1``;
        ``deep=True`` runs one short probe job (it may start SolidWorks once, and
        quits anything it started -- use ``keep_app`` for a job that must stay).
        """
        info = super().capabilities()
        state = self.probe_state(deep=deep)
        info.update(state)
        # ``available`` keeps meaning installed for the existing callers, while
        # ``installed``/``reachable``/``launchable``/``last_error``/``note`` say
        # what is actually known (report P1-3).
        info["available"] = bool(state["installed"])
        info["driver"] = str(self.driver_path())
        info["python"] = self.python_executable
        install = self.install
        info["install_exe"] = str(install.exe) if install is not None else None
        return info

    def _command(self, request: Path, workdir: Path) -> list[str]:
        driver = self.driver_path()
        if not driver.exists():
            raise EnvironmentError_(
                "the SolidWorks driver script is missing",
                detail=str(driver),
                hint="restore tools/sw/driver.py from the repository",
            )
        # The request path travels in SWNX_REQUEST (Backend.run sets it), which
        # keeps SOX/quoting issues out of the command line entirely.
        return [self.python_executable, str(driver)]

    def _env(self, workdir: Path) -> dict[str, str]:
        env = super()._env(workdir)
        # Make the source checkout importable by the driver even when the caller
        # did not install the package into that interpreter.
        source_root = str(REPO_ROOT / "src")
        existing = env.get("PYTHONPATH", "")
        if source_root not in existing.split(os.pathsep):
            env["PYTHONPATH"] = source_root + (os.pathsep + existing if existing else "")
        env["SWNX_SW_DRIVER"] = str(self.driver_path())
        if self._exe:
            env["SWNX_SW_EXE"] = str(self._exe)
        elif self.install is not None:
            env["SWNX_SW_EXE"] = str(self.install.exe)
        return env

    # -- job helpers (mirroring the NX backend API) ------------------------- #
    def workdir(self, workdir: str | os.PathLike | None = None) -> Path:
        folder = Path(workdir) if workdir else DEFAULT_WORKDIR
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def run_job(self, job: Job, *, workdir: str | os.PathLike | None = None, timeout: float | None = None) -> JobResult:
        return self.run(job, workdir=self.workdir(workdir), timeout=timeout or self.timeout)

    def probe(
        self,
        path: str | os.PathLike | None = None,
        *,
        options: dict | None = None,
        workdir: str | os.PathLike | None = None,
        timeout: float | None = None,
    ) -> JobResult:
        """Report the live SolidWorks build and exchange capabilities (one start)."""
        # ``path`` is accepted for API parity with NXBackend, whose probe opens a
        # part; the SolidWorks probe needs no document.
        return self.run_job(Job(kind="probe", input=str(path) if path else None, options=dict(options or {})),
                            workdir=workdir, timeout=timeout)

    def read(
        self,
        path: str | os.PathLike,
        *,
        output: str | os.PathLike | None = None,
        options: dict | None = None,
        workdir: str | os.PathLike | None = None,
        timeout: float | None = None,
    ) -> JobResult:
        """Build the neutral IR from a native SolidWorks document (read-only)."""
        return self.run_job(
            Job(
                kind="read",
                input=str(path),
                output=str(output) if output else None,
                options=dict(options or {}),
            ),
            workdir=workdir,
            timeout=timeout,
        )

    def export(
        self,
        path: str | os.PathLike,
        output: str | os.PathLike,
        *,
        format: str = "step",  # noqa: A002 - matches the Job field name
        options: dict | None = None,
        workdir: str | os.PathLike | None = None,
        timeout: float | None = None,
    ) -> JobResult:
        """Export a native part to STEP (``format='step'``) or Parasolid."""
        return self.run_job(
            Job(
                kind="export",
                input=str(path),
                output=str(output),
                format=format,
                options=dict(options or {}),
            ),
            workdir=workdir,
            timeout=timeout,
        )

    def import_(
        self,
        path: str | os.PathLike,
        output: str | os.PathLike,
        *,
        options: dict | None = None,
        workdir: str | os.PathLike | None = None,
        timeout: float | None = None,
    ) -> JobResult:
        """Import a STEP / Parasolid file and save it as a new ``.sldprt``."""
        return self.run_job(
            Job(
                kind="import",
                input=str(path),
                output=str(output),
                options=dict(options or {}),
            ),
            workdir=workdir,
            timeout=timeout,
        )


def _progid_registered(progid: str) -> bool:
    """HKCR\\SldWorks.Application exists (the same test as paths._com_registered)."""
    if sys.platform != "win32":  # pragma: no cover - the product targets Windows
        return False
    try:
        import winreg  # noqa: PLC0415 - Windows-only

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid):
            return True
    except OSError:
        return False
