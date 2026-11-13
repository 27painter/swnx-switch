"""Siemens NX backend — batch NX driven by ``NXBIN\\run_journal.exe``.

Why one NX process per job
--------------------------
NX has no COM server and no embeddable host interpreter: the only unattended
entry point is ``run_journal.exe``, which starts a *separate* batch NX, runs one
``.py`` journal inside NX's own embedded Python (3.10 on NX 2406) and exits.  On
this machine that exit code is ``0xC0000409`` even for a clean shutdown, so the
verdict comes from the result file and the log (handled by
:meth:`swnx_switch.backends.base.Backend.run`); this module only has to start the
process correctly and describe the install.

The command line is the fragile part
------------------------------------
``ugiicmd.bat`` sets the licence/PATH/``UGII_*`` environment, and it must run in
the same ``cmd.exe`` that then starts ``run_journal.exe``::

    cmd.exe /d /s /c ""<NX_HOME>\\UGII\\ugiicmd.bat" "<NX_HOME>" && "<NX_HOME>\\NXBIN\\run_journal.exe" "<journal>"

``/s`` makes ``cmd`` strip the *first and last* quote of the ``/c`` argument, so
the doubled leading quote is intentional.  Python's ``subprocess`` quotes a
*sequence* with :func:`subprocess.list2cmdline`, which escapes the embedded
quotes as ``\\"`` — ``cmd.exe`` then fails with "The filename, directory name, or
volume label syntax is incorrect" before NX ever starts.  The fix (same one
dsh-nx uses through ``windowsVerbatimArguments``) is to hand ``Popen`` a single
already-quoted *string*: with ``shell=False`` Windows passes it to
``CreateProcess`` verbatim.  :meth:`NXBackend._command` therefore returns a
``str`` although the base class annotates ``list[str]``.
"""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path

from ..errors import EnvironmentError_
from ..paths import REPO_ROOT, NxInstall, find_nx, require_nx
from .base import Backend, Job, JobResult

#: The one journal that runs inside NX.  It is version-controlled next to the
#: backend so an installed wheel still finds it (``REPO_ROOT``) and so a
#: developer can point ``SWNX_NX_JOURNAL`` at an experimental copy.
DRIVER_PATH = REPO_ROOT / "tools" / "nx" / "driver.py"

#: Where run artefacts land when a caller does not pass a workdir.
DEFAULT_WORKDIR = REPO_ROOT / "work" / "nx"

#: NX batch cold start is 30-90 s; STEP translation of a real part adds to it.
DEFAULT_TIMEOUT = 900.0


# --------------------------------------------------------------------------- #
# install / version discovery (never starts NX)
# --------------------------------------------------------------------------- #
def _file_version(path: Path) -> str | None:
    """Product version of a PE file, e.g. ``2406.1700.0.0``.

    Same approach as dsh-nx: read the VS_FIXEDFILEINFO block through
    ``version.dll`` with ctypes instead of pulling in pywin32 (the NX driver must
    not depend on the host having pywin32 installed).
    """
    if sys.platform != "win32":  # pragma: no cover - NX is Windows only
        return None
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return None
        fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
        ms, ls = fixed[2], fixed[3]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:  # pragma: no cover - unreadable metadata is not an error
        return None


def release_of(version: str | None) -> str | None:
    """``'2406.1700.0.0'`` -> ``'2406'``; ``'12.0.0.27'`` -> ``'12.0'``."""
    if not version:
        return None
    parts = version.split(".")
    if parts[0] == "12":
        return ".".join(parts[:2])
    return parts[0]


def _release_from_root_name(root: Path) -> str | None:
    """Last-resort release label from the install folder, e.g. ``UG NX2406``."""
    match = re.search(r"(?:NX|UG)[\s_-]*(\d{2,4})", root.name, re.IGNORECASE)
    if not match:
        return None
    digits = match.group(1)
    return "12.0" if digits == "12" else digits


@lru_cache(maxsize=8)
def _version_for_root(root: str) -> str:
    """Release string for an install root; cached because it is read a lot."""
    root_path = Path(root)
    for exe in ("ugraf.exe", "run_journal.exe"):
        candidate = root_path / "NXBIN" / exe
        if candidate.exists():
            release = release_of(_file_version(candidate))
            if release:
                return release
    return _release_from_root_name(root_path) or "unknown"


