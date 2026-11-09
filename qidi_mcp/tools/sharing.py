"""The printer lease: a cooperative claim so two agents cannot drive at once.

See ``qidi_mcp.lease`` for why the lease lives in Moonraker's database (every agent
runs its own server process, so the printer is the only shared state) and why it is
cooperative rather than a lock.

The *queue* half of sharing this machine already exists, in ``jobs.py``:
``get_job_queue``, ``queue_jobs``, ``remove_queued_jobs`` and ``control_job_queue``.
Those are the polite path when somebody else is printing -- your file runs after
theirs instead of fighting for the machine -- and they are lease-aware through the
gate in ``Runtime._check_lease`` rather than through a second, competing set of
tools.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import InvalidRequest
from ..policy import Risk
from ..runtime import Runtime, guarded

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def register(server: MCPServer, rt: Runtime) -> None:
    # -- the lease ----------------------------------------------------------

    @server.tool(
        name="get_printer_lease",
        description=(
            "Who is currently driving this printer, if anyone. Returns the holder's name, "
            "their stated reason, and how many minutes are left, plus whether it is you. "
            "A print started at the touchscreen or from Fluidd shows as a manual-print claim "
            "(manual=true) held by nobody. "
            "Call this first when a write is refused with 'lease_held', and before starting "
            "any long job. Reports coordination as unavailable on a printer whose Moonraker "
            "has no database component."
        ),
        annotations=READ,
    )
    @guarded
    async def get_printer_lease() -> dict[str, Any]:
        return rt.ok("get_printer_lease", Risk.READ, {}, await rt.lease.status())

    @server.tool(
        name="acquire_printer_lease",
        description=(
            "Claim this printer for yourself, so other agents are refused mutating actions "
            "while you work. Give a short human reason ('bracket v3, ~40 min') — the point is "
            "that whoever gets blocked can see who to talk to. Refused if somebody else holds "
            "it. Expires on its own so a forgotten lease cannot strand the machine; renew it "
            "with renew_printer_lease for long jobs. This is cooperative: it does not stop the "
            "printer's own touchscreen or Fluidd."
        ),
        annotations=ACT,
    )
    @guarded
    async def acquire_printer_lease(
        reason: str = "", minutes: float | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        wanted = float(minutes) if minutes is not None else rt.settings.lease_minutes
        if wanted <= 0:
            raise InvalidRequest("minutes must be positive.")
        args = {"reason": reason, "minutes": wanted}
        ticket = await rt.authorize("acquire_printer_lease", Risk.LOW, args, confirm=confirm)
        if ticket is not None:
            return ticket
        lease = await rt.lease.acquire(reason, wanted)
        return rt.ok(
            "acquire_printer_lease",
            Risk.LOW,
            args,
            {
                "acquired": True,
                "lease": lease.to_payload(),
                "note": (
                    "Other agents running this server will now be refused mutating "
                    "actions. Release it when you are done — and remember the "
                    "touchscreen and Fluidd do not consult it."
                ),
            },
        )

    @server.tool(
        name="renew_printer_lease",
        description=(
            "Extend your own lease, for a job that is taking longer than planned. Refused if "
            "you do not hold it, or if it already lapsed and somebody else took it."
        ),
        annotations=ACT,
    )
    @guarded
    async def renew_printer_lease(minutes: float | None = None) -> dict[str, Any]:
        wanted = float(minutes) if minutes is not None else rt.settings.lease_minutes
        args = {"minutes": wanted}
        await rt.authorize("renew_printer_lease", Risk.LOW, args, confirm=True)
        lease = await rt.lease.renew(wanted)
        return rt.ok("renew_printer_lease", Risk.LOW, args, {"lease": lease.to_payload()})

    @server.tool(
        name="release_printer_lease",
        description=(
            "Give the printer back when you are finished. Do this rather than letting the "
            "lease expire — the next person should not have to wait out your timeout."
        ),
        annotations=ACT,
    )
    @guarded
    async def release_printer_lease() -> dict[str, Any]:
        await rt.authorize("release_printer_lease", Risk.LOW, {}, confirm=True)
        return rt.ok("release_printer_lease", Risk.LOW, {}, await rt.lease.release())

    @server.tool(
        name="break_printer_lease",
        description=(
            "Force-release SOMEBODY ELSE's lease, for when its holder has clearly gone away "
            "and the machine is stranded. Two-step confirmation, and the break is recorded in "
            "the audit log with your name. Check get_printer_lease and look at the machine "
            "first: if a print is actually running, breaking the lease does not stop it, and "
            "cancel_print or emergency_stop is what you want. On a manual-print claim (a print "
            "started at the touchscreen, shown with manual=true) this takes the print over: the "
            "claim becomes yours so you can then pause or cancel it."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def break_printer_lease(
        why: str = "", confirm_token: str | None = None
    ) -> dict[str, Any]:
        args = {"why": why.strip()[:200]}
        ticket = await rt.authorize(
            "break_printer_lease", Risk.DESTRUCTIVE, args, confirm_token=confirm_token
        )
        if ticket is not None:
            current = await rt.lease.status()
            ticket["currently_held_by"] = current.get("lease")
            ticket["why_this_is_gated"] = (
                "Overriding someone else's claim on a shared machine should be a decision, "
                "not a reflex. If they are mid-print, this does not stop the print."
            )
            return ticket
        return rt.ok("break_printer_lease", Risk.DESTRUCTIVE, args, await rt.lease.break_lease())
