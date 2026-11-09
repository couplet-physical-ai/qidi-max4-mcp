# Sharing one printer between several agents

There is one printer and more than one person with an agent pointed at it. This page
is about what happens when two of them decide to print at the same time.

## The problem, concretely

Every agent runs its **own copy** of this server, on its own laptop. There is no
shared process. So nothing in the code could coordinate them: an in-process lock
protects an agent from itself, and a file lock protects one laptop. Neither notices
the person across the room.

Before this existed, two agents told to print at the same moment would both succeed
in calling `start_print`. The second one would land on a bed that already had a part
on it.

## What was added

A **lease**: one claim, stored *on the printer*, in a namespace of Moonraker's own
key-value database. The printer is the only thing every agent can already see, which
is what makes it the right place for this.

```text
acquire_printer_lease(reason="bracket v3, ~40 min")
  → you now hold it; other agents are refused mutating calls
start_print(...)                # allowed, because you hold it
release_printer_lease()         # give it back when you are done
```

Another agent that tries to touch the machine meanwhile gets a refusal that tells it
who to talk to:

```json
{
  "ok": false,
  "error": "lease_held",
  "message": "Cannot run set_temperature: alice@laptop is driving this printer
              (bracket v3, ~40 min), for another 30.0 min.",
  "hint": "Wait for them, or line your file up with queue_jobs so it runs after
           theirs instead of competing for the machine. If they have clearly gone
           away, break_printer_lease overrides this — and records that you did."
}
```

## It is cooperative, and that word is load-bearing

