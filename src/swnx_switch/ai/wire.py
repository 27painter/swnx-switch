"""Strict JSON for the tool boundary.

The host validates every tool return value by parsing it as **lossless JSON**:
``NaN``, ``Infinity`` and ``-Infinity`` are not JSON, and a single non-finite
float anywhere inside a nested comparison turns a successful conversion into
``returned invalid output: value is not lossless JSON`` (this happened for real —
see the user report P0-1).

Two rules keep that from recurring:

1. :func:`dumps` always serialises with ``allow_nan=False``; if a value is not
   representable it is sanitised (non-finite float -> ``null``) and serialised
   again, so the host never sees a bare ``NaN`` token.
2. :func:`sanitize` is applied to every payload the MCP server writes, so the
   ``structuredContent`` half cannot smuggle a non-finite number out either.

The validator already cleans its own report path (``validate._clean``); this
module exists so the *tool* path cannot drift from it.
"""

from __future__ import annotations

import json
import math
from typing import Any

MAX_DEPTH = 40


def sanitize(value: Any, _depth: int = 0) -> Any:
    """Return a JSON-safe copy: non-finite floats become ``None``.

    Unknown objects are stringified rather than dropped, because a tool result
    that silently loses a field is harder to debug than one with ``"<Path>"``
    in it.
    """
    if _depth > MAX_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): sanitize(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, _depth + 1) for item in value]
    # Path is the one non-JSON type that shows up in every payload.
    try:
        from pathlib import Path

        if isinstance(value, Path):
            return str(value)
    except Exception:  # pragma: no cover - Path is always importable
        pass
    return str(value)


def dumps(value: Any, **kwargs: Any) -> str:
    """``json.dumps`` that can never emit a NaN/Infinity token."""
    kwargs.setdefault("ensure_ascii", False)
    try:
        return json.dumps(value, allow_nan=False, **kwargs)
    except (TypeError, ValueError):
        return json.dumps(sanitize(value), allow_nan=False, **kwargs)


def parse_constant(token: str) -> Any:
    """``json.loads`` hook that rejects NaN/Infinity instead of accepting them."""
    raise ValueError(f"non-finite JSON token {token!r}")


def loads_strict(text: str) -> Any:
    """Strict parse: the same rule the host applies to tool results."""
    return json.loads(text, parse_constant=parse_constant)


__all__ = ["sanitize", "dumps", "parse_constant", "loads_strict"]
