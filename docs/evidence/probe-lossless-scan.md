# `swnx_probe {deep:true}` lossless-JSON failure — reproduction and fix (0.2.1)

Second real-machine usage report (2026-11-14 14:2x) observed:

```
tool "mcp__swnx-switch__swnx_probe" returned invalid output: value is not lossless JSON
```

The conversion itself passed, so this was purely the tool *return value* boundary.

## How it was reproduced

`work/verify/probe_wire_scan.py` drives the real MCP server over stdio, calls
`swnx_probe` with `deep=false` and `deep=true`, saves the exact response line to
`work/verify/probe_*_response.json`, and scans every value against the host's
lossless-JSON rules (NaN / Infinity / `-0.0` / integers outside ±2^53).

## Before the fix (0.2.0)

```
probe_deep: 51123 bytes
  HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[0].axis[1] = -0.0
  HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[1].axis[2] = -0.0
  HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[2].axis[0] = -0.0
  HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[2].axis[1] = -0.0
  HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[4].axis[2] = -0.0
  HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[5].axis[0] = -0.0
```

Root cause: the deep probe embeds the NX driver's raw probe payload, whose face
samples contain `-0.0` axes. `-0.0` is *valid JSON text* (`json.dumps` writes
`-0.0`), so the old `wire.dumps` "serialise first, sanitise only on failure" fast
path never ran the sanitiser; the host rejects `-0` because a JS number would
normalise it (`Object.is(value, -0)`).

## After the fix (0.2.1)

```
probe_shallow: 4099 bytes  -> structuredContent: clean for the host's lossless rules
probe_deep:    51115 bytes -> structuredContent: clean for the host's lossless rules
```

The payload shrank by 8 bytes: six `-0.0` occurrences became `0.0`.

## Fix

* `wire.sanitize` folds `-0.0` to `0.0`, turns integers beyond ±2^53 into their
  decimal string, stringifies non-string mapping keys, and still maps
  non-finite floats to `null`.
* `wire.dumps` always sanitises before serialising (the fast path is gone).
* `wire.scan` exposes the host rule so tests and this script share one definition.
* Regression tests: `tests/test_ai_output.py::test_negative_zero_is_normalised`,
  `test_unsafe_integer_becomes_string`, `test_scan_flags_what_the_host_rejects`,
  and the protocol-level `test_every_tool_response_is_lossless_json`.