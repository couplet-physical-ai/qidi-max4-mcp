"""Shared runtime: the one object every tool talks to.

Holds the Moonraker client, the safety policy, and the capability cache, and
provides the three things every mutating tool needs:

*   ``authorize()`` — run the safety gate; returns a ticket payload to hand
    straight back to the caller, or ``None`` when the call is cleared.
*   ``state()`` — a briefly-cached snapshot of live printer state for interlocks.
*   ``ok()`` / ``guarded()`` — consistent success envelopes and error handling.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar, get_type_hints

from .capabilities import Capabilities, CapabilityCache
from .client import MoonrakerClient
from .config import Settings
from .errors import KlippyNotReady, QidiError
from .identity import IdentityRequired, RoleRequired, current_caller
from .lease import LeaseManager, LeaseRequired, _held_error
from .policy import (
    MUTATING,
    UNKNOWN_PRINT_STATE,
    Interlocks,
    PrinterState,
    Risk,
    SafetyPolicy,
)

T = TypeVar("T")

#: Tools the lease never blocks.
#:
#: The three aborts are the machine's escape hatches, and an escape hatch that can
#: be locked by another agent is not one -- if a hotend is climbing wrongly, whose
#: turn it is stops being interesting. The lease tools themselves are here because a
#: lease you cannot inspect or break is a lease that strands the printer when its
#: holder goes home. Pausing the job queue is exempt too -- handled in
#: :func:`lease_exempt`, since it shares a tool with the un-exempt "start" -- because
#: it only ever prevents a *future* automatic start: stopping the conveyor belt is the
#: safe direction, the same reason turning hardware off is never refused on capability
#: grounds.
#:
#: Deliberately NOT exempt: ``cooldown`` and ``cancel_print``. Both are harmless to
#: the machine and ruinous to somebody else's eight-hour print, which is exactly the
#: takeover this is here to prevent. ``abort_heating`` remains available for the
#: genuine thermal emergency.
LEASE_EXEMPT_TOOLS = frozenset(
    {
        "emergency_stop",
        "abort_heating",
        "abort_macro",
        "acquire_printer_lease",
        "renew_printer_lease",
        "release_printer_lease",
        "get_printer_lease",
        "break_printer_lease",
    }
)

#: Print-job tools whose two-step token is a confirmation, not a privilege: the
#: role table judges them as ``Risk.PRINT`` (see ``Runtime._check_role``).
PRINT_JOB_TOOLS = frozenset({"start_print", "cancel_print"})


def lease_exempt(tool: str, args: Mapping[str, Any]) -> bool:
    """Whether the lease should stay out of the way of this particular call.

    Mostly a set membership test, with one argument-dependent case:
    ``control_job_queue`` carries both directions in one tool, and they are not
    symmetric. ``pause`` only ever prevents a *future* automatic start, so anyone
    may do it; ``start`` releases the brake and is the call that turns a list of
    files into hours of machine time.
    """
    if tool in LEASE_EXEMPT_TOOLS:
        return True
    return tool == "control_job_queue" and str(args.get("action", "")).lower() == "pause"


def lease_required(tool: str, args: Mapping[str, Any]) -> bool:
    """Whether this call needs an *explicit* claim even on a free printer.

    Committing the machine for hours without saying "this is mine" is how two
    agents discover each other the expensive way. Three ways to do it:

    * ``start_print`` -- the obvious one.
    * ``control_job_queue`` with ``action='start'`` -- releases the queue.
    * ``queue_jobs`` **when the queue is already live**. This is the case that is
      easy to miss: with ``queue_state == 'ready'``, Moonraker starts the next job
      the moment the printer goes idle, so "just adding a file to the list" *is*
      starting a print. The tool reads the queue state and puts it in its audit
      args, which is what makes the distinction visible here.
    """
    if tool == "start_print":
        return True
    action = str(args.get("action", "")).lower()
    if tool == "control_job_queue" and action == "start":
        return True
    return tool == "queue_jobs" and str(args.get("queue_state", "")).lower() == "ready"

#: Objects needed to evaluate interlocks — kept small; this is queried often.
_STATE_OBJECTS: dict[str, list[str] | None] = {
    "webhooks": None,
    "print_stats": ["state", "filename", "message"],
    "toolhead": ["homed_axes", "position"],
    "extruder": ["temperature", "target"],
    "gcode_move": ["gcode_position"],
}


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = MoonrakerClient(settings)
        self.policy = SafetyPolicy(settings)
        self.caps = CapabilityCache(self.client, settings)
        self.lease = LeaseManager(self.client, settings)
        self._state: PrinterState | None = None
        self._state_at = 0.0
        self._state_ttl = 0.75

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _degraded_state(self, exc: KlippyNotReady) -> PrinterState:
        """Best-effort state when Klipper cannot be queried.

        A 503 from Moonraker is ambiguous, and the ambiguity is dangerous:

        * Klipper really is in shutdown — nothing is moving or heating, and the
          recovery tools (restart_firmware, restart_service, reboot) are exactly
          what the operator needs. Blocking them here would strand the printer.
        * Moonraker was restarted, or a proxy in front of it hiccuped, while
          Klipper carried on **printing**. Same 503, completely different
          situation — rebooting the host now ruins the job and leaves a hot
          nozzle parked in the part.

        Guessing "idle" was the old behaviour and it is unsafe. So ask Moonraker
        itself: ``/server/info`` is served by Moonraker, not Klipper, and reports
        ``klippy_state`` even when the Klipper connection is down. If it confirms
        shutdown/error, Klipper is genuinely halted. If that call also fails,
        Moonraker is unreachable and we honestly do not know — so the print state
        stays unknown, which ``job_active`` treats as "possibly printing".
        """
        klippy_state = "shutdown"
        print_state = UNKNOWN_PRINT_STATE
        try:
            info = await self.client.server_info()
            reported = str(info.get("klippy_state", "") or "").lower()
            if reported:
                klippy_state = reported
            if reported in {"shutdown", "error"}:
                # Confirmed by Moonraker: Klipper halted, so no job is running.
                # Motion and heating are stopped; recovery is safe to authorise.
                print_state = "standby"
        except QidiError:
            # Moonraker itself is unreachable: keep print_state unknown.
            pass
        return PrinterState(
            klippy_state=klippy_state,
            klippy_message=str(exc.message),
            print_state=print_state,
            homed_axes="",
        )

    # -- live state ---------------------------------------------------------

    async def state(self, *, fresh: bool = False) -> PrinterState:
        """Live printer state, cached for a fraction of a second.

        The cache exists so one tool call does not query twice while building its
        response. It is **never** used for a safety decision: ``authorize()``
        always passes ``fresh=True``, because a 750 ms-old snapshot is long
        enough for someone to press Print on the touchscreen between a refusal
        and the agent's retry.
        """
        now = time.monotonic()
        if not fresh and self._state is not None and (now - self._state_at) < self._state_ttl:
            return self._state

        status = await self.client.query(_STATE_OBJECTS)
        webhooks = status.get("webhooks", {}) or {}
        stats = status.get("print_stats", {}) or {}
        toolhead = status.get("toolhead", {}) or {}
        extruder = status.get("extruder", {}) or {}

        caps = self.caps.cached()
        min_extrude = 135.0
        if caps is not None:
            info = caps.heaters.get("extruder") or {}
            with contextlib.suppress(TypeError, ValueError):
                min_extrude = float(info.get("min_extrude_temp", min_extrude))

        position = toolhead.get("position") or [0.0, 0.0, 0.0, 0.0]
        while len(position) < 4:
            position.append(0.0)

        state = PrinterState(
            klippy_state=str(webhooks.get("state", "unknown")),
            klippy_message=str(webhooks.get("state_message", "") or ""),
            print_state=str(stats.get("state", "standby")),
            filename=str(stats.get("filename", "") or ""),
            homed_axes=str(toolhead.get("homed_axes", "") or ""),
            extruder_temp=float(extruder.get("temperature", 0.0) or 0.0),
            extruder_target=float(extruder.get("target", 0.0) or 0.0),
            min_extrude_temp=min_extrude,
            position=tuple(float(p or 0.0) for p in position[:4]),  # type: ignore[arg-type]
        )
        self._state, self._state_at = state, now
        return state

    def invalidate_state(self) -> None:
        self._state = None

    async def capabilities(self, *, refresh: bool = False) -> Capabilities:
        return await self.caps.get(refresh=refresh)

    # -- who is calling ------------------------------------------------------

    def _check_role(self, tool: str, risk: Risk, args: Mapping[str, Any]) -> None:
        """Refuse work the authenticated caller is not permitted to do.

        Only active in a shared deployment (``QIDI_IDENTITY_ENFORCE=1``). A local
        stdio checkout has no edge to authenticate against and one obvious user,
        so this stays out of the way there entirely.

        Ordering is deliberate and matches ``_check_lease``:

        * ``EMERGENCY`` returns first. Deliberate: the
          person who can see the machine on fire is whoever is looking at it, not
          whoever is on a list.
        * Global read-only short-circuits, so a blocked caller hears the most
          specific reason (``read_only_mode``) rather than a role complaint about
          a server that is refusing everyone anyway.
        * The role check precedes the lease check, because "you may not move this
          printer at all" is more useful than "someone else is driving".
        """
        if risk is Risk.EMERGENCY or risk not in MUTATING:
            return
        if not self.settings.identity_enforce:
            return
        if self.settings.read_only:
            return  # policy.authorize refuses this next, with a better message

        caller = current_caller()
        if caller is None:
            # Fail closed. Reaching here means the edge did not stamp a verified
            # identity on a mutating call -- which should be impossible through
            # the platform, and is exactly the case not to guess about.
            self.policy.audit.record(tool, risk, args, "refused", "identity_required")
            raise IdentityRequired(
                f"{tool} changes the printer, and this request carried no verified "
                "identity.",
                hint=(
                    "Connect through the authenticating proxy in front of this server so your sign-in is "
                    "forwarded. Reads work without this; writes do not."
                ),
            )

        # Starting and cancelling a print take the two-step token, so they reach
        # this gate as DESTRUCTIVE -- but the token is about *confirming* the job,
        # not about who may run it. Printing is the operator's job by design (see
        # identity.py), and judging these by their confirmation tier quietly made
        # every print maintainer-only.
        role_risk = Risk.PRINT if tool in PRINT_JOB_TOOLS and risk is Risk.DESTRUCTIVE else risk
        if caller.role.allows(role_risk):
            return

        needed = "maintainer" if role_risk is Risk.DESTRUCTIVE else "operator"
        self.policy.audit.record(
            tool, risk, args, "refused", f"role_required:{needed}:{caller.email}"
        )
        raise RoleRequired(
            f"Cannot run {tool}: it needs the '{needed}' role and "
            f"{caller.email} is '{caller.role.value}'.",
            hint=(
                "Everyone signed in can read the printer; moving, heating and "
                "printing are granted per person. Ask in #software-interns to be "
                "added, and say what you want to print."
            ),
            caller=caller.as_dict(),
        )

    # -- cross-agent coordination -------------------------------------------

    async def _check_lease(self, tool: str, risk: Risk, args: Mapping[str, Any]) -> None:
        """Refuse mutating work that belongs to another agent's session.

        Runs before every other gate, and asks the printer rather than trusting
        local memory: the lease lives in Moonraker's database precisely because the
        other agent is in a different process on a different laptop.

        Three deliberate choices:

        * **Read-only mode short-circuits first.** Otherwise the dashboard -- which
          polls constantly with read-only forced on -- would spend a round trip per
          poll asking who holds a lease it can never use, and a blocked caller would
          be told ``lease_held`` when the real reason is ``read_only_mode``. The most
          specific refusal is the useful one.
        * **An unreachable or absent database fails open.** Coordination is advisory;
          refusing all work because the coordination channel is down would convert a
          missing convenience into an outage, and every other gate still applies.
        * **No caching.** ``state()`` is cached for a fraction of a second but is
          never used for a safety decision; the same rule applies here. Someone can
          acquire the lease between a refusal and a retry.
        """
        if risk is Risk.EMERGENCY or risk not in MUTATING:
            return
        if self.settings.read_only:
            return  # policy.authorize refuses this next, with a better message
        if lease_exempt(tool, args):
            return

        needs_own = self.settings.lease_required_for_print and lease_required(tool, args)
        if not self.settings.lease_enforce and not needs_own:
            return

        try:
            lease = await self.lease.current()
        except QidiError:
            return  # fail open, deliberately -- see the docstring

        if lease is not None and not self.lease.holds(lease):
            if self.settings.lease_enforce:
                self.policy.audit.record(
                    tool, risk, args, "refused", f"lease_held_by:{lease.owner}"
                )
                raise _held_error(lease, action=f"run {tool}")
            return

        if needs_own and not self.lease.holds(lease):
            self.policy.audit.record(tool, risk, args, "refused", "lease_required")
            raise LeaseRequired(
                f"{tool} needs the printer lease first, so other agents can see the "
                "machine is in use.",
                hint=(
                    "Call acquire_printer_lease with a short reason (e.g. 'bracket v3, "
                    "~40 min'), then retry. Release it when you are done."
                ),
            )

    # -- the gate -----------------------------------------------------------

    async def authorize(
        self,
        tool: str,
        risk: Risk,
        args: Mapping[str, Any],
        *,
        confirm: bool = False,
        confirm_token: str | None = None,
        interlocks: Interlocks | None = None,
    ) -> dict[str, Any] | None:
        """Safety gate. Returns a ticket payload to return verbatim, or None."""
        self._check_role(tool, risk, args)
        await self._check_lease(tool, risk, args)
        state: PrinterState | None = None
        if interlocks is not None:
            try:
                # fresh=True is mandatory here. Interlocks are the one place a
                # cached snapshot is unacceptable: a refused call primes the
                # cache, and the agent's retry would then be judged against
                # state from before the user pressed Print.
                state = await self.state(fresh=True)
            except KlippyNotReady as exc:
                # Recovery paths (restart_firmware, restart_service, reboot) set
                # require_klippy_ready=False precisely because Klippy is down,
                # so fetching state must not be what blocks them — that would
                # make a shutdown unrecoverable through this server.
                #
                # But we must NOT claim to know that nothing is printing. A 503
                # only means Moonraker could not reach Klipper; Moonraker
                # restarting while a print continues produces exactly the same
                # 503. Asserting print_state="standby" here would let
                # forbid_during_print pass mid-print and authorise a reboot or a
                # firmware restart on a live job. print_state is left unknown and
                # PrinterState.job_active treats unknown as "possibly printing".
                if interlocks.require_klippy_ready:
                    raise
                state = await self._degraded_state(exc)
        return self.policy.authorize(
            tool,
            risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            state=state,
            interlocks=interlocks,
        )

    def ok(
        self,
        tool: str,
        risk: Risk,
        args: Mapping[str, Any],
        payload: Mapping[str, Any] | None = None,
        *,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Record success in the audit log and build the response envelope."""
        if risk is not Risk.READ:
            self.policy.audit.record(tool, risk, args, "ok", detail)
            self.invalidate_state()
        body: dict[str, Any] = {"ok": True}
        if payload:
            body.update(payload)
        return body

    # -- effective limits ---------------------------------------------------

    async def sync_limits(self) -> None:
        """Tighten the policy envelope using limits the printer itself reports."""
        caps = await self.capabilities()
        effective = self.caps.effective_envelope(caps)
        from dataclasses import replace

        self.policy.settings = replace(self.policy.settings, safety=effective)
        self.settings = self.policy.settings


