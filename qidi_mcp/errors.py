"""Exception hierarchy for qidi-mcp.

Every error that can reach an MCP client is a QidiError subclass carrying a
short machine-readable ``code`` plus a human/agent-readable message and an
optional ``hint`` telling the agent what to do next. Tool wrappers convert
these into structured error payloads rather than tracebacks.
"""

from __future__ import annotations

from typing import Any


class QidiError(Exception):
    """Base class for all qidi-mcp errors."""

    code = "qidi_error"

    def __init__(self, message: str, *, hint: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.extra)
        return payload


# --- connectivity -----------------------------------------------------------


class ConnectionFailed(QidiError):
    code = "connection_failed"


class MoonrakerHTTPError(QidiError):
    """Moonraker answered with a non-2xx status."""

    code = "moonraker_http_error"

    def __init__(self, status: int, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint, status=status)
        self.status = status


class KlippyNotReady(QidiError):
    """Klipper is disconnected, shut down, or in an error state."""

    code = "klippy_not_ready"


# --- safety -----------------------------------------------------------------


class SafetyViolation(QidiError):
    """The requested action is refused by the safety policy."""

    code = "safety_violation"


class ConfirmationRequired(QidiError):
    """A guarded action was called without the required confirmation."""

    code = "confirmation_required"


class ReadOnlyMode(QidiError):
    """The server is running read-only; the action mutates printer state."""

    code = "read_only_mode"


class LimitExceeded(SafetyViolation):
    """A requested value is outside the allowed envelope."""

    code = "limit_exceeded"


class InterlockBlocked(SafetyViolation):
    """A precondition derived from live printer state is not satisfied."""

    code = "interlock_blocked"


class NotSupported(QidiError):
    """The connected printer/Moonraker build does not expose this capability."""

    code = "not_supported"


class InvalidRequest(QidiError):
    code = "invalid_request"


class FileNotFound(QidiError):
    """Moonraker has no such file, or no metadata for it yet.

    Kept apart from ``NotSupported`` on purpose: a 404 from ``/server/files/metadata``
    names a missing *file*, not a missing *route*. Reporting it as "this printer's
    Moonraker build does not provide /server/files/metadata" sends an agent off to
    call get_capabilities instead of checking the file name.
    """

    code = "file_not_found"


class AuditSinkUnwritable(QidiError):
    """The configured audit log cannot be written, on a deployment that requires one.

    Raised at startup, not at first write. Runtime write failures stay fail-open --
    an audit-logging problem must never abort a move in progress -- but a sink that
    was never writable at all is a configuration error, and on a shared deployment
    it silently removes the accountability that made hosting worth doing.
    """

    code = "audit_sink_unwritable"
