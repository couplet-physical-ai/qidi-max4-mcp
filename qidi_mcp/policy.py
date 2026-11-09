"""The safety policy: risk tiers, confirmation tickets, interlocks, audit log.

Design goals, in priority order:

1.  **A read is always safe.** Status/query tools never touch this machinery.
2.  **Nothing dangerous happens by accident.** Every mutating tool declares a
    risk tier; anything above LOW needs an explicit ``confirm=True`` from the
    caller, and DESTRUCTIVE actions need a two-step ticket the agent cannot
    guess (it must first call the tool, read the ticket back, and re-call).
3.  **The envelope is enforced numerically, not by prose.** Temperatures and
    coordinates are range-checked against the hardware/safety envelope before a
    single byte reaches Moonraker.
4.  **Live state gates actions.** Interlocks read the printer's real status —
    printing, unhomed, Klipper shut down — and refuse actions that only make
    sense in another state.
5.  **Everything mutating is logged**, with the tier, args, and outcome.

The confirmation model is deliberately one-step for MOTION/THERMAL/PRINT
(``confirm=True``) and two-step for DESTRUCTIVE. A pure two-step handshake on
every heater change would make routine agent work miserable and push users
toward disabling safety altogether; a one-step explicit flag still means the
model had to *decide* to act, which is what we want to gate.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .config import Settings
from .errors import (
    AuditSinkUnwritable,
    ConfirmationRequired,
    InterlockBlocked,
    LimitExceeded,
    QidiError,
    ReadOnlyMode,
    SafetyViolation,
)
from .limits import MAX_GCODE_OFFSET_MM


class Risk(StrEnum):
    """How much damage a tool can do."""

    READ = "read"
    """No side effects at all."""

    LOW = "low"
    """Reversible, non-thermal, non-motion: lights, beeper, fan speed, messages."""

    MOTION = "motion"
    """Moves an axis or the extruder. Can crash the toolhead."""

    THERMAL = "thermal"
    """Changes a heater target. Can burn, or damage filament/parts."""

    PRINT = "print"
    """Starts/stops/alters a print job. Wastes material and time; long-running."""

    DESTRUCTIVE = "destructive"
    """Irreversible or system-level: delete files, write config, restart
    services, reboot the host, firmware actions, calibration that overwrites
    saved values."""

    EMERGENCY = "emergency"
    """Emergency stop. Always permitted, never gated, even in read-only mode."""


#: Tiers that mutate printer state (i.e. blocked by read-only mode).
MUTATING = frozenset({Risk.LOW, Risk.MOTION, Risk.THERMAL, Risk.PRINT, Risk.DESTRUCTIVE})

#: Tiers needing an explicit confirm=True from the caller.
NEEDS_CONFIRM = frozenset({Risk.MOTION, Risk.THERMAL, Risk.PRINT, Risk.DESTRUCTIVE})

#: Tiers needing the two-step ticket handshake.
NEEDS_TICKET = frozenset({Risk.DESTRUCTIVE})


@dataclass
class Ticket:
    """A short-lived, single-use confirmation token for a destructive action."""

    token: str
    tool: str
    fingerprint: str
    created: float
    ttl: float

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > self.ttl


class ConfirmationBroker:
    """Issues and redeems one-time tickets for destructive actions."""

    #: Hard cap on live tickets. A ticket is ~200 bytes and only one is normally
    #: outstanding; a flood means a buggy or hostile client, and an unbounded dict
    #: would grow without limit while _sweep() got slower on every issue.
    MAX_TICKETS = 256

    def __init__(self, ttl: float = 120.0) -> None:
        # Defence in depth: config validates this, but the broker's whole promise
        # is that tickets are short-lived, so it refuses a TTL that would break
        # that rather than minting immortal tickets.
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError(f"confirmation TTL must be a positive finite number, got {ttl!r}")
        self.ttl = ttl
        self._tickets: dict[str, Ticket] = {}

    @staticmethod
    def fingerprint(tool: str, args: Mapping[str, Any]) -> str:
        """Stable identity of a call, so a ticket cannot be reused for other args."""
        material = {k: v for k, v in args.items() if k not in {"confirm", "confirm_token"}}
        blob = json.dumps({"tool": tool, "args": material}, sort_keys=True, default=str)
        return blob

    def issue(self, tool: str, args: Mapping[str, Any]) -> Ticket:
        self._sweep()
        if len(self._tickets) >= self.MAX_TICKETS:
            # Drop the oldest outstanding ticket rather than growing without
            # bound. Losing a stale unconfirmed ticket is harmless -- the caller
            # simply gets a fresh one -- whereas unbounded growth is not.
            oldest = min(self._tickets.values(), key=lambda t: t.created)
            self._tickets.pop(oldest.token, None)
        ticket = Ticket(
            token=f"qidi-{secrets.token_urlsafe(9)}",
            tool=tool,
            fingerprint=self.fingerprint(tool, args),
            created=time.monotonic(),
            ttl=self.ttl,
        )
        self._tickets[ticket.token] = ticket
        return ticket

    def redeem(self, token: str, tool: str, args: Mapping[str, Any]) -> None:
        self._sweep()
        ticket = self._tickets.pop(token, None)
        if ticket is None:
            raise ConfirmationRequired(
                "That confirmation token is unknown, already used, or expired.",
                hint="Call the tool again without confirm_token to get a fresh one.",
            )
        if ticket.tool != tool or ticket.fingerprint != self.fingerprint(tool, args):
            raise ConfirmationRequired(
                "That confirmation token was issued for a different action or different "
                "arguments, so it cannot be used here.",
                hint="Re-request confirmation for the exact call you intend to make.",
            )

    def _sweep(self) -> None:
        for tok in [t for t, k in self._tickets.items() if k.expired]:
            self._tickets.pop(tok, None)


#: Sentinel print state used when we genuinely could not determine whether a job
#: is running (e.g. Moonraker answered 503, which happens both when Klipper is in
#: shutdown AND when Moonraker itself restarted while the print carried on).
#: Interlocks must treat it as "possibly printing" rather than "idle".
UNKNOWN_PRINT_STATE = "unknown"


@dataclass
class PrinterState:
    """The slice of live printer state the interlocks care about."""

    klippy_state: str = "ready"          # webhooks.state: ready/startup/shutdown/error
    klippy_message: str = ""
    print_state: str = "standby"         # print_stats.state
    filename: str = ""                   # print_stats.filename
    homed_axes: str = ""                 # toolhead.homed_axes, e.g. "xyz"
    extruder_temp: float = 0.0
    extruder_target: float = 0.0
    min_extrude_temp: float = 135.0
    position: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    @property
    def klippy_ready(self) -> bool:
        return self.klippy_state == "ready"

    @property
    def is_printing(self) -> bool:
        return self.print_state in {"printing"}

    @property
    def is_paused(self) -> bool:
        return self.print_state == "paused"

    @property
    def print_state_known(self) -> bool:
        return self.print_state != UNKNOWN_PRINT_STATE

    @property
    def job_active(self) -> bool:
        """A job may occupy the machine (printing, paused, or state unknown).

        Unknown counts as active on purpose. ``forbid_during_print`` exists to
        stop us wrecking a running print, and the honest answer to "is it
        printing?" after a failed query is "I cannot tell" — which must not be
        rounded down to "no" for tools that reboot the host or restart firmware.
        """
        return self.print_state in {"printing", "paused", UNKNOWN_PRINT_STATE}

    def homed(self, axes: Iterable[str] = "xyz") -> bool:
        have = set(self.homed_axes.lower())
        return set(a.lower() for a in axes) <= have


@dataclass
class Interlocks:
    """Preconditions a tool requires of live printer state."""

    require_klippy_ready: bool = True
    require_homed: tuple[str, ...] = ()
    #: Refuse if a job is printing/paused (unless settings.allow_during_print).
    forbid_during_print: bool = False
    #: Refuse unless a job is printing/paused (e.g. pause/resume/cancel).
    require_job_active: bool = False
    #: Refuse unless the hotend is at/above min_extrude_temp (for extrusion).
    require_extrude_temp: bool = False


def _current_actor() -> str | None:
    """The verified caller's email, when one is bound to this request.

    Imported inside the function on purpose: ``identity`` imports ``Risk`` from
    this module, so a top-level import here would be a cycle. By call time both
    modules are loaded, and the cost is one lookup per audited action.
    """
    from .identity import current_caller

    caller = current_caller()
    return caller.email if caller else None


@dataclass
class AuditEntry:
    ts: float
    tool: str
    risk: str
    args: dict[str, Any]
    outcome: str
    detail: str | None = None
    #: The verified caller, in a shared deployment. ``None`` on a laptop, where the
    #: process owner *is* the actor and recording a self-declared name would add
    #: no information. This is the field that makes the audit log answer "who",
    #: rather than "which laptop said it was who".
    actor: str | None = None
    #: Which printer, when one app drives several. ``None`` for a single printer,
    #: so a laptop's log is unchanged.
    printer: str | None = None


class AuditLog:
    """Append-only JSONL record of every non-read action, plus an in-memory tail."""

    def __init__(
        self,
        path: str | None,
        *,
        tail: int = 200,
        max_bytes: int = 0,
        keep: int = 3,
        require_writable: bool = False,
        printer: str | None = None,
    ) -> None:
        self.path = path
        self.printer = printer or None
        self._tail: list[AuditEntry] = []
        self._max_tail = tail
        #: Rotate at this size; 0 disables rotation entirely.
        self.max_bytes = max_bytes
        self.keep = keep
        if path and require_writable:
            self._assert_writable(path)

    @staticmethod
    def _assert_writable(path: str) -> None:
        """Fail at startup if the sink cannot be written -- loudly, and once.

        ``record()`` swallows OSError on purpose: an audit-log failure must never
        abort a printer operation mid-move. That is right for the machine and wrong
        for the operator, because the two failure modes are indistinguishable from
        outside -- an app with an unwritable path comes up healthy, serves its
        in-memory tail so /api/state looks populated, and durably records nothing.

        On a shared deployment the audit trail is the whole point (it is what makes a
        central server better than everyone's laptop), so there the sink is a
        precondition rather than a nicety. Checked once, at construction, where the
        error can still name the fix -- not at first write, hours later, inside a
        suppressed handler.
        """
        parent = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise AuditSinkUnwritable(
                f"audit log {path!r} is not writable: {exc}. "
                "Identity enforcement is on, which means this deployment is shared and "
                "the audit trail is load-bearing -- refusing to start rather than run "
                "unaudited. Pick a path the service user owns (in many deployment setups "
                "apps run as `app`, so /home/app/... or /opt/<repo>/...), or unset "
                "QIDI_AUDIT_LOG to accept the in-memory-only tail."
            ) from exc

    def _rotate_if_needed(self) -> None:
        """Roll ``audit.jsonl`` to ``.1``, ``.1`` to ``.2``, and drop the oldest.

        Checked *before* appending, so the entry that trips the limit lands in the
        fresh file rather than pushing the old one over it. With ``keep=0`` the log is
        simply truncated, which is still better than growing until the disk is full --
        an audit trail that takes the machine out has defeated its own purpose.
        """
        if not self.path or self.max_bytes <= 0:
            return
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return  # not there yet, or unreadable; the append below will report it

        if self.keep <= 0:
            with contextlib.suppress(OSError):
                os.remove(self.path)
            return

        with contextlib.suppress(OSError):
            os.remove(f"{self.path}.{self.keep}")
        for index in range(self.keep - 1, 0, -1):
            with contextlib.suppress(OSError):
                os.replace(f"{self.path}.{index}", f"{self.path}.{index + 1}")
        with contextlib.suppress(OSError):
            os.replace(self.path, f"{self.path}.1")

    def record(
        self,
        tool: str,
        risk: Risk,
        args: Mapping[str, Any],
        outcome: str,
        detail: str | None = None,
        *,
        app_wide: bool = False,
    ) -> None:
        """Append one entry. ``app_wide`` leaves out the printer, for actions such
        as role changes that are about the app rather than any one machine."""
        entry = AuditEntry(
            ts=time.time(),
            tool=tool,
            risk=risk.value,
            args={k: _redact(k, v) for k, v in args.items()},
            outcome=outcome,
            detail=detail,
            actor=_current_actor(),
            printer=None if app_wide else self.printer,
        )
        self._tail.append(entry)
        if len(self._tail) > self._max_tail:
            del self._tail[: len(self._tail) - self._max_tail]
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry), default=str) + "\n")
        except OSError:
            # Never let audit-logging failure break a printer operation.
            pass

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        return [asdict(e) for e in self._tail[-n:]]


def _redact(key: str, value: Any) -> Any:
    if any(s in key.lower() for s in ("token", "key", "password", "secret")):
        return "<redacted>"
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + f"...<{len(value) - 500} more bytes>"
    return value


class SafetyPolicy:
    """Single decision point for 'may this call proceed?'."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.broker = ConfirmationBroker(ttl=settings.confirm_ttl)
        self.audit = AuditLog(
            settings.audit_log,
            max_bytes=settings.audit_max_bytes,
            keep=settings.audit_keep,
            # Identity enforcement means this is a shared deployment serving several
            # people, which is exactly where "who did that?" has to be answerable.
            # There, an unwritable sink is a startup failure; on a laptop it stays a
            # best-effort convenience.
            require_writable=settings.identity_enforce,
            printer=settings.printer_id,
        )

    # -- gate ---------------------------------------------------------------

    def authorize(
        self,
        tool: str,
        risk: Risk,
        args: Mapping[str, Any],
        *,
        confirm: bool = False,
        confirm_token: str | None = None,
        state: PrinterState | None = None,
        interlocks: Interlocks | None = None,
    ) -> dict[str, Any] | None:
        """Decide whether a call may proceed.

        Returns ``None`` when the call is cleared. Returns a *payload dict* when
        the caller must be handed a confirmation ticket instead of acting.
        Raises a ``SafetyViolation`` subclass when the call is refused outright.
        """
        if risk is Risk.EMERGENCY:
            return None  # never gated — an e-stop must always be reachable

        if risk in MUTATING and self.settings.read_only:
            self.audit.record(tool, risk, args, "refused", "read_only_mode")
            raise ReadOnlyMode(
                f"{tool} changes printer state, but this server is running in read-only mode.",
                hint="Restart the MCP server with QIDI_READ_ONLY=0 to allow control.",
            )

        if state is not None and interlocks is not None:
            self._check_interlocks(tool, risk, args, state, interlocks)

        needs_ticket = risk in NEEDS_TICKET or (
            self.settings.strict_confirm and risk in NEEDS_CONFIRM
        )

        if confirm_token and not needs_ticket:
            # A token supplied to a tool that only needs the confirm flag must
            # still be validated, never silently ignored: otherwise a token
            # issued for a destructive tool would appear to be "accepted" here,
            # and an agent could believe one confirmation covers another action.
            self._redeem_logged(confirm_token, tool, risk, args)

        if needs_ticket:
            if confirm_token:
                self._redeem_logged(confirm_token, tool, risk, args)
                # Every gate decision gets a line, including the ones that pass.
                # Without this, an action whose tool raised after authorisation
                # (a network error once the G-code was already sent) left only a
                # 'ticket_issued' entry and no record of the attempt.
                self.audit.record(tool, risk, args, "cleared", "token_redeemed")
                return None
            ticket = self.broker.issue(tool, args)
            self.audit.record(tool, risk, args, "ticket_issued", ticket.token)
            return {
                "ok": False,
                "status": "confirmation_required",
                "risk": risk.value,
                "tool": tool,
                "message": (
                    f"{tool} is a {risk.value} action and needs explicit confirmation. "
                    "Re-call this tool with the confirm_token below, unchanged, to proceed."
                ),
                "confirm_token": ticket.token,
                "expires_in_seconds": int(ticket.ttl),
                "arguments_locked_to": {
                    k: v for k, v in args.items() if k not in {"confirm", "confirm_token"}
                },
            }

        if risk in NEEDS_CONFIRM and not confirm:
            self.audit.record(tool, risk, args, "refused", "confirm_flag_missing")
            raise ConfirmationRequired(
                f"{tool} is a {risk.value} action. Re-call it with confirm=true to proceed.",
                hint=(
                    "This gate exists so a printer never moves, heats, or prints as a "
                    "side effect of an exploratory call."
                ),
                risk=risk.value,
            )
        if risk in MUTATING:
            self.audit.record(
                tool, risk, args, "cleared", "confirm_flag" if confirm else "no_gate"
            )
        return None

    def _redeem_logged(
        self, token: str, tool: str, risk: Risk, args: Mapping[str, Any]
    ) -> None:
        """Redeem a token, recording a rejection before it propagates.

        A rejected redemption is the signature of a replay attempt, a token used
        against different arguments, or an expired ticket — exactly what an audit
        reader wants to see. Letting the exception escape unlogged made those
        invisible.
        """
        try:
            self.broker.redeem(token, tool, args)
        except QidiError as exc:
            self.audit.record(
                tool, risk, args, "refused", f"token_rejected: {exc.code}"
            )
            raise

    # -- interlocks ---------------------------------------------------------

    def _check_interlocks(
        self,
        tool: str,
        risk: Risk,
        args: Mapping[str, Any],
        state: PrinterState,
        lk: Interlocks,
    ) -> None:
        def refuse(msg: str, hint: str | None = None) -> None:
            self.audit.record(tool, risk, args, "refused", msg)
            raise InterlockBlocked(msg, hint=hint)

        if lk.require_klippy_ready and not state.klippy_ready:
            refuse(
                f"Klipper is not ready (state={state.klippy_state!r}"
                + (f": {state.klippy_message}" if state.klippy_message else "")
                + ").",
                hint=(
                    "Read the error with get_printer_status, then use "
                    "restart_firmware once you understand the cause."
                ),
            )

        if lk.require_job_active and not state.job_active:
            refuse(
                f"No print job is active (print state={state.print_state!r}), "
                f"so {tool} does not apply.",
                hint="Use get_job_status to see the current job.",
            )

        if lk.forbid_during_print and state.job_active and not self.settings.allow_during_print:
            refuse(
                f"A print job is {state.print_state} — {tool} is blocked to avoid ruining it.",
                hint=(
                    "Pause or cancel the print first, or start the server with "
                    "QIDI_ALLOW_DURING_PRINT=1 if you accept the risk."
                ),
            )

        if lk.require_homed and not state.homed(lk.require_homed):
            need = "".join(lk.require_homed).upper()
            have = state.homed_axes.upper() or "none"
            refuse(
                f"Axes {need} must be homed first (currently homed: {have}).",
                hint="Call home_axes to home the printer.",
            )

        if lk.require_extrude_temp:
            floor = max(state.min_extrude_temp, 1.0)
            if state.extruder_temp < floor:
                refuse(
                    f"Hotend is {state.extruder_temp:.1f} °C; extrusion needs at least "
                    f"{floor:.0f} °C (Klipper's min_extrude_temp).",
                    hint=f"Call set_temperature heater='extruder' target={floor:.0f} and wait.",
                )

    # -- numeric envelope ---------------------------------------------------

    def check_temperature(self, heater: str, target: float) -> None:
        self.require_finite(f"target temperature for {heater}", target)
        limits = self.settings.safety.heater(heater)
        if limits is None:
            raise SafetyViolation(
                f"Unknown heater {heater!r}.",
                hint="Use get_capabilities to see the heaters this printer exposes.",
            )
        if not limits.contains(target):
            raise LimitExceeded(
                f"{target} °C is outside the allowed range for {heater} "
                f"({limits.min:.0f}–{limits.max:.0f} °C).",
                hint=(
                    "Raise the ceiling with the QIDI_MAX_*_TEMP env vars if the hardware "
                    "genuinely supports it."
                ),
                heater=heater,
                requested=target,
                allowed_min=limits.min,
                allowed_max=limits.max,
            )

    def temperature_needs_confirm(self, heater: str, target: float) -> bool:
        limits = self.settings.safety.heater(heater)
        return bool(limits and target > limits.confirm_above)

    def check_position(self, axis: str, value: float) -> None:
        self.require_finite(f"{axis} position", value)
        hw = self.settings.hardware
        axis_l = axis.lower()
        limits = {"x": hw.x, "y": hw.y, "z": hw.z}.get(axis_l)
        if limits is None:
            raise SafetyViolation(f"Unknown axis {axis!r}; expected X, Y or Z.")
        if axis_l == "z":
            floor = max(limits.min, self.settings.safety.min_jog_z)
            if value < floor:
                raise LimitExceeded(
                    f"Z={value} is below the configured floor of {floor} mm.",
                    hint="Lower QIDI_MIN_JOG_Z only if you know the nozzle will clear the bed.",
                    axis="z",
                    requested=value,
                    allowed_min=floor,
                    allowed_max=limits.max,
                )
        if not limits.contains(value):
            raise LimitExceeded(
                f"{axis.upper()}={value} is outside the machine's travel "
                f"({limits.min}–{limits.max} mm).",
                axis=axis_l,
                requested=value,
                allowed_min=limits.min,
                allowed_max=limits.max,
            )

    @staticmethod
    def require_finite(name: str, value: float) -> float:
        """Refuse NaN and infinity before any comparison is made.

        Every comparison involving NaN is False, so a bound written in the
        natural "refuse if too big" direction (``if abs(v) > cap``) silently
        *approves* NaN — it fails open. ``min(nan, cap)`` is worse: it returns
        NaN, so a clamp hands the bad value straight back. NaN then formats as
        the literal text ``nan``, producing G-code like ``G1 Enan F300``, which
        Klipper's ``get_float()`` parses successfully and feeds into the
        kinematics.

        Checking finiteness once, up front, makes the property hold for every
        guard rather than depending on each comparison being spelled defensively.
        """
        if not math.isfinite(value):
            raise LimitExceeded(
                f"{name} must be a real number, got {value!r}.",
                hint=(
                    "NaN and infinity are refused: they defeat numeric comparisons and "
                    "would reach the printer as literal 'nan'/'inf' in a G-code move."
                ),
                requested=str(value),
            )
        return value

    def check_jog(self, axis: str, distance: float) -> None:
        self.require_finite(f"jog distance for {axis}", distance)
        cap = self.settings.safety.max_jog_distance
        if abs(distance) > cap:
            raise LimitExceeded(
                f"A single jog is capped at {cap} mm; {distance} mm was requested.",
                axis=axis.lower(),
                requested=distance,
                allowed_max=cap,
            )

    def check_extrude(self, distance: float) -> None:
        self.require_finite("extrude distance", distance)
        cap = self.settings.safety.max_extrude_distance
        if abs(distance) > cap:
            raise LimitExceeded(
                f"A single extrude/retract is capped at {cap} mm; {distance} mm was requested.",
                requested=distance,
                allowed_max=cap,
            )

    def check_feedrate(self, feedrate: float | None) -> float | None:
        if feedrate is None:
            return None
        self.require_finite("feedrate", feedrate)
        cap = self.settings.safety.max_move_feedrate
        if feedrate <= 0:
            raise LimitExceeded(f"Feedrate must be positive, got {feedrate}.")
        return min(feedrate, cap)

    def check_kinematic_limit(self, kind: str, value: float, *, command: str = "") -> None:
        """Bound a runtime velocity/accel/square-corner-velocity change.

        ``kind`` is a value of :data:`~qidi_mcp.limits.LIMIT_COMMANDS`. Klipper
        applies these without comparing them to printer.cfg, so the envelope is the
        only ceiling; zero or negative velocity/accel is refused too, because it
        either errors in the firmware or stalls every move that follows.
        """
        label = f"{kind.replace('_', ' ')}{' in ' + command if command else ''}"
        self.require_finite(label, value)
        env = self.settings.safety
        caps = {
            "velocity": (env.max_velocity, "mm/s", "QIDI_MAX_VELOCITY", False),
            "accel": (env.max_accel, "mm/s²", "QIDI_MAX_ACCEL", False),
            "square_corner_velocity": (
                env.max_square_corner_velocity,
                "mm/s",
                "QIDI_MAX_SQUARE_CORNER_VELOCITY",
                True,
            ),
        }
        if kind not in caps:
            raise SafetyViolation(f"Unknown kinematic limit {kind!r}.")
        cap, unit, knob, zero_ok = caps[kind]
        floor_ok = value >= 0 if zero_ok else value > 0
        if not floor_ok or value > cap:
            raise LimitExceeded(
                f"{label} = {value} {unit} is outside the allowed range "
                f"({'0' if zero_ok else '>0'}–{cap:g} {unit}).",
                hint=(
                    f"The ceiling is the printer.cfg value; lower it with {knob}. It "
                    "cannot be raised past what the machine is configured for."
                ),
                limit=kind,
                requested=value,
                allowed_max=cap,
            )

    def check_gcode_offset(self, name: str, value: float, *, command: str = "") -> None:
        """Bound a G-code offset change by the same cap set_z_offset uses."""
        label = f"{name}{' in ' + command if command else ''}"
        self.require_finite(label, value)
        if abs(value) > MAX_GCODE_OFFSET_MM:
            raise LimitExceeded(
                f"{label} = {value} mm is outside the allowed G-code offset "
                f"(±{MAX_GCODE_OFFSET_MM:g} mm).",
                hint="Baby-stepping is normally a few hundredths of a millimetre; "
                "use set_z_offset.",
                requested=value,
                allowed_max=MAX_GCODE_OFFSET_MM,
            )

    def check_fan_speed(self, speed: float) -> float:
        self.require_finite("fan speed", speed)
        if not 0.0 <= speed <= 1.0:
            raise LimitExceeded(
                f"Fan speed is a fraction from 0.0 to 1.0; got {speed}.",
                requested=speed,
                allowed_min=0.0,
                allowed_max=1.0,
            )
        return speed