class NXBackend(Backend):
    """Drive Siemens NX through a batch journal.

    Nothing here launches NX except :meth:`run` (via the base class), so
    :meth:`available` / :meth:`capabilities` are cheap enough to call from a CLI
    start-up path.
    """

    name = "nx"
    app = "Siemens NX"
    #: NX parts are the only native format NX owns; everything else is exchange.
    supports_native = ("prt",)
    #: Importer builders that actually exist in NX 2406's ``DexManager``
    #: (authoritative list from ``NXBIN\\managed\\NXOpen.xml``): STEP 203/214/242
    #: and Parasolid.  There is no SolidWorks importer, so ``.sldprt`` reaches NX
    #: only through a STEP/Parasolid round trip done by the SolidWorks backend.
    supports_import = ("step", "parasolid")

    def __init__(
        self,
        root: str | os.PathLike | None = None,
        driver: str | os.PathLike | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._root = Path(root) if root else None
        self._driver = Path(driver) if driver else None
        self.timeout = float(timeout)

    # -- install ----------------------------------------------------------- #
    @property
    def install(self) -> NxInstall | None:
        return find_nx(self._root)

    def require_install(self) -> NxInstall:
        return require_nx(self._root)

    def available(self) -> bool:
        install = self.install
        if install is None:
            return False
        try:
            return install.run_journal.exists() and self.driver_path().exists()
        except OSError:  # pragma: no cover - unreadable install
            return False

    def app_version(self) -> str:
        install = self.install
        if install is None:
            return "unknown"
        return _version_for_root(str(install.root))

    def driver_path(self) -> Path:
        override = os.environ.get("SWNX_NX_JOURNAL")
        if self._driver:
            return self._driver
        if override:
            return Path(override)
        return DRIVER_PATH

    # -- process construction ---------------------------------------------- #
    def _command(self, request: Path, workdir: Path) -> str:  # type: ignore[override]
        """Full command line, quoted for ``cmd.exe`` (see module docstring).

        Returns a *string* on purpose: ``Popen`` passes a string to
        ``CreateProcess`` verbatim, while it would re-quote a list.
        """
        install = self.require_install()
        journal = str(self.driver_path())
        exe = _quote(install.run_journal)
        if install.ugiicmd.exists():
            inner = '%s %s && %s %s' % (
                _quote(install.ugiicmd),
                _quote(install.root),
                exe,
                _quote(journal),
            )
            return 'cmd.exe /d /s /c "%s"' % inner
        # Without ugiicmd.bat the NX environment has to come from the caller's
        # environment; run_journal.exe directly is the honest fallback.
        return "%s %s" % (exe, _quote(journal))

    def _env(self, workdir: Path) -> dict[str, str]:
        env = super()._env(workdir)
        install = self.install
        workdir = Path(workdir)
        profile = workdir / ".nx-profile"
        tmp = workdir / ".nx-tmp"
        for folder in (profile, tmp):
            folder.mkdir(parents=True, exist_ok=True)
        if install is not None:
            env["NX_HOME"] = str(install.root)
            env["UGII_BASE_DIR"] = str(install.root)
        # Both default to %LOCALAPPDATA%\Siemens and %TEMP%; pointing them at the
        # workdir keeps an unattended run out of the user's NX profile and makes
        # runs reproducible.  (They are not sufficient on their own where a file
        # sandbox also denies NX's licence/AppData reads -- that is an execution
        # environment constraint, not something the driver works around.)
        env["UGII_USER_PROFILE_DIR"] = str(profile.resolve())
        env["UGII_TMP_DIR"] = str(tmp.resolve())
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # The journal reports the release in the IR; passing the fast, host-side
        # detection avoids a second NX API guess inside the journal.
        env["SWNX_NX_RELEASE"] = self.app_version()
        # The journal adds ../../src to sys.path itself; PYTHONPATH is a second
        # chance so `import swnx_switch.ir` works even from a copied journal.
        pythonpath = env.get("PYTHONPATH", "")
        src = str(REPO_ROOT / "src")
        if src not in pythonpath.split(os.pathsep):
            env["PYTHONPATH"] = src + (os.pathsep + pythonpath if pythonpath else "")
        return env

    # -- job helpers ------------------------------------------------------- #
    def workdir(self, workdir: str | os.PathLike | None = None) -> Path:
        folder = Path(workdir) if workdir else DEFAULT_WORKDIR
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def probe(self, path=None, *, options: dict | None = None, workdir=None, timeout=None) -> JobResult:
        """Ask the journal to report the live NX environment (one NX start).

        ``path`` opens that part for the read-API part of the probe; without it
        the probe reports the session/DEX capabilities only.
        """
        return self.run_job(
            Job(kind="probe", input=str(path) if path else None, options=dict(options or {})),
            workdir=workdir,
            timeout=timeout,
        )

    def read(self, path: str | os.PathLike, *, output=None, workdir=None, timeout=None) -> JobResult:
        """Build the neutral IR from a native ``.prt``.

        The input part is opened read-only and never saved; when ``output`` is
        given the journal also writes the IR JSON there.
        """
        return self.run_job(
            Job(kind="read", input=str(path), output=str(output) if output else None),
            workdir=workdir,
            timeout=timeout,
        )

    def export(
        self,
        path: str | os.PathLike,
        output: str | os.PathLike,
        *,
        format: str = "step",
        options: dict | None = None,
        workdir=None,
        timeout=None,
    ) -> JobResult:
        """Export a ``.prt`` to STEP (``format='step'``) or Parasolid."""
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
        workdir=None,
        timeout=None,
    ) -> JobResult:
        """Import a STEP / Parasolid file and save it as a new NX ``.prt``."""
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

    def run_job(self, job: Job, *, workdir=None, timeout: float | None = None) -> JobResult:
        folder = self.workdir(workdir)
        return self.run(job, workdir=folder, timeout=timeout or self.timeout)


def _quote(value: str | os.PathLike) -> str:
    return '"%s"' % str(value)


def probe_environment() -> dict:
    """Cheap, NX-free description of the NX install (for ``describe_environment``)."""
    backend = NXBackend()
    install = backend.install
    if install is None:
        return {"available": False, "reason": "run_journal.exe not found"}
    return {
        "available": backend.available(),
        "root": str(install.root),
        "source": install.source,
        "run_journal": str(install.run_journal),
        "ugiicmd": install.ugiicmd.exists(),
        "release": backend.app_version(),
        "nxopen_python": install.nxopen_python,
        "driver": str(backend.driver_path()),
    }


__all__ = [
    "NXBackend",
    "DEFAULT_TIMEOUT",
    "DEFAULT_WORKDIR",
    "DRIVER_PATH",
    "probe_environment",
    "release_of",
]
