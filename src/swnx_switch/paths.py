"""Locate the CAD installations, the interpreter, and the repo layout.

Nothing here launches a CAD application; the drivers do that. This module only
answers "where is NX / SolidWorks on *this* machine", which is the part that
differs between the author's box and a colleague's.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import EnvironmentError_

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKDIR = REPO_ROOT / "work"
DEFAULT_OUTDIR = REPO_ROOT / "out"

_NX_REGISTRY = r"SOFTWARE\Siemens\NX"
_SW_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def _reg_values(key: str) -> dict[str, str]:
    """Read a registry key into a flat dict; missing registry is not an error."""
    try:
        import winreg  # noqa: PLC0415  (Windows-only, imported lazily)
    except ImportError:  # pragma: no cover - non-Windows
        return {}
    out: dict[str, str] = {}
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | view) as handle:
                count = winreg.QueryInfoKey(handle)[1]
                for i in range(count):
                    name, value, _ = winreg.EnumValue(handle, i)
                    out.setdefault(name, str(value))
        except OSError:
            continue
    return out


def _reg_subkeys(key: str) -> list[str]:
    try:
        import winreg  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return []
    names: list[str] = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | view) as handle:
                count = winreg.QueryInfoKey(handle)[0]
                for i in range(count):
                    name = winreg.EnumKey(handle, i)
                    if name not in names:
                        names.append(name)
        except OSError:
            continue
    return names


@dataclass(frozen=True)
class NxInstall:
    root: Path
    source: str
    nxopen_python: str | None = None

    @property
    def run_journal(self) -> Path:
        return self.root / "NXBIN" / "run_journal.exe"

    @property
    def ugiicmd(self) -> Path:
        return self.root / "UGII" / "ugiicmd.bat"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.root} ({self.source})"


@dataclass(frozen=True)
class SolidWorksInstall:
    exe: Path
    version: str
    source: str

    @property
    def root(self) -> Path:
        return self.exe.parent

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.exe} ({self.source})"


def nx_python_of(root: Path) -> str | None:
    """NX's embedded interpreter major.minor, read from the NXBIN DLL names.

    NX ships ``python310.dll`` (no dot) on 2406 and ``python36.dll`` on 12, so
    both spellings are checked.
    """
    bin_dir = root / "NXBIN"
    for name, version in (
        ("python312.dll", "3.12"),
        ("python311.dll", "3.11"),
        ("python310.dll", "3.10"),
        ("python3.12.dll", "3.12"),
        ("python3.10.dll", "3.10"),
        ("python36.dll", "3.6"),
    ):
        if (bin_dir / name).exists():
            return version
    return None


def find_nx(explicit: str | os.PathLike | None = None) -> NxInstall | None:
    """Resolve the NX installation root.

    Order: explicit argument, ``NX_HOME``, ``UGII_BASE_DIR``, registry, then a
    couple of conventional paths. Returns ``None`` when NX is not installed.
    """
    seen: list[tuple[Path, str]] = []
    if explicit:
        seen.append((Path(explicit), "argument"))
    for env in ("NX_HOME", "UGII_BASE_DIR"):
        value = os.environ.get(env)
        if value:
            seen.append((Path(value), f"${env}"))
    for name in _reg_subkeys(_NX_REGISTRY):
        values = _reg_values(rf"{_NX_REGISTRY}\{name}")
        for key in ("InstallDir", "UGII_BASE_DIR", "BaseDir"):
            if values.get(key):
                seen.append((Path(values[key]), f"registry {name}"))
                break
    for candidate in (r"D:\UG NX2406", r"C:\Program Files\Siemens\NX2406", r"D:\UG-NX12"):
        seen.append((Path(candidate), "convention"))

    for root, source in seen:
        try:
            if (root / "NXBIN" / "run_journal.exe").exists():
                return NxInstall(root=root, source=source, nxopen_python=nx_python_of(root))
        except OSError:
            continue
    return None


def require_nx(explicit: str | os.PathLike | None = None) -> NxInstall:
    install = find_nx(explicit)
    if install is None:
        raise EnvironmentError_(
            "Siemens NX was not found",
            hint="Set NX_HOME to the NX install root (the folder holding NXBIN\\run_journal.exe), "
            "e.g. NX_HOME='D:\\UG NX2406'.",
        )
    return install


def find_solidworks(explicit: str | os.PathLike | None = None) -> SolidWorksInstall | None:
    """Resolve SLDWORKS.exe, preferring an explicit path, then the registry."""
    candidates: list[tuple[Path, str]] = []
    if explicit:
        candidates.append((Path(explicit), "argument"))
    for env in ("SWNX_SLDPRT_EXE", "SOLIDWORKS_EXE"):
        value = os.environ.get(env)
        if value:
            candidates.append((Path(value), f"${env}"))
    for name in _reg_subkeys(_SW_UNINSTALL):
        if "solidworks 20" not in name.lower() or "visualize" in name.lower():
            continue
        values = _reg_values(rf"{_SW_UNINSTALL}\{name}")
        location = values.get("InstallLocation") or values.get("DisplayIcon") or ""
        location = location.strip('"').split(",")[0]
        if not location:
            continue
        base = Path(location)
        for exe in (base / "SLDWORKS.exe", base / "SOLIDWORKS" / "SLDWORKS.exe"):
            if exe.exists():
                candidates.append((exe, f"registry {name}"))
                break
    for candidate in (
        r"D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
        r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
    ):
        candidates.append((Path(candidate), "convention"))

    for exe, source in candidates:
        try:
            if exe.exists():
                version = _file_version(exe)
                return SolidWorksInstall(exe=exe, version=version, source=source)
        except OSError:
            continue
    return None


def require_solidworks(explicit: str | os.PathLike | None = None) -> SolidWorksInstall:
    install = find_solidworks(explicit)
    if install is None:
        raise EnvironmentError_(
            "SolidWorks (SLDWORKS.exe) was not found",
            hint="Set SWNX_SLDPRT_EXE to the full path of SLDWORKS.exe.",
        )
    return install


def _file_version(exe: Path) -> str:
    try:
        from win32api import GetFileVersionInfo  # type: ignore  # noqa: PLC0415
    except Exception:  # pragma: no cover - pywin32 missing
        return "unknown"
    try:
        info = GetFileVersionInfo(str(exe), "\\")
        parts = info.get("FileVersionMS"), info.get("FileVersionLS")
        if parts[0] is None or parts[1] is None:
            return "unknown"
        ms, ls = parts
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # pragma: no cover - unreadable metadata
        return "unknown"


def host_python() -> Path:
    """The host interpreter that runs the SolidWorks driver (needs pywin32)."""
    return Path(sys.executable).resolve()


def ensure_workdir(path: str | os.PathLike | None = None) -> Path:
    workdir = Path(path).resolve() if path else DEFAULT_WORKDIR
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def ensure_outdir(path: str | os.PathLike | None = None) -> Path:
    outdir = Path(path).resolve() if path else DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    return outdir


def describe_environment() -> dict:
    """Everything an agent or a bug report needs about this machine."""
    nx = find_nx()
    sw = find_solidworks()
    return {
        "repo_root": str(REPO_ROOT),
        "python": {"executable": str(host_python()), "version": sys.version.split()[0]},
        "pywin32": _has_pywin32(),
        "nx": None
        if nx is None
        else {
            "root": str(nx.root),
            "source": nx.source,
            "run_journal": str(nx.run_journal),
            "ugiicmd": nx.ugiicmd.exists(),
            "nxopen_python": nx.nxopen_python,
        },
        "solidworks": None
        if sw is None
        else {"exe": str(sw.exe), "version": sw.version, "source": sw.source, "com_registered": _com_registered()},
        "workdir": str(DEFAULT_WORKDIR),
        "outdir": str(DEFAULT_OUTDIR),
    }


def _has_pywin32() -> bool:
    try:
        import win32com.client  # noqa: F401,PLC0415
    except Exception:
        return False
    return True


def _com_registered() -> bool:
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application"):
            return True
    except OSError:
        return False
