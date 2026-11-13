"""Exception types shared by every layer.

Every failure carries a ``code`` so the CLI and the agent tool layer can return
something machine-actionable instead of a traceback.
"""

from __future__ import annotations


class SwnxError(Exception):
    """Base class for every swnx-switch failure."""

    code = "swnx_error"

    def __init__(self, message: str, *, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        if self.hint:
            out["hint"] = self.hint
        return out


class EnvironmentError_(SwnxError):
    """A required CAD application / interpreter is missing or not licensed."""

    code = "environment"


class BackendError(SwnxError):
    """A CAD driver ran but did not do what it was asked."""

    code = "backend"


class BackendTimeout(BackendError):
    code = "backend_timeout"


class ConversionError(SwnxError):
    """Native -> exchange -> native conversion failed."""

    code = "conversion"


class ValidationError_(SwnxError):
    """A tolerance check did not pass."""

    code = "validation"


class UnsupportedFormat(SwnxError):
    code = "unsupported_format"


class IRError(SwnxError):
    """The neutral document model is malformed or incomplete."""

    code = "ir"