> **WARNING: This is not a lock, and treating it as one is how a print gets ruined**
>
> The lease stops a well-behaved agent running *this* server. It does **not** bind:
>
> * the printer's **own touchscreen** — somebody standing at the machine;
> * **Fluidd** or any other web UI in a browser (though a print either of them
>   starts now takes the lease, [see below](#prints-started-at-the-machine-take-the-lease-too));
> * an **older build** of this server, or one run with `QIDI_LEASE_ENFORCE=0`;
> * anyone who calls `break_printer_lease`, which is deliberately available.
>
> So it does not remove the reason to look at the machine before committing it.
> What it does is turn a silent collision into a loud, attributable one.

A real lock would need Moonraker to offer compare-and-set, which it does not.
Claiming otherwise in these docs would invite an agent to skip the one check that
actually works — looking.

## Prints started at the machine

Cooperative, not a lock: the touchscreen and Fluidd ignore the lease, and prints
started there do not take the lease in this release (the during-print interlock
still refuses motion/thermal changes while any job runs, from *any* agent,
regardless of who holds the lease). `lease.py` has the plumbing for an
automatic claim on a print nobody took (`LeaseManager.cover_job`), but nothing in
this release calls it — that required a watcher polling Moonraker's job history,
which is not part of this distribution. If you wire one up yourself,
`get_printer_lease` and the lease machinery will reflect whatever it claims; out
of the box, a print started at the machine simply does not show up as a lease at
all. `emergency_stop` always works regardless of who, if anyone, holds the lease.

## The rules the gate applies

| You want to | Lease needed? |
| --- | --- |
| Read anything — status, temps, job, files | **No.** An agent that cannot see the machine is more dangerous than one that can. |
| `emergency_stop`, `abort_heating`, `abort_macro` | **No.** An escape hatch another agent can lock is not an escape hatch. |
| Pause the job queue (`control_job_queue action='pause'`) | **No.** It only prevents a *future* automatic start — the safe direction. |
| Heat, move, cancel, cool down, delete a file | Not while **somebody else** holds it. |
| `start_print` | **Yes, explicitly** — even on a free printer. |
| Release the queue (`control_job_queue action='start'`) | **Yes, explicitly.** |
| `queue_jobs` **when the queue is already `ready`** | **Yes** — see below. |

Two of those deserve their reasoning stated.

**`cooldown` and `cancel_print` are *not* exempt.** Both are harmless to the machine
and ruinous to somebody else's eight-hour print, which is precisely the takeover this
exists to prevent. `abort_heating` remains free for a genuine thermal emergency.

**Queueing onto a live queue is starting a print.** With `queue_state` at `ready`,
Moonraker begins the next job the moment the printer goes idle. So "just adding a
file to the list" commits the machine, and is gated like it. On a *paused* queue it
genuinely is only a note for later, and needs nothing but `confirm=true`.

## The polite alternative to waiting

When somebody else is printing, the answer is usually not to wait for the machine
but to line your job up behind theirs:

```text
get_job_queue                         # what is already waiting, and is it live?
queue_jobs(filenames=["mine.gcode"], confirm=true)
```

That turns a collision into an ordering. The queue is Moonraker's, shared by
everybody, and survives your agent exiting.

## A forgotten lease cannot strand the machine

Two independent protections, because "the intern went home holding the lease" is a
certainty, not a risk:

1. **It expires.** Default 60 minutes (`QIDI_LEASE_MINUTES`); `renew_printer_lease`
   extends it for a long job. Once it lapses, the next agent simply takes it.
2. **It can be broken.** `break_printer_lease` force-releases someone else's claim.
   Two-step confirmation, and the audit log records who broke whose.

    Breaking a lease does **not** stop a running print. If a job is actually going and
    you need it stopped, that is `cancel_print` or `emergency_stop`.

## Seeing who is driving

Three places, no setup:

* **`get_printer_lease`** — holder, their stated reason, minutes left, and whether it
  is you.
* **`qidi-dashboard`** — a badge in the header and a "Who is driving" card. See
  [the README's "Live dashboard" section](../../README.md#live-dashboard).
* **`get_capabilities`** and **`get_safety_policy`** — both report a `sharing` block,
  including whether coordination is possible on this printer at all.

## When coordination is not available

If a printer's Moonraker lacks the `database` component, there is nowhere shared to
put a lease. That case **fails open**: the server keeps working, reports
`coordination: "unavailable"` with a note saying so, and every other gate still
applies. Refusing all work because the coordination channel is missing would turn an
absent convenience into an outage.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `QIDI_LEASE_ENFORCE` | `1` | Refuse mutating actions while another agent holds the lease. |
| `QIDI_REQUIRE_LEASE_FOR_PRINT` | `1` | Require an explicit claim before starting a print or releasing the queue. |
| `QIDI_LEASE_MINUTES` | `60` | Default duration when a caller does not say. |
| `QIDI_AGENT_NAME` | `user@host` | How you appear to everyone else. Set it to something a colleague will recognise. |
| `QIDI_LEASE_HOLDER_KEY` | unset | A secret that makes successive server processes one holder. See below. |

Enforcement only ever blocks you when **somebody else** holds the lease, so a single
user never meets it. If you are genuinely alone on a printer and want the old
behaviour, `QIDI_REQUIRE_LEASE_FOR_PRINT=0` drops the explicit-claim step; it does not
make the server lie about who holds the lease.

## Keeping your lease across a restart

The holder is a server *process*, not a person, so two shells of yours are still two
agents. That has a cost for anything that does not keep one process alive: a script
that runs one `qidi-mcp` per command, or a long-running client session you restart. The process that
claimed the printer is gone, and the next one is refused by your own lease. It
cannot `release_printer_lease` it either, so the only ways out are
`break_printer_lease` on yourself or waiting for the lease to expire.

Set `QIDI_LEASE_HOLDER_KEY` to a secret, the same one for every process that should
count as one agent. The holder id becomes a hash of that key. The id is visible to
everyone in `get_printer_lease`, but knowing it does not let anyone claim to be you;
only the key does. Two agents that run *at the same time* need different keys,
otherwise the server treats them as one agent.

## Next

* [Going live safely](going-live.md) — what to check before the first real print.
* [Live dashboard](../../README.md#live-dashboard) — watching a job, and who owns it.
