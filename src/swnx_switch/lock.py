"""One CAD job at a time per application.

NX and SolidWorks both dislike two automation clients at once (one licence seat,
one COM apartment, one active document).  A CLI run, a DSH tool call and a
teammate's probe can easily overlap, so every driver job takes a per-backend
lock before it spawns anything.

Implemented with ``msvcrt.locking`` on a lock file in the work directory: an
advisory byte-range lock that the kernel drops when the owning process dies, so
a crashed driver cannot leave a stale lock behind.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Iterator

from .errors import BackendTimeout

DEFAULT_LOCK_TIMEOUT = 1800.0


@contextlib.contextmanager
def backend_lock(name: str, *, workdir: str | os.PathLike | None = None, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[Path]:
    """Serialise driver jobs per backend name.

    ``timeout`` is how long a caller waits for a *busy* backend, not how long the
    job may run — a queued job is normal when an agent and a human share the
    machine.
    """
    directory = Path(workdir) if workdir else Path(os.environ.get("SWNX_WORKDIR") or "work")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f".lock-{name}"
    handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            import msvcrt  # noqa: PLC0415

            while True:
                try:
                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise BackendTimeout(
                            f"{name} is busy: another job has held the lock for {timeout:.0f}s",
                            hint=f"wait for the running job, or delete {path} if you are sure nothing is running",
                        ) from None
                    time.sleep(0.5)
        else:  # pragma: no cover - the product only targets Windows
            acquired = True
        yield path
    finally:
        if acquired and sys.platform == "win32":  # pragma: no cover
            try:
                import msvcrt  # noqa: PLC0415

                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(handle)
