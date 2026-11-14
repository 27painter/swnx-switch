"""Background conversion jobs for the MCP layer.

A SolidWorks -> NX conversion takes 30 s – 5 min.  A synchronous tool call gives
the caller nothing to look at for that entire time and no way to tell "still
working" from "hung" (the user report saw a silent 429 s failure).  This module
turns a conversion into a job:

    job_id = manager.start("convert", run, params={...})   # returns immediately
    manager.get(job_id)   # {"state": "running", "stage": "import", "elapsed_s": 21.4}

The worker thread calls back with stage milestones, so a polling client sees
"source_read -> export -> import -> target_read -> validate -> report" instead of
a black box.  Jobs are kept in memory only; the durable record is the run
directory under ``work/``.
"""

from __future__ import annotations

import datetime as _dt
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_JOBS = 25
MAX_AGE_SECONDS = 6 * 3600


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class JobRecord:
    id: str
    kind: str
    params: dict = field(default_factory=dict)
    state: str = "running"  # running | done | failed
    stage: str = "queued"
    stages: list = field(default_factory=list)
    created_at: str = ""
    started_ts: float = 0.0
    finished_ts: float | None = None
    result: dict | None = None
    error: str | None = None

    def snapshot(self) -> dict:
        elapsed = (self.finished_ts or time.time()) - self.started_ts if self.started_ts else 0.0
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "elapsed_s": round(elapsed, 2),
            "stages": list(self.stages),
            "created_at": self.created_at,
            "params": dict(self.params),
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    """Thread-per-job registry; a conversion never blocks the MCP loop."""

    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    # -- public API -------------------------------------------------------- #
    def start(
        self,
        kind: str,
        work: Callable[[Callable[[str, dict | None], None]], dict],
        *,
        params: dict | None = None,
    ) -> str:
        """Start ``work(progress)`` on a daemon thread and return its job id.

        ``work`` must return a JSON-safe dict (the tool payload).  It may raise;
        the exception is captured into ``error`` and the job becomes ``failed``.
        """
        job_id = f"job-{uuid.uuid4().hex[:10]}"
        record = JobRecord(
            id=job_id,
            kind=kind,
            params=dict(params or {}),
            created_at=_now_iso(),
            started_ts=time.time(),
        )
        with self._lock:
            self._prune_locked()
            self._jobs[job_id] = record
            self._order.append(job_id)

        def progress(stage: str, info: dict | None = None) -> None:
            self._record_stage(job_id, stage, info)

        def runner() -> None:
            try:
                result = work(progress)
            except BaseException as exc:  # noqa: BLE001 - a job must not kill the server
                with self._lock:
                    record.state = "failed"
                    record.stage = "failed"
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.finished_ts = time.time()
                return
            with self._lock:
                record.state = "done"
                record.stage = "done"
                record.result = result
                record.finished_ts = time.time()

        threading.Thread(target=runner, name=f"swnx-{job_id}", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return record.snapshot() if record else None

    def list(self) -> list[dict]:
        with self._lock:
            return [self._jobs[job_id].snapshot() for job_id in reversed(self._order) if job_id in self._jobs]

    # -- internals --------------------------------------------------------- #
    def _record_stage(self, job_id: str, stage: str, info: dict | None) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record.state != "running":
                return
            record.stage = stage
            at_s = round(time.time() - record.started_ts, 2)
            # ``delta_s`` saves every caller from doing the subtraction itself
            # (user report suggestion 2); it is the time this stage took, so a
            # 37 s conversion immediately shows which step is the slow one.
            previous = record.stages[-1]["at_s"] if record.stages else 0.0
            record.stages.append(
                {
                    "stage": stage,
                    "at_s": at_s,
                    "delta_s": round(max(0.0, at_s - previous), 2),
                    "info": info or {},
                }
            )

    def _prune_locked(self) -> None:
        cutoff = time.time() - MAX_AGE_SECONDS
        keep: list[str] = []
        for job_id in self._order:
            record = self._jobs.get(job_id)
            if record is None:
                continue
            if record.state != "running" and (record.finished_ts or record.started_ts) < cutoff:
                self._jobs.pop(job_id, None)
                continue
            keep.append(job_id)
        while len(keep) > self._max_jobs:
            oldest = keep.pop(0)
            self._jobs.pop(oldest, None)
        self._order = keep


#: Process-wide manager: the MCP server is one process, one job table.
MANAGER = JobManager()


__all__ = ["JobManager", "JobRecord", "MANAGER"]
