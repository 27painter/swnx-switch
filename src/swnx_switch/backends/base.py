"""Driver protocol: one subprocess per CAD job, JSON request in / JSON out.

Why subprocesses and not in-process calls
-----------------------------------------
* NX has no COM server.  It is driven by ``run_journal.exe`` in a *batch* NX
  session, so one job == one NX process (slow, but crash-isolated).
* SolidWorks is a COM server, and touching it from the same process that is
  converting files makes a crashed COM call take the whole converter down.  The
  driver also needs a different interpreter/lifetime than the caller.
* A file protocol is debuggable: the request, the log and the result stay on
  disk after a failure, which is exactly what an agent needs to retry.

Wire protocol
-------------
Caller writes ``<workdir>/request-<kind>-<uuid>.json``::

    {"kind": "read"|"probe"|"export"|"import"|"save"|"convert",
     "input": "<native path>", "output": "<path>", "format": "step",
     "options": {...}}

and exports its path as ``SWNX_REQUEST``; the driver must write the result to
``SWNX_RESULT`` in this shape::

    {"ok": true, "kind": "read", "backend": "nx", "app": "Siemens NX 2406",
     "app_version": "2406", "outputs": ["..."], "ir": {...}, "warnings": [...],
     "error": null}

Exit codes are only a hint — NX batch exits 0xC0000409 on a clean shutdown, so
the verdict is the result file plus the log tokens (same rule as dsh-nx).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import BackendError, BackendTimeout, EnvironmentError_, UnsupportedFormat

NATIVE_FORMATS = {
    ".sldprt": "sldprt",
    ".slddrw": "slddrw",
    ".sldasm": "sldasm",
    ".prt": "prt",
}

EXCHANGE_FORMATS = {
    ".step": "step",
    ".stp": "step",
    ".x_t": "parasolid",
    ".x_b": "parasolid",
}

STEP_VARIANTS = ("ap203", "ap214", "ap242")

# Tokens that mean the run is bad no matter what the exit code says.
HARD_FAILURES = (
    "General Fault Exception",
    "failed to initialize UFUN",
    "FATAL ERROR",
    "_FAIL",
    "Traceback (most recent call last)",
)


def detect_format(path: str | os.PathLike) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in NATIVE_FORMATS:
        return NATIVE_FORMATS[suffix]
    if suffix in EXCHANGE_FORMATS:
        return EXCHANGE_FORMATS[suffix]
    raise UnsupportedFormat(
        f"unsupported file type: {Path(path).name}",
        hint="supported: .sldprt .slddrw .sldasm .prt | exchange: .step .stp .x_t .x_b",
    )


def format_owner(fmt: str) -> str | None:
    """Which backend owns a format (None for exchange formats)."""
    if fmt in ("sldprt", "slddrw", "sldasm"):
        return "solidworks"
    if fmt == "prt":
        return "nx"
    return None


@dataclass
class Job:
    kind: str  # probe | read | export | import | save | convert
    input: str | None = None
    output: str | None = None
    format: str | None = None  # for export: step | parasolid; for import: native fmt
    options: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "input": self.input,
            "output": self.output,
            "format": self.format,
            "options": self.options,
        }


@dataclass
class JobResult:
    ok: bool
    kind: str
    backend: str
    app: str = ""
    app_version: str = "unknown"
    outputs: list[str] = field(default_factory=list)
    ir: dict | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    log: str | None = None
    exit_code: int | None = None
    raw: dict = field(default_factory=dict)

    def require_ok(self) -> "JobResult":
        if not self.ok:
            raise BackendError(
                self.error or f"{self.backend} {self.kind} job failed",
                detail=self.raw.get("detail") if isinstance(self.raw, dict) else None,
                hint=f"driver log: {self.log}" if self.log else None,
            )
        return self

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "backend": self.backend,
            "app": self.app,
            "app_version": self.app_version,
            "outputs": self.outputs,
            "warnings": self.warnings,
            "error": self.error,
            "duration_s": self.duration_s,
            "log": self.log,
            "exit_code": self.exit_code,
            "ir": self.ir,
        }


class Backend(ABC):
    """Base driver.  Subclasses provide the command line and the environment."""

    name: str = "backend"
    app: str = "unknown"
    supports_native: tuple[str, ...] = ()
    supports_import: tuple[str, ...] = ()

    # -- to implement ------------------------------------------------------ #
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def app_version(self) -> str: ...

    @abstractmethod
    def _command(self, request: Path, workdir: Path) -> list[str]:
        """Full argv of the driver process (request path travels in the env)."""

    def _env(self, workdir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "SWNX_BACKEND": self.name,
            }
        )
        return env

    def capabilities(self) -> dict:
        return {
            "backend": self.name,
            "app": self.app,
            "app_version": self.app_version() if self.available() else None,
            "available": self.available(),
            "native_formats": list(self.supports_native),
            "import_formats": list(self.supports_import),
        }

    # -- shared runner ----------------------------------------------------- #
    def run(self, job: Job, *, workdir: Path, timeout: float = 600.0) -> JobResult:
        """Run one job in a driver subprocess, serialised per backend.

        The lock is taken around the whole spawn-and-wait: two agents driving NX
        at once is one licence seat and two batch sessions fighting over it.
        """
        from ..lock import backend_lock  # noqa: PLC0415 - keeps base import light

        with backend_lock(self.name, workdir=workdir):
            return self._run_locked(job, workdir=Path(workdir), timeout=timeout)

    def _run_locked(self, job: Job, *, workdir: Path, timeout: float = 600.0) -> JobResult:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex[:8]
        stem = f"{job.kind}-{token}"
        request = workdir / f"request-{stem}.json"
        result = workdir / f"result-{stem}.json"
        log = workdir / f"log-{stem}.txt"
        request.write_text(json.dumps(job.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        command = self._command(request, workdir)
        env = self._env(workdir)
        env.update(
            {
                "SWNX_REQUEST": str(request),
                "SWNX_RESULT": str(result),
                "SWNX_LOG": str(log),
                "SWNX_WORKDIR": str(workdir),
            }
        )

        started = time.time()
        exit_code: int | None = None
        timed_out = False
        with open(log, "ab") as log_handle:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(workdir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                raise EnvironmentError_(
                    f"cannot start the {self.name} driver",
                    detail=f"{command[0]}: {exc}",
                ) from exc
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(process.pid)
                try:
                    exit_code = process.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                    exit_code = None

        duration = round(time.time() - started, 2)
        log_text = _read_text(log)
        payload: dict[str, Any] = {}
        if result.exists():
            try:
                payload = json.loads(result.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                payload = {"ok": False, "error": f"driver wrote invalid result JSON: {exc}"}

        if timed_out:
            raise BackendTimeout(
                f"{self.name} {job.kind} timed out after {timeout:.0f}s",
                detail=_tail(log_text),
                hint=f"driver log: {log}",
            )

        if not payload:
            hint = (
                f"driver log: {log}"
                if exit_code is not None
                else "the driver process was not started"
            )
            raise BackendError(
                f"{self.name} {job.kind} produced no result file (exit {exit_code})",
                detail=_tail(log_text),
                hint=hint,
            )

        ok = bool(payload.get("ok"))
        error = payload.get("error")
        if ok:
            for token_ in HARD_FAILURES:
                if token_ in log_text and not payload.get("allow_failure_tokens"):
                    ok = False
                    error = f"driver log contains {token_!r}"
                    break

        job_result = JobResult(
            ok=ok,
            kind=payload.get("kind", job.kind),
            backend=payload.get("backend", self.name),
            app=payload.get("app", self.app),
            app_version=str(payload.get("app_version", self.app_version())),
            outputs=[str(p) for p in payload.get("outputs") or []],
            ir=payload.get("ir"),
            warnings=[str(w) for w in payload.get("warnings") or []],
            error=None if ok else (error or _tail(log_text) or "unknown driver error"),
            duration_s=duration,
            log=str(log),
            exit_code=exit_code,
            raw=payload,
        )
        return job_result


class BackendRegistry:
    """Lazily constructed backends, keyed by the formats they own."""

    def __init__(self) -> None:
        self._cache: dict[str, Backend] = {}

    def get(self, name: str) -> Backend:
        if name not in self._cache:
            if name == "nx":
                from .nx import NXBackend  # noqa: PLC0415 - avoid an import cycle

                self._cache[name] = NXBackend()
            elif name == "solidworks":
                from .sw import SolidWorksBackend  # noqa: PLC0415

                self._cache[name] = SolidWorksBackend()
            else:
                raise UnsupportedFormat(f"unknown backend {name!r}")
        return self._cache[name]

    def for_format(self, fmt: str) -> Backend:
        owner = format_owner(fmt)
        if owner is None:
            raise UnsupportedFormat(f"{fmt!r} is not a native CAD format")
        return self.get(owner)

    def for_path(self, path: str | os.PathLike) -> Backend:
        return self.for_format(detect_format(path))

    def describe(self) -> list[dict]:
        out = []
        for name in ("nx", "solidworks"):
            try:
                out.append(self.get(name).capabilities())
            except Exception as exc:  # a broken install must not hide the other one
                out.append({"backend": name, "available": False, "error": str(exc)})
        return out


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _read_text(path: Path, limit: int = 400_000) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > limit:
        data = data[-limit:]
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _tail(text: str, lines: int = 12) -> str:
    stripped = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(stripped[-lines:])


def _kill_tree(pid: int) -> None:
    """NX spawns ugraf.exe and friends; killing the shell alone leaks licence seats."""
    if sys.platform != "win32":  # pragma: no cover
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def require_pywin32() -> None:
    try:
        import win32com.client  # noqa: F401,PLC0415
    except Exception as exc:  # pragma: no cover - Windows only
        raise EnvironmentError_(
            "the SolidWorks driver needs pywin32",
            detail=str(exc),
            hint="python -m pip install pywin32",
        ) from exc


def which_python() -> str:
    return sys.executable or shutil.which("python") or "python"
