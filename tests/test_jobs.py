"""Job manager tests — no CAD, no subprocess.

The background job path is what stops a 1–5 minute conversion from being a black
box (user report P1-1); these tests cover the state machine, stage recording and
failure capture.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from swnx_switch.jobs import JobManager  # noqa: E402


def _wait(manager: JobManager, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    snapshot = manager.get(job_id)
    while snapshot and snapshot["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
        snapshot = manager.get(job_id)
    assert snapshot is not None
    return snapshot


def test_job_records_stages_and_result() -> None:
    manager = JobManager()

    def work(progress):
        progress("source_read", {"app": "solidworks"})
        progress("export", {"exchange": "parasolid", "ok": True})
        return {"ok": True, "verdict": "pass", "seconds": 1.5}

    job_id = manager.start("convert", work, params={"source": "a.sldprt"})
    snapshot = _wait(manager, job_id)
    assert snapshot["state"] == "done"
    assert snapshot["stage"] == "done"
    assert snapshot["result"]["verdict"] == "pass"
    assert [stage["stage"] for stage in snapshot["stages"]] == ["source_read", "export"]
    assert snapshot["elapsed_s"] >= 0
    assert snapshot["params"]["source"] == "a.sldprt"


def test_job_failure_is_captured_not_raised() -> None:
    manager = JobManager()

    def work(progress):
        progress("export", {"ok": False})
        raise RuntimeError("driver died")

    job_id = manager.start("convert", work)
    snapshot = _wait(manager, job_id)
    assert snapshot["state"] == "failed"
    assert snapshot["stage"] == "failed"
    assert "driver died" in snapshot["error"]
    assert snapshot["result"] is None


def test_unknown_job_returns_none() -> None:
    manager = JobManager()
    assert manager.get("job-does-not-exist") is None


def test_job_list_is_newest_first_and_prunes() -> None:
    manager = JobManager(max_jobs=2)

    def instant(progress):
        return {"ok": True}

    first = manager.start("convert", instant)
    second = manager.start("convert", instant)
    third = manager.start("convert", instant)
    for job_id in (first, second, third):
        _wait(manager, job_id)
    listed = [entry["job_id"] for entry in manager.list()]
    assert listed[:3] == [third, second, first]
    assert len(listed) <= 3


if __name__ == "__main__":  # pragma: no cover
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001 - plain runner
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
