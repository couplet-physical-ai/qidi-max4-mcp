"""A cooperative lease so two agents cannot drive the printer at once.

The problem this solves is specific to how this server is deployed. Every agent
runs its **own** ``qidi-mcp`` process, on its own laptop, all pointing at the same
Moonraker. So there is no shared server process, and therefore nowhere in this
codebase that a lock could live: an in-process mutex protects one agent from
itself, and a file lock protects one *machine*. Neither notices the intern across
the room.

The one piece of state every agent can already see is **the printer**. Moonraker
ships a namespaced key-value database (``/server/database/item``) intended for
exactly this kind of third-party state, and QIDI's fork keeps it -- verified
against the machine, including writing and deleting a scratch namespace. So the
lease lives there, and is visible to every agent, the dashboard, and anyone with
``curl``.

What this is and is not
-----------------------

It is **cooperative**, and the docs say so in those words. It stops a well-behaved
agent from starting a print on a machine somebody else is using. It cannot stop:

* the printer's own touchscreen, or Fluidd in a browser -- neither consults it;
* an older build of this server, or one run with ``QIDI_LEASE_ENFORCE=0``;
* someone who calls ``break_printer_lease``, which is deliberately available.

Calling it a lock would therefore be a lie, and a dangerous one -- it would invite
an agent to skip looking at the machine. It is a coordination signal that makes a
collision *loud and attributable* instead of silent.

Two design decisions worth stating, because both could reasonably have gone the
other way:

**Acquisition writes then verifies, rather than pretending to be atomic.**
Moonraker's database has no compare-and-set. So acquire reads, writes, waits a
moment, and reads back: if the holder id that comes back is not ours, somebody
else won and we refuse. That closes the window in the case that actually happens
-- two agents told to print at the same time -- without claiming a guarantee the
transport cannot provide.

**A missing database component fails OPEN, not closed.** If a printer lacks the
component, refusing every write would brick this server on that machine for a
feature that is advisory in the first place. Instead coordination reports itself
as unavailable, loudly, in ``get_capabilities`` and in the lease status.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import secrets
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__
from .client import MoonrakerClient
from .config import Settings
from .errors import MoonrakerHTTPError, NotSupported, QidiError, SafetyViolation

#: Statuses that mean "there is no lease stored", as opposed to a real failure.
#:
#: Moonraker answers **404** for a missing key -- measured on the machine, for both a
#: missing key in an existing namespace and a namespace that does not exist yet. The
#: client turns 404 into ``NotSupported`` because for every other endpoint a 404 does
#: mean "this fork dropped that route". Here it does not, and conflating the two would
#: be the ``soft_abort`` bug over again: an absent *key* read as an absent *feature*,
#: making the printer look permanently unleased or permanently unsupported.
#:
#: The disambiguation is that ``supported()`` asks a different endpoint
#: (``/server/database/list``) whether the component exists at all. Once that says
#: yes, a 404 from the item route can only mean the key is not there.
_ABSENT_STATUSES = (400, 404)

#: Third-party namespace. Moonraker creates it on first write and drops it when
#: the last key is deleted, so an unused install leaves nothing behind.
NAMESPACE = "qidi_mcp"
KEY = "lease"

#: Holder id prefix of a claim written for a print nobody claimed: started on the
#: touchscreen, from Fluidd, or by a slicer upload. No caller can hold it, so it
#: refuses everyone, which is the point -- the machine is busy, whoever started it.
JOB_HOLDER_PREFIX = "printer-job:"
#: How the printer appears as the holder of such a claim.
MANUAL_OWNER = "a manual print at the machine"
#: Slack beyond the file's own estimate, so a slow print is not left unclaimed.
_JOB_MARGIN_S = 30 * 60.0
#: A covering claim is rewritten only when it is this close to lapsing, so the
#: watcher's 15-second poll does not become a database write every 15 seconds.
_RENEW_BELOW_S = 20 * 60.0

#: How long to wait before reading back our own write to detect a lost race.
#: Long enough to catch a genuinely concurrent acquire, short enough that nobody
#: notices it in an interactive session.
_VERIFY_DELAY_S = 0.35


class LeaseHeld(SafetyViolation):
    """Somebody else is driving the printer right now."""

    code = "lease_held"


class LeaseRequired(SafetyViolation):
    """This action needs an explicit lease, and the caller does not hold one."""

    code = "lease_required"


@dataclass
class Lease:
    """One agent's claim on the machine."""

    holder_id: str
    owner: str
    reason: str
    acquired_at: float
    expires_at: float
    renewed_at: float
    agent: str = ""
    # The build that claimed the machine. An older server does not enforce the
    # lease at all, so "who is driving" is only actionable next to "on what".
    server_version: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    @property
    def expired(self) -> bool:
        return self.seconds_remaining <= 0.0

    @property
    def manual(self) -> bool:
        """A claim the server wrote for a print started outside it."""
        return self.holder_id.startswith(JOB_HOLDER_PREFIX)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("extra", None)
        payload["manual"] = self.manual
        payload["seconds_remaining"] = round(self.seconds_remaining, 1)
        payload["minutes_remaining"] = round(self.seconds_remaining / 60, 1)
        payload["expired"] = self.expired
        return payload

    @classmethod
    def from_value(cls, value: Any) -> Lease | None:
        """Parse a stored lease, tolerating anything that is not one.

        A malformed value must not be able to wedge the printer: if somebody
        writes junk into the namespace by hand, we treat the machine as free
        rather than refusing every action until a human cleans up.
        """
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                holder_id=str(value["holder_id"]),
                owner=str(value.get("owner", "unknown")),
                reason=str(value.get("reason", "")),
                acquired_at=float(value.get("acquired_at", 0.0)),
                expires_at=float(value["expires_at"]),
                renewed_at=float(value.get("renewed_at", value.get("acquired_at", 0.0))),
                agent=str(value.get("agent", "")),
                server_version=str(value.get("server_version", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def default_owner(configured: str = "") -> str:
    """A human-recognisable identity: ``user@host``.

    The point of the owner field is that whoever gets blocked by a lease can walk
    over and talk to the person holding it, so this favours recognisability over
    uniqueness. ``QIDI_AGENT_NAME`` overrides it.
    """
    if configured.strip():
        return configured.strip()[:120]
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - depends on a broken passwd db
        user = "unknown"
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:  # pragma: no cover
        host = "unknown"
    return f"{user}@{host}"[:120]


def holder_id_for(key: str = "") -> str:
    """The lease holder id for a server process.

    Without a key, a fresh random id: every process is its own agent.

    With ``QIDI_LEASE_HOLDER_KEY``, a stable id derived from that secret, so a
    *sequence* of short-lived stdio processes -- a CLI that runs one command per
    process, or a long-running client session that restarts -- is one holder. Without it, a lease
    taken by one process is, to the next, somebody else's: the same person is
    refused by their own claim, cannot ``release_printer_lease`` it, and must
    ``break_printer_lease`` themselves or wait out the timeout.

    The id is a hash of the key rather than the key itself because the id is
    public: ``get_printer_lease`` shows it to everyone. Knowing it does not let
    another agent present itself as the holder; only knowing the key does.
    Two processes given the same key are deliberately one agent -- that is the
    point -- so give each concurrently-running agent its own key.
    """
    if not key:
        return secrets.token_hex(8)
    return hashlib.sha256(b"qidi-lease-holder:" + key.encode("utf-8")).hexdigest()[:16]


def _caller_holder_id(email: str) -> str:
    """A shared deployment's holder id for a verified caller.

    A separate hash domain from ``holder_id_for``: if it were
    ``holder_id_for("caller:" + email)``, a laptop setting that string as its
    QIDI_LEASE_HOLDER_KEY would become that person's holder without breaking
    anything, and nothing would be recorded as a break.
    """
    key = email.strip().lower().encode("utf-8")
    return hashlib.sha256(b"qidi-lease-caller:" + key).hexdigest()[:16]


class LeaseManager:
    """Reads and writes the shared lease in Moonraker's database."""

    def __init__(self, client: MoonrakerClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        #: Identifies *this server process*. Deliberately not derived from the
        #: user: two shells belonging to the same person are still two agents,
        #: and letting them share a lease would defeat the purpose -- unless the
        #: operator opts in with QIDI_LEASE_HOLDER_KEY (see ``holder_id_for``).
        self._process_holder_id = holder_id_for(settings.lease_holder_key)
        self._configured_owner = default_owner(settings.agent_name)
        self._supported: bool | None = None

    @property
    def holder_id(self) -> str:
        """Who holds a lease, resolved per call like ``owner``.

        On a laptop, the process. In a shared deployment, the verified caller: one
        process serves everybody there, so a per-process id made every signed-in
        person the holder of whatever lease anyone took -- the lease refused
        nobody inside the app it was meant to coordinate. Keyed by email, a
        person's agent and their dashboard session are one holder, which is the
        useful reading of "who is driving".
        """
        from .identity import current_caller

        caller = current_caller()
        if caller is not None:
            return _caller_holder_id(caller.email)
        return self._process_holder_id

    @property
    def owner(self) -> str:
        """Who to name in the lease, resolved per call rather than at startup.

        On a laptop this is ``user@host`` (or ``QIDI_AGENT_NAME``) -- self-declared,
        because there is nothing better available and the person is sitting there.

        In a shared deployment, the authenticating proxy has already authenticated the caller,
        so the verified address from your auth proxy wins. That matters because the
        entire purpose of the owner field is that whoever gets blocked can go and
        talk to whoever is holding it, and one shared process serving everyone
        would otherwise stamp every lease with the *server's* hostname -- turning a
        useful name into noise.

        Resolved per access, not cached in ``__init__``: one process handles many
        callers, so there is no single owner for the lifetime of the object.
        """
        from .identity import current_caller

        caller = current_caller()
        if caller is not None:
            return caller.email[:120]
        return self._configured_owner

    # -- transport ----------------------------------------------------------

    async def supported(self) -> bool:
        """Whether this printer has the database component, cached once it is known.

        Only a definite answer is cached: the route answering, or the route being
        absent (404). A printer that cannot be reached says nothing about its
        database, so that is re-asked next time. Caching it as "no database" is how
        Printers 1 and 3 lost their lease on 2026-11-04: the app first looked while
        the firewall still blocked them, decided "unsupported" for the life of the
        process, and from then on never took a lease there -- so the dashboard's
        start was refused with ``lease_required`` once the printers were reachable.
        """
        if self._supported is None:
            try:
                await self.client.get("/server/database/list")
                self._supported = True
            except NotSupported:
                self._supported = False
            except MoonrakerHTTPError as exc:
                if exc.status != 404:
                    return False  # not a definite answer; ask again next time
                self._supported = False
            except QidiError:
                return False  # unreachable or timed out; ask again next time
        return self._supported

    async def _read_raw(self) -> Any:
        try:
            result = await self.client.get(
                "/server/database/item", params={"namespace": NAMESPACE, "key": KEY}
            )
        except NotSupported:
            # 404: no lease stored. See _ABSENT_STATUSES for why this is not the
            # same as "this printer has no database component".
            return None
        except MoonrakerHTTPError as exc:
            if exc.status in _ABSENT_STATUSES:
                return None
            raise
        if isinstance(result, dict):
            return result.get("value", result)
        return result

    async def _write(self, lease: Lease) -> None:
        payload = asdict(lease)
        payload.pop("extra", None)
        await self.client.post(
            "/server/database/item",
            json_body={"namespace": NAMESPACE, "key": KEY, "value": payload},
        )

    async def _delete(self) -> None:
        try:
            await self.client.delete(
                "/server/database/item", params={"namespace": NAMESPACE, "key": KEY}
            )
        except NotSupported:
            pass  # already gone, which is the state we wanted
        except MoonrakerHTTPError as exc:
            if exc.status not in _ABSENT_STATUSES:
                raise

    # -- reads --------------------------------------------------------------

    async def current(self) -> Lease | None:
        """The live lease, or None if nobody holds one (or it lapsed)."""
        if not await self.supported():
            return None
        lease = Lease.from_value(await self._read_raw())
        if lease is None or lease.expired:
            return None
        return lease

    def holds(self, lease: Lease | None) -> bool:
        return lease is not None and lease.holder_id == self.holder_id

    async def status(self) -> dict[str, Any]:
        supported = await self.supported()
        if not supported:
            return {
                "coordination": "unavailable",
                "held": False,
                "you_hold_it": False,
                "your_holder_id": self.holder_id,
                "your_owner": self.owner,
                "enforced": False,
                "note": (
                    "This printer's Moonraker has no 'database' component, so there is "
                    "nowhere shared to store a lease. Coordinate with your team by hand; "
                    "this server cannot detect a second agent."
                ),
            }
        lease = await self.current()
        return {
            "coordination": "available",
            "held": lease is not None,
            "you_hold_it": self.holds(lease),
            "your_holder_id": self.holder_id,
            "your_owner": self.owner,
            "enforced": self.settings.lease_enforce,
            "required_for_print": self.settings.lease_required_for_print,
            "lease": lease.to_payload() if lease else None,
        }

    # -- writes -------------------------------------------------------------

    async def acquire(self, reason: str, minutes: float) -> Lease:
        if not await self.supported():
            raise NotSupported(
                "This printer's Moonraker has no 'database' component, so a shared "
                "lease cannot be stored.",
                hint=(
                    "Coordinate by hand. Every other safety gate still applies; only "
                    "cross-agent coordination is unavailable."
                ),
            )
        existing = await self.current()
        if existing is not None and not self.holds(existing):
            raise _held_error(existing, action="acquire a lease")

        now = time.time()
        lease = Lease(
            holder_id=self.holder_id,
            owner=self.owner,
            reason=reason.strip()[:200] or "unspecified",
            acquired_at=existing.acquired_at if self.holds(existing) else now,
            expires_at=now + max(60.0, minutes * 60.0),
            renewed_at=now,
            agent=self.owner,
            server_version=__version__,
        )
        await self._write(lease)

        # Write-then-verify: see the module docstring. Without this, two agents
        # that read "free" at the same moment would both believe they hold it,
        # and the loser would find out by colliding with a real print.
        await asyncio.sleep(_VERIFY_DELAY_S)
        confirmed = await self.current()
        if not self.holds(confirmed):
            raise _held_error(
                confirmed,
                action="acquire a lease",
                lost_race=True,
            )
        return confirmed  # type: ignore[return-value]

    async def renew(self, minutes: float) -> Lease:
        lease = await self.current()
        if lease is None:
            raise LeaseRequired(
                "There is no lease to renew — it has already lapsed or been released.",
                hint="Call acquire_printer_lease to take it again.",
            )
        if not self.holds(lease):
            raise _held_error(lease, action="renew the lease")
        now = time.time()
        lease.expires_at = now + max(60.0, minutes * 60.0)
        lease.renewed_at = now
        await self._write(lease)
        return lease

    async def release(self) -> dict[str, Any]:
        lease = await self.current()
        if lease is None:
            return {"released": False, "note": "No lease was held."}
        if not self.holds(lease):
            raise _held_error(lease, action="release the lease")
        await self._delete()
        return {"released": True, "was": lease.to_payload()}

    async def break_lease(self) -> dict[str, Any]:
        """Force-release somebody else's lease.

        Deliberately available. A lease whose holder went home would otherwise
        strand the machine, and an agent that cannot break a stale claim will
        reach for something worse. It is gated behind a two-step token and every
        break is recorded in the audit log with who broke what.

        Breaking a manual-print claim **takes it over** instead of freeing it: the
        print is still running, so a free machine would be re-claimed for it on the
        next watcher poll and the breaker could still do nothing. Handing the claim
        to whoever broke it is what lets a maintainer pause or cancel a print that
        was started at the touchscreen.
        """
        lease = await self.current()
        if lease is None:
            return {"broken": False, "note": "No live lease to break."}
        if lease.manual:
            now = time.time()
            mine = Lease(
                holder_id=self.holder_id,
                owner=self.owner,
                reason=f"took over {_job_file(lease)}",
                acquired_at=now,
                expires_at=max(lease.expires_at, now + 60.0),
                renewed_at=now,
                agent=self.owner,
                server_version=__version__,
            )
            await self._write(mine)
            return {"broken": True, "taken_over": True, "was": lease.to_payload(),
                    "now": mine.to_payload(), "broken_by": self.owner}
        await self._delete()
        return {"broken": True, "was": lease.to_payload(), "broken_by": self.owner}

    # -- prints nobody claimed ---------------------------------------------

    async def cover_job(
        self, job: dict[str, Any], *, estimated_s: float | None = None
    ) -> dict[str, Any] | None:
        """Make sure a running print is covered by a live lease.

        Called by a deployment's watcher for the job Moonraker reports in
        progress. The lease is otherwise only written by callers of this server,
        so a print started on the touchscreen, from Fluidd or by a slicer upload
        left the machine looking free while it ran. Three cases:

        * **Someone holds a live lease**: theirs. Nothing to do, unless it is an
          older manual claim for a different job, which is replaced.
        * **A person's lease lapsed during their own print** (it was held when the
          job started): extended to the file's estimate, so the person who started
          it keeps it rather than it being relabelled as a manual print.
        * **Nothing covers it**: a manual-print claim, held by no caller.

        Returns what was done, or None when nothing changed.
        """
        if not await self.supported():
            return None
        now = time.time()
        start = float(job.get("start_time") or now)
        until = (start + estimated_s if estimated_s else now) + _JOB_MARGIN_S
        until = min(max(until, now + _JOB_MARGIN_S), now + 86400.0)
        job_holder = f"{JOB_HOLDER_PREFIX}{job.get('job_id')}"
        stored = Lease.from_value(await self._read_raw())

        if stored is not None and not stored.expired:
            if not stored.manual:
                return None
            if stored.holder_id == job_holder:
                if stored.seconds_remaining >= _RENEW_BELOW_S:
                    return None
                stored.expires_at, stored.renewed_at = until, now
                await self._write(stored)
                return {"action": "renewed_manual", "lease": stored.to_payload()}
            # A manual claim for a job that has ended: fall through and replace it.

        if (
            stored is not None
            and not stored.manual
            and stored.acquired_at <= start + 600
            and stored.expires_at >= start
        ):
            stored.expires_at, stored.renewed_at = until, now
            await self._write(stored)
            return {"action": "extended_for_their_print", "lease": stored.to_payload()}

        filename = str(job.get("filename") or "a file")
        lease = Lease(
            holder_id=job_holder,
            owner=MANUAL_OWNER,
            reason=f"{filename}{_MANUAL_REASON}",
            acquired_at=start,
            expires_at=until,
            renewed_at=now,
            agent="printer",
            server_version=__version__,
        )
        await self._write(lease)
        return {"action": "claimed_manual", "lease": lease.to_payload()}

    async def release_job_claims(self) -> dict[str, Any] | None:
        """Drop a manual-print claim once nothing is printing. People's leases stay."""
        if not await self.supported():
            return None
        stored = Lease.from_value(await self._read_raw())
        if stored is None or not stored.manual:
            return None
        await self._delete()
        return {"action": "released_manual", "was": stored.to_payload()}


_MANUAL_REASON = " started on the printer (touchscreen, Fluidd or a slicer), not through this app"


def _job_file(lease: Lease) -> str:
    """The file a manual-print claim is for, read back out of its reason."""
    return lease.reason.split(_MANUAL_REASON, 1)[0] or "a manual print"


def _held_error(lease: Lease | None, *, action: str, lost_race: bool = False) -> LeaseHeld:
    if lease is None:  # pragma: no cover - defensive
        return LeaseHeld(f"Cannot {action}: the lease changed hands while checking.")
    minutes = round(lease.seconds_remaining / 60, 1)
    if lease.manual:
        return LeaseHeld(
            f"Cannot {action}: a print is running that was started at the printer, not "
            f"through this app ({_job_file(lease)}). The machine is taken "
            "until it finishes.",
            hint=(
                "Ask around the room who started it. A maintainer can take the "
                "print over with break_printer_lease (recorded in the audit log) to pause "
                "or cancel it; emergency_stop always works."
            ),
            lease=lease.to_payload(),
        )
    preamble = (
        "Another agent acquired the lease at the same moment"
        if lost_race
        else f"Cannot {action}"
    )
    return LeaseHeld(
        f"{preamble}: {lease.owner} is driving this printer"
        + (f" ({lease.reason})" if lease.reason else "")
        + f", for another {minutes} min.",
        hint=(
            "Wait for them, or line your file up with queue_jobs so it runs after "
            "theirs instead of competing for the machine. If they have clearly gone "
            "away, break_printer_lease overrides this — and records that you did."
        ),
        lease=lease.to_payload(),
    )
