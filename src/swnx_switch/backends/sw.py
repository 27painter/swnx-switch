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
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import Backend, Job, JobResult
from ..errors import EnvironmentError_
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

_UNSET = object()


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
    def available(self) -> bool:
        """True when SLDWORKS.exe exists, the driver exists and COM is registered."""
        install = self.install
        if install is None or not Path(install.exe).exists():
            return False
        if not self.driver_path().exists():
            return False
        return _progid_registered(PROGID)

    def app_version(self) -> str:
        """File version of SLDWORKS.exe, without starting SolidWorks."""
        install = self.install
        return install.version if install is not None else "unknown"

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

    def capabilities(self) -> dict:
        info = super().capabilities()
        info["driver"] = str(self.driver_path())
        info["python"] = self.python_executable
        install = self.install
        info["install_exe"] = str(install.exe) if install is not None else None
        return info

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