def _runtime_of(fn: Callable[..., Any]) -> Runtime | None:
    """Recover the Runtime a tool closed over, for audit logging.

    Every tool is defined inside ``register(server, rt)`` and closes over ``rt``,
    so it can be read back from the closure. Passing the runtime through the
    decorator explicitly would be cleaner, but would mean touching all 89 tool
    definitions to fix a logging gap -- a large diff whose size would obscure the
    one-line behavioural change. Returns None rather than raising if the shape is
    ever different, because failing to log must never break a printer operation.
    """
    for cell in getattr(fn, "__closure__", None) or ():
        with contextlib.suppress(ValueError):
            if isinstance(cell.cell_contents, Runtime):
                return cell.cell_contents
    return None


def guarded(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Convert QidiError into a structured payload instead of a traceback.

    Unexpected exceptions are also caught: an MCP tool that raises leaves the
    agent with no idea whether the printer moved, which is worse than a clear
    error object.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except QidiError as exc:
            # A refusal is evidence, not a non-event.
            #
            # Gate decisions inside authorize() are logged, but a refusal raised
            # BEFORE the gate -- a temperature outside the envelope, a NaN, a
            # deny-listed command on the raw path -- previously left no trace,
            # while the harmless calls that reached the gate were all recorded.
            # That is exactly backwards for anyone reading the log to find out
            # what an agent tried to do.
            rt = _runtime_of(fn)
            if rt is not None and exc.code != "read_only_mode":
                # read_only_mode is already recorded by the registration shield.
                with contextlib.suppress(Exception):
                    rt.policy.audit.record(
                        getattr(fn, "__name__", "unknown"),
                        Risk.LOW,
                        {k: v for k, v in kwargs.items() if k != "ctx"},
                        "refused",
                        exc.code,
                    )
            return exc.to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "ok": False,
                "error": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "This is a bug in qidi-mcp, not a printer refusal.",
            }

    # Preserve the signature so the MCP SDK builds the right input schema.
    #
    # These modules use `from __future__ import annotations`, so annotations are
    # strings. functools.wraps copies them verbatim, but the wrapper's
    # __globals__ belong to *this* module — where names like `Image` are not
    # imported — so the SDK would resolve them in the wrong namespace. Resolve
    # eagerly against the original function's module instead.
    try:
        wrapper.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
        wrapper.__annotations__ = dict(get_type_hints(fn))
    except Exception:  # pragma: no cover - fall back to unresolved strings
        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper
