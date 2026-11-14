"""Tool-boundary tests: strict JSON, slim payloads, async job tools.

Regression cover for the user report P0-1 ("conversion succeeded but the tool
returned ``value is not lossless JSON``"): every byte the MCP server writes must
parse under a strict JSON parser that rejects ``NaN``/``Infinity``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
ENV = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRATCH = REPO / "work" / "test-ai-output"

sys.path.insert(0, str(SRC))

from swnx_switch.ai import tools, wire  # noqa: E402


# --------------------------------------------------------------------------- #
# wire helpers
# --------------------------------------------------------------------------- #
def test_sanitize_replaces_non_finite_floats() -> None:
    value = {
        "nan": float("nan"),
        "inf": float("inf"),
        "neg": -float("inf"),
        "ok": 1.5,
        "nested": [float("nan"), {"deep": float("inf")}, 2],
        "text": "x",
        "none": None,
    }
    cleaned = wire.sanitize(value)
    assert cleaned["nan"] is None
    assert cleaned["inf"] is None
    assert cleaned["neg"] is None
    assert cleaned["ok"] == 1.5
    assert cleaned["nested"] == [None, {"deep": None}, 2]
    wire.loads_strict(wire.dumps(cleaned))


def test_dumps_never_emits_nan_tokens() -> None:
    text = wire.dumps({"a": float("nan"), "b": [float("inf")]})
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"a": None, "b": [None]}


def test_loads_strict_rejects_nan() -> None:
    try:
        wire.loads_strict('{"x": NaN}')
    except ValueError:
        return
    raise AssertionError("strict parser must reject NaN")


def test_negative_zero_is_normalised() -> None:
    """The host rejects -0.0 even though it is valid JSON text.

    Real failure (2026-11-14 14:2x): ``swnx_probe {deep: true}`` embedded raw NX
    face axes containing ``-0.0``; ``json.dumps`` wrote ``-0.0`` and the host
    answered ``value is not lossless JSON``.  The old ``dumps`` had a fast path
    that skipped sanitising, so the value survived.
    """
    cleaned = wire.sanitize({"axis": [-0.0, 0.0, 1.0]})
    assert cleaned["axis"][0] == 0.0
    assert str(cleaned["axis"][0]) == "0.0"
    text = wire.dumps({"axis": [-0.0]})
    assert "-0.0" not in text, text
    assert wire.scan(json.loads(text)) == []


def test_unsafe_integer_becomes_string() -> None:
    huge = 2**63 - 1
    cleaned = wire.sanitize({"serial": huge, "count": 7})
    assert cleaned["serial"] == str(huge)
    assert cleaned["count"] == 7
    assert wire.scan(json.loads(wire.dumps({"serial": huge}))) == []


def test_scan_flags_what_the_host_rejects() -> None:
    problems = wire.scan({"a": float("nan"), "b": [-0.0], "c": 2**64, "d": {"e": float("inf")}})
    kinds = {kind for _, kind, _ in problems}
    assert "non-finite float" in kinds
    assert "negative zero" in kinds
    assert "int outside the safe range" in kinds
    assert wire.scan({"ok": [1, 2.5, "x", None, True]}) == []


def test_stage_label_shows_delta_and_backend() -> None:
    """``job_status`` must not make the caller do the subtraction (suggestion 2/3)."""
    assert tools._stage_label({"stage": "export", "at_s": 10.0, "delta_s": 1.6, "info": {"exchange": "parasolid"}}) == (
        "export@10.0s(+1.6s)[exchange=parasolid]"
    )
    assert tools._stage_label({"stage": "preflight", "at_s": 10.1, "delta_s": 10.1, "info": {"backend": "solidworks"}}) == (
        "preflight@10.1s(+10.1s)[backend=solidworks]"
    )


# --------------------------------------------------------------------------- #
# slim payload
# --------------------------------------------------------------------------- #
def _fake_result() -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        verdict="pass",
        source_format="sldprt",
        target_format="prt",
        target=Path("D:/deliver/switch_test1.prt"),
        seconds=34.2,
        paths={
            "target_sha256": "a" * 64,
            "report_md_near": "D:/deliver/switch_test1.validation.md",
            "report_json_near": "D:/deliver/switch_test1.validation.json",
        },
        comparison={
            "verdict": "pass",
            "checks": [
                {
                    "id": "volume",
                    "status": "pass",
                    "actual": float("nan"),  # the value that used to break the wire
                    "expected": 461736.37,
                    "delta": float("inf"),
                    "tolerance": 461.7,
                    "detail": "volume",
                },
                {"id": "feature_tree", "status": "warn", "actual": 2, "expected": 45, "delta": -43},
            ],
        },
        run_dir=Path("D:/work/run1"),
        attempts=[],
        warnings=["one warning"],
        error=None,
    )


def test_slim_payload_is_small_and_strict_json() -> None:
    data = tools._slim_payload(_fake_result())
    wire.loads_strict(wire.dumps(data))  # would raise on a NaN token
    assert data["ok"] is True
    assert data["verdict"] == "pass"
    assert data["target_sha256"] == "a" * 64
    assert data["report_md"].endswith("switch_test1.validation.md")
    # only KEY_CHECKS survive, and the non-finite numbers became null
    assert [check["id"] for check in data["key_checks"]] == ["volume"]
    assert data["key_checks"][0]["actual"] is None
    assert data["key_checks"][0]["delta"] is None
    # the heavy comparison / IR must not be in the payload
    assert "comparison" not in data and "ir" not in data


def test_convert_text_starts_with_expected_difference() -> None:
    data = tools._slim_payload(_fake_result())
    text = tools._convert_text(_fake_result(), data)
    assert text.splitlines()[0] == tools.EXPECTATION
    assert "pass" in text
    assert "参数化特征" in tools.EXPECTATION


# --------------------------------------------------------------------------- #
# server-level: every stdout line must be strict JSON
# --------------------------------------------------------------------------- #
class _Server:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "swnx_switch.ai.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=str(REPO),
            env=ENV,
        )
        self._id = 0

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"server closed stdout while handling {method}"
        # The whole point: a strict parser must accept it.
        return wire.loads_strict(line)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        finally:
            self.proc.wait(timeout=10)


def _call(server: _Server, name: str, arguments: dict) -> dict:
    reply = server.rpc("tools/call", {"name": name, "arguments": arguments})
    assert "result" in reply, reply
    return reply["result"]


def test_nan_in_ir_never_reaches_the_wire() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    reference = json.loads((FIXTURES / "ref_part.json").read_text(encoding="utf-8"))
    reference.setdefault("model", {}).setdefault("mass_properties", {})["volume_mm3"] = float("nan")
    nan_path = SCRATCH / "ref_nan.json"
    nan_path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")  # writes NaN literally

    server = _Server()
    try:
        server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}})
        result = _call(
            server,
            "swnx_compare_parts",
            {
                "reference": str(nan_path),
                "candidate": str(FIXTURES / "candidate_identical.json"),
            },
        )
        assert result["isError"] in (True, False)
        text = result["content"][0]["text"]
        # text is prose, but the structured half must be strict JSON
        wire.loads_strict(json.dumps(result.get("structuredContent"), ensure_ascii=False))
    finally:
        server.close()


def test_convert_start_and_job_status_over_mcp() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    missing = SCRATCH / "does-not-exist.SLDPRT"
    target = SCRATCH / "out.prt"

    server = _Server()
    try:
        server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}})
        started = _call(server, "swnx_convert_start", {"source": str(missing), "target": str(target)})
        assert started["isError"] is False, started
        job_id = started["structuredContent"]["job_id"]
        assert job_id.startswith("job-")

        snapshot = None
        deadline = time.time() + 10
        while time.time() < deadline:
            reply = _call(server, "swnx_job_status", {"job_id": job_id})
            snapshot = reply["structuredContent"]
            if snapshot["state"] != "running":
                break
            time.sleep(0.05)
        assert snapshot is not None
        assert snapshot["state"] == "done", snapshot
        assert snapshot["result"]["ok"] is False
        assert "not found" in (snapshot["result"]["error"] or "")

        unknown = _call(server, "swnx_job_status", {"job_id": "job-nope"})
        assert unknown["isError"] is True
    finally:
        server.close()


def test_every_tool_response_is_lossless_json() -> None:
    """Protocol-level regression for report suggestion 1.

    Every tool that can run without CAD is called through a real MCP subprocess,
    and each response is checked against the host's lossless-JSON rules
    (NaN/Infinity, -0.0, out-of-range integers).  This is the test that would
    have caught the ``swnx_probe {deep:true}`` failure where raw NX face axes
    carried ``-0.0`` into ``structuredContent``.
    """
    SCRATCH.mkdir(parents=True, exist_ok=True)
    reference = json.loads((FIXTURES / "ref_part.json").read_text(encoding="utf-8"))
    reference["model"]["mass_properties"]["volume_mm3"] = float("nan")
    for sample in reference.get("model", {}).get("geometry", {}).get("face_samples", []):
        if isinstance(sample.get("axis"), list) and sample["axis"]:
            sample["axis"][0] = -0.0  # what NX actually returns
    nasty = SCRATCH / "ref_nasty.json"
    nasty.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")

    calls = [
        ("swnx_list_routes", {}),
        ("swnx_probe", {"deep": False}),
        ("swnx_part_brief", {"ir": str(FIXTURES / "ref_part.json")}),
        ("swnx_read_part", {"path": str(SCRATCH / "missing.SLDPRT")}),
        (
            "swnx_compare_parts",
            {"reference": str(nasty), "candidate": str(FIXTURES / "candidate_identical.json")},
        ),
        ("swnx_job_status", {"job_id": "job-does-not-exist"}),
    ]

    server = _Server()
    try:
        server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}})
        for name, arguments in calls:
            reply = server.rpc("tools/call", {"name": name, "arguments": arguments})
            assert "result" in reply or "error" in reply, (name, reply)
            problems = wire.scan(reply, name)
            assert problems == [], f"{name} emitted host-rejected values: {problems[:5]}"
            result = reply.get("result") or {}
            if "content" in result:
                assert isinstance(result["content"], list) and result["content"]
                assert isinstance(result["content"][0]["text"], str)
    finally:
        server.close()


def test_job_status_stage_entries_carry_delta() -> None:
    """Stage entries must include ``delta_s`` so the caller sees the slow step."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    server = _Server()
    try:
        server.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}})
        started = _call(
            server,
            "swnx_convert_start",
            {"source": str(SCRATCH / "nope.SLDPRT"), "target": str(SCRATCH / "nope.prt")},
        )
        job_id = started["structuredContent"]["job_id"]
        snapshot = None
        deadline = time.time() + 10
        while time.time() < deadline:
            reply = _call(server, "swnx_job_status", {"job_id": job_id})
            snapshot = reply["structuredContent"]
            if snapshot["state"] != "running":
                break
            time.sleep(0.05)
        assert snapshot is not None
        for entry in snapshot["stages"]:
            assert "delta_s" in entry, entry
            assert entry["delta_s"] >= 0
            assert isinstance(entry["info"], dict)
        assert wire.scan(snapshot) == []
    finally:
        server.close()


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
