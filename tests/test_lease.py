"""Two agents, one printer.

The scenario every test here is about: two people each tell their own agent to
print something, at roughly the same time, against the same machine. Before the
lease, both would have succeeded in *starting* — the second one landing on a bed
that already had a part on it, or worse, interrupting nothing and simply being
refused at random depending on timing.

Each agent gets its own ``Runtime`` here, because that is the real topology: two
processes, two laptops, one printer. They share only the mock Moonraker — which is
exactly the constraint the lease is designed around, since the printer's database
is the only state both can see.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from qidi_mcp.config import load_settings
from qidi_mcp.errors import ReadOnlyMode
from qidi_mcp.lease import NAMESPACE, Lease, LeaseHeld, LeaseRequired, default_owner
from qidi_mcp.policy import Risk
from qidi_mcp.runtime import Runtime, lease_exempt, lease_required


@pytest_asyncio.fixture
async def make_agent(moonraker):
    """Build independent Runtimes against the same mock printer.

    Each one is a separate ``Runtime`` on purpose: that is the real topology, two
    processes on two laptops sharing nothing but the machine. A fixture that handed
    back one shared Runtime would test nothing, because a single process trivially
    knows what it is doing.
    """
    base_url, _ = moonraker
    created: list[Runtime] = []

    def _make(name: str, **env: str) -> Runtime:
        settings = load_settings(
            {
                "QIDI_PRINTER_URL": base_url,
                "QIDI_TEST_MODE": "1",
                "QIDI_READ_ONLY": "0",
                "QIDI_AGENT_NAME": name,
                **env,
            }
        )
        rt = Runtime(settings)
        created.append(rt)
        return rt

    try:
        yield _make
    finally:
        for rt in created:
            await rt.aclose()


# -- the core contention case ----------------------------------------------


async def test_second_agent_cannot_take_the_printer(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")

    await alice.lease.acquire("bracket v3, ~40 min", minutes=30)

    with pytest.raises(LeaseHeld) as caught:
        await bob.lease.acquire("my thing", minutes=30)

    # The refusal has to be actionable: who, why, how long.
    message = str(caught.value)
    assert "alice@laptop" in message
    assert "bracket v3" in message
    assert caught.value.extra["lease"]["owner"] == "alice@laptop"
    assert caught.value.hint and "queue" in caught.value.hint.lower()


async def test_bobs_mutating_calls_are_refused_while_alice_holds_it(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)

    for tool, risk in (
        ("set_temperature", Risk.THERMAL),
        ("home_axes", Risk.MOTION),
        ("start_print", Risk.DESTRUCTIVE),
        ("cancel_print", Risk.PRINT),
        ("cooldown", Risk.THERMAL),
        ("delete_file", Risk.DESTRUCTIVE),
    ):
        with pytest.raises(LeaseHeld):
            await bob.authorize(tool, risk, {})


async def test_alice_is_not_blocked_by_her_own_lease(make_agent):
    alice = make_agent("alice@laptop")
    await alice.lease.acquire("printing", minutes=30)
    assert await alice.authorize("set_temperature", Risk.THERMAL, {}, confirm=True) is None


async def test_reads_are_never_blocked(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)

    # Watching is always allowed -- an agent that cannot see the machine is more
    # dangerous than one that can.
    assert await bob.authorize("get_printer_status", Risk.READ, {}) is None
    assert await bob.authorize("get_temperatures", Risk.READ, {}) is None
    status = await bob.lease.status()
    assert status["held"] is True
    assert status["you_hold_it"] is False
    assert status["lease"]["owner"] == "alice@laptop"


async def test_safety_escapes_are_never_blocked(make_agent):
    """An escape hatch another agent can lock is not an escape hatch."""
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)

    assert await bob.authorize("emergency_stop", Risk.EMERGENCY, {}) is None
    assert await bob.authorize("abort_heating", Risk.THERMAL, {}, confirm=True) is None
    assert await bob.authorize("abort_macro", Risk.THERMAL, {}, confirm=True) is None
    # And stopping the queue from feeding more work is the safe direction.
    assert (
        await bob.authorize("control_job_queue", Risk.PRINT, {"action": "pause"}, confirm=True)
        is None
    )


# -- expiry, renewal, release ----------------------------------------------


async def test_an_expired_lease_frees_the_machine(make_agent):
    """A forgotten lease must not strand the printer forever."""
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)

    stale = await alice.lease.current()
    assert stale is not None
    stale.expires_at = time.time() - 1
    await alice.lease._write(stale)

    assert await bob.lease.current() is None
    assert await bob.authorize("home_axes", Risk.MOTION, {}, confirm=True) is None
    got = await bob.lease.acquire("my turn", minutes=10)
    assert got.owner == "bob@desktop"


async def test_release_hands_it_over_immediately(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)
    assert (await alice.lease.release())["released"] is True

    got = await bob.lease.acquire("my turn", minutes=10)
    assert got.owner == "bob@desktop"


async def test_bob_cannot_release_alices_lease(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)
    with pytest.raises(LeaseHeld):
        await bob.lease.release()
    with pytest.raises(LeaseHeld):
        await bob.lease.renew(30)


async def test_renew_extends_without_changing_ownership(make_agent):
    alice = make_agent("alice@laptop")
    first = await alice.lease.acquire("long print", minutes=5)
    renewed = await alice.lease.renew(60)
    assert renewed.holder_id == first.holder_id
    assert renewed.expires_at > first.expires_at
    # acquired_at is preserved, so "how long have they had it" stays honest.
    assert renewed.acquired_at == pytest.approx(first.acquired_at, abs=0.01)


async def test_break_is_possible_but_attributed(make_agent):
    """A stranded machine must be recoverable, loudly."""
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("went home", minutes=600)

    broken = await bob.lease.break_lease()
    assert broken["broken"] is True
    assert broken["was"]["owner"] == "alice@laptop"
    assert broken["broken_by"] == "bob@desktop"
    assert await bob.lease.current() is None


async def test_two_shells_belonging_to_one_person_are_still_two_agents(make_agent):
    """holder_id is per process, not per user: same name, still exclusive."""
    first = make_agent("alice@laptop")
    second = make_agent("alice@laptop")
    assert first.lease.holder_id != second.lease.holder_id

    await first.lease.acquire("shell one", minutes=30)
    with pytest.raises(LeaseHeld):
        await second.lease.acquire("shell two", minutes=30)


# -- requiring an explicit claim -------------------------------------------


async def test_start_print_needs_a_lease_even_on_a_free_printer(make_agent):
    alice = make_agent("alice@laptop")
    assert await alice.lease.current() is None

    with pytest.raises(LeaseRequired) as caught:
        await alice.authorize("start_print", Risk.DESTRUCTIVE, {"filename": "a.gcode"})
    assert "acquire_printer_lease" in (caught.value.hint or "")

    await alice.lease.acquire("bracket", minutes=30)
    ticket = await alice.authorize("start_print", Risk.DESTRUCTIVE, {"filename": "a.gcode"})
    assert ticket is not None and ticket["status"] == "confirmation_required"


async def test_releasing_the_queue_needs_a_lease_but_pausing_does_not(make_agent):
    alice = make_agent("alice@laptop")
    with pytest.raises(LeaseRequired):
        await alice.authorize("control_job_queue", Risk.PRINT, {"action": "start"}, confirm=True)
    assert (
        await alice.authorize("control_job_queue", Risk.PRINT, {"action": "pause"}, confirm=True)
        is None
    )


async def test_queueing_onto_a_LIVE_queue_needs_a_lease(make_agent):
    """The subtle one: with queue_state 'ready', enqueueing *is* starting a print."""
    alice = make_agent("alice@laptop")

    # Paused queue: lining up a file is just a note for later.
    assert (
        await alice.authorize(
            "queue_jobs", Risk.PRINT, {"filenames": ["a.gcode"], "queue_state": "paused"}, confirm=True
        )
        is None
    )
    # Live queue: Moonraker will start it the moment the printer is idle.
    with pytest.raises(LeaseRequired):
        await alice.authorize(
            "queue_jobs", Risk.PRINT, {"filenames": ["a.gcode"], "queue_state": "ready"}, confirm=True
        )


async def test_queueing_is_allowed_while_someone_else_prints(make_agent):
    """The whole point of the queue: cooperate instead of competing."""
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing for an hour", minutes=60)

    # Bob may not touch the machine...
    with pytest.raises(LeaseHeld):
        await bob.authorize("start_print", Risk.DESTRUCTIVE, {"filename": "b.gcode"})
    # ...but with enforcement on, queueing is a mutating call too, so it is also
    # refused while Alice holds it. Bob's route is to wait or ask -- which is the
    # honest answer, and better than silently appending to her queue.
    with pytest.raises(LeaseHeld):
        await bob.authorize(
            "queue_jobs", Risk.PRINT, {"filenames": ["b.gcode"], "queue_state": "paused"}, confirm=True
        )


# -- configuration and failure modes ---------------------------------------


async def test_enforcement_can_be_turned_off(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop", QIDI_LEASE_ENFORCE="0", QIDI_REQUIRE_LEASE_FOR_PRINT="0")
    await alice.lease.acquire("printing", minutes=30)

    # Documented escape for a single-user setup. It does not lie about the lease.
    assert await bob.authorize("home_axes", Risk.MOTION, {}, confirm=True) is None
    assert (await bob.lease.status())["lease"]["owner"] == "alice@laptop"


async def test_read_only_mode_reports_read_only_not_lease_held(make_agent):
    """The most specific refusal is the useful one."""
    alice = make_agent("alice@laptop")
    await alice.lease.acquire("printing", minutes=30)
    watcher = make_agent("dashboard@laptop", QIDI_READ_ONLY="1")

    with pytest.raises(ReadOnlyMode):
        await watcher.authorize("set_case_light", Risk.LOW, {"on": True})


async def test_missing_database_component_fails_open(make_agent, monkeypatch):
    """A printer without the component must not become unusable."""
    alice = make_agent("alice@laptop")

    async def no_database(*_a, **_k):
        from qidi_mcp.errors import MoonrakerHTTPError

        raise MoonrakerHTTPError(404, "no such route")

    monkeypatch.setattr(alice.client, "get", no_database)
    alice.lease._supported = None

    assert await alice.lease.supported() is False
    status = await alice.lease.status()
    assert status["coordination"] == "unavailable"
    assert status["enforced"] is False
    # And the gate lets work through rather than bricking the server.
    assert await alice.authorize("home_axes", Risk.MOTION, {}, confirm=True) is None


async def test_an_unreachable_printer_is_not_remembered_as_having_no_database(
    make_agent, monkeypatch
):
    """Not being able to ask is not the same as the answer being "no".

    A deployment that looked at a printer while a firewall blocked it, cached
    "no database" for the life of the process, and then refused every print there
    with lease_required once it became reachable, would be wrong in exactly this way.
    """
    from qidi_mcp.errors import ConnectionFailed

    alice = make_agent("alice@laptop")
    real_get = alice.client.get

    async def unreachable(*_a, **_k):
        raise ConnectionFailed("could not connect")

    monkeypatch.setattr(alice.client, "get", unreachable)
    alice.lease._supported = None
    assert await alice.lease.supported() is False
    assert alice.lease._supported is None, "a failed connection must not be cached"

    monkeypatch.setattr(alice.client, "get", real_get)
    assert await alice.lease.supported() is True
    status = await alice.lease.status()
    assert status["coordination"] == "available"
    await alice.lease.acquire("printing", minutes=30)
    assert alice.lease.holds(await alice.lease.current())


async def test_refusals_are_recorded_with_who_holds_it(make_agent):
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")
    await alice.lease.acquire("printing", minutes=30)

    with pytest.raises(LeaseHeld):
        await bob.authorize("home_axes", Risk.MOTION, {}, confirm=True)

    entry = bob.policy.audit.tail(1)[0]
    assert entry["tool"] == "home_axes"
    assert entry["outcome"] == "refused"
    assert "alice@laptop" in entry["detail"]


async def test_lease_lives_in_a_namespace_the_printer_shares(make_agent):
    """Not in this process: another agent must be able to see it."""
    alice = make_agent("alice@laptop")
    await alice.lease.acquire("printing", minutes=30)

    raw = await alice.client.get(
        "/server/database/item", params={"namespace": NAMESPACE, "key": "lease"}
    )
    assert raw["value"]["owner"] == "alice@laptop"


# -- pure-function edges ---------------------------------------------------


def test_malformed_lease_data_frees_the_machine_rather_than_wedging_it():
    for junk in (None, "nonsense", 42, {}, {"holder_id": "x"}, {"expires_at": "soon"}):
        assert Lease.from_value(junk) is None


def test_owner_name_is_recognisable_and_overridable():
    assert default_owner("alex@bench") == "alex@bench"
    assert "@" in default_owner("")
    assert len(default_owner("x" * 500)) <= 120


def test_exempt_and_required_predicates_are_argument_aware():
    assert lease_exempt("emergency_stop", {})
    assert lease_exempt("control_job_queue", {"action": "pause"})
    assert not lease_exempt("control_job_queue", {"action": "start"})
    assert not lease_exempt("cooldown", {})  # ruins someone else's print
    assert not lease_exempt("cancel_print", {})

    assert lease_required("start_print", {})
    assert lease_required("control_job_queue", {"action": "start"})
    assert lease_required("queue_jobs", {"queue_state": "ready"})
    assert not lease_required("queue_jobs", {"queue_state": "paused"})
    assert not lease_required("set_temperature", {})


async def test_simultaneous_acquire_produces_exactly_one_winner(make_agent):
    """The case that actually happens: two agents told to print at the same moment.

    Neither reads the other's lease, because at the instant they look there is not
    one. Both write. Without the read-back in acquire() both would believe they hold
    it, and the loser would find out by colliding with a real print. The write is
    last-writer-wins, so exactly one holder_id survives -- and the verify step is what
    turns the loser's false success into an honest refusal.
    """
    agents = [make_agent(f"agent{i}@host") for i in range(5)]

    results = await asyncio.gather(
        *(a.lease.acquire(f"job {i}", minutes=10) for i, a in enumerate(agents)),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, Lease)]
    losers = [r for r in results if isinstance(r, LeaseHeld)]

    assert len(winners) == 1, f"expected one winner, got {len(winners)}"
    assert len(losers) == len(agents) - 1, "every loser must be told, not silently dropped"

    # And the survivor is the one the printer actually reports, for everybody.
    stored = await agents[0].lease.current()
    assert stored is not None
    assert stored.holder_id == winners[0].holder_id
    for agent in agents:
        assert (await agent.lease.status())["lease"]["holder_id"] == winners[0].holder_id


async def test_the_race_check_is_what_catches_it(make_agent, monkeypatch):
    """Prove the read-back is load-bearing rather than decorative."""
    alice = make_agent("alice@laptop")
    bob = make_agent("bob@desktop")

    # Both see a free printer, then Bob's write lands after Alice's.
    real_write = alice.lease._write

    async def write_then_let_bob_overwrite(lease):
        await real_write(lease)
        await bob.lease._write(
            Lease(
                holder_id=bob.lease.holder_id,
                owner=bob.lease.owner,
                reason="sneaked in",
                acquired_at=time.time(),
                expires_at=time.time() + 600,
                renewed_at=time.time(),
            )
        )

    monkeypatch.setattr(alice.lease, "_write", write_then_let_bob_overwrite)

    with pytest.raises(LeaseHeld) as caught:
        await alice.lease.acquire("bracket", minutes=10)
    assert "same moment" in str(caught.value)
    assert caught.value.extra["lease"]["owner"] == "bob@desktop"


# -- one holder across short-lived processes (QIDI_LEASE_HOLDER_KEY) --------


async def test_a_restarted_process_cannot_release_its_own_lease_without_a_key(make_agent):
    """The bug: a stdio CLI runs one process per command, so the process that
    claimed the printer is gone by the time the next command wants to use it."""
    first = make_agent("alice@laptop")
    await first.lease.acquire("request-start", minutes=30)
    later = make_agent("alice@laptop")  # same person, next command
    with pytest.raises(LeaseHeld):
        await later.lease.release()


async def test_a_holder_key_makes_successive_processes_one_holder(make_agent):
    first = make_agent("alice@laptop", QIDI_LEASE_HOLDER_KEY="alice-cli-secret")
    lease = await first.lease.acquire("request-start", minutes=30)
    later = make_agent("alice@laptop", QIDI_LEASE_HOLDER_KEY="alice-cli-secret")
    assert later.lease.holder_id == first.lease.holder_id
    assert later.lease.holds(await later.lease.current())

    renewed = await later.lease.acquire("confirm-start", minutes=45)
    assert renewed.acquired_at == pytest.approx(lease.acquired_at)  # resumed, not re-taken
    assert (await later.lease.release())["released"] is True


async def test_a_holder_key_does_not_let_anyone_else_in(make_agent):
    alice = make_agent("alice@laptop", QIDI_LEASE_HOLDER_KEY="alice-cli-secret")
    await alice.lease.acquire("printing", minutes=30)
    public_id = (await alice.lease.status())["lease"]["holder_id"]

    bob = make_agent("bob@desktop", QIDI_LEASE_HOLDER_KEY="bob-secret")
    with pytest.raises(LeaseHeld):
        await bob.lease.acquire("mine now", minutes=30)
    # The id everyone can see is not the key: presenting it as a key gets nothing.
    mallory = make_agent("mallory@x", QIDI_LEASE_HOLDER_KEY=public_id)
    assert mallory.lease.holder_id != public_id
    with pytest.raises(LeaseHeld):
        await mallory.lease.release()


def test_holder_id_is_a_hash_of_the_key_and_random_without_one():
    from qidi_mcp.lease import holder_id_for

    assert holder_id_for("k") == holder_id_for("k")
    assert holder_id_for("k") != holder_id_for("k2")
    assert holder_id_for("") != holder_id_for("")
    assert len(holder_id_for("k")) == len(holder_id_for("")) == 16


def test_the_holder_key_is_not_in_the_settings_repr():
    settings = load_settings(
        {
            "QIDI_PRINTER_URL": "http://127.0.0.1:7125",
            "QIDI_TEST_MODE": "1",
            "QIDI_LEASE_HOLDER_KEY": "super-secret-value",
        }
    )
    assert settings.lease_holder_key == "super-secret-value"
    assert "super-secret-value" not in repr(settings)
