# Going live safely

The graduated path from read-only to a real print. The ordering is not
bureaucracy: each step tests a different failure mode, and the ones that can only
fail loudly come before the ones that can fail quietly.

> **DANGER: What is and is not established**
>
> The whole test suite and every safety proof run against an **in-process mock
> Moonraker**.
> They establish that the *logic* is right. They do **not** establish that the
> firmware behaves as the mock does — and the gap between those two is where every
> live bug so far has lived.
>
> On the lab's Max 4, capability discovery and **one complete 27-minute PLA print**
> are qualified, token flow included. That is one machine and one job. It means the
> write path works, not that it is blessed — and on a machine this has not been run
> against, you are back to Step 0 below.

> **INFO: More than one of you will be pointed at the same printer**
>
> Agents now coordinate through a **lease** stored on the printer, so a second agent
> is refused mutating calls while you work, and `start_print` requires claiming the
> machine first. It is cooperative, not a lock: the touchscreen and Fluidd ignore it.
> Read [sharing the printer](sharing-the-printer.md) before several people start
> using one machine.

## Why the mock is not enough

Four bugs were found by connecting to the real printer, each invisible to the
test suite by construction:

| Found live | Why the mock could not catch it |
|---|---|
| `soft_abort` reported **absent** on a printer that has the routes | Discovery searched Klipper's endpoint list for routes QIDI adds to *Moonraker*. Detection now probes with a GET: **405 = exists (POST-only), 404 = absent**. aiohttp answers 404 where the firmware answers 405, so the path was untestable. |
| `httpx` honoured `http_proxy` for **printer traffic** | The mock is loopback, and loopback bypasses the proxy. On a machine exporting `all_proxy`, the first live connection would have sent heater targets and print starts to an external host. Both clients now set `trust_env=False`. |
| `upload_gcode` rejected `.gcode.3mf` | The mock accepted whatever the tests uploaded. `.gcode.3mf` is what OrcaSlicer exports and what QIDI's own screen writes — i.e. the format of *every* file in a stock library. |
| `SET_AI_DETECT_RESULT` screened as harmless | Reading the real object model turned up `ai_func_manager` and a command that lets a caller *forge* a spaghetti verdict (`STATE=1`) or **suppress a real one** (`STATE=0`). The mock had no AI module, so there was nothing to forge a verdict to. Now denied outright: nothing legitimate needs an agent to impersonate the camera. |

The pattern: **the mock passes because it implements what we believed.** Expect
more of this class on first write, and prefer steps whose failure is immediately
visible.

## Step 0 — read-only

Start with `QIDI_READ_ONLY=1`, as in [the README's "Configure in your MCP
client" section](../../README.md#configure-in-your-mcp-client). In
this mode mutating tools are refused **at registration**, before any network I/O,
driven by each tool's own annotations. A tool that forgot its own check is still
refused.

Confirm, in one session:

- `get_capabilities` matches the machine in front of you — heaters, fans, axis travel.
- `get_printer_status` reports `ready`, not a degraded state.
- `get_safety_policy` shows the envelope you expect.
- `get_audit_log` shows the reads and no writes.

> **TIP: `emergency_stop` works even here**
>
> It is the one tool read-only mode does not shield, deliberately. Know how to
> reach it before you turn read-only off — and know that it shuts Klipper down, so
> recovery needs `restart_firmware`.

## Step 1 — something reversible and visible

Turn read-only off. The first write should be one whose success or failure you can
see from across the room, with no thermal or mechanical consequence:

```text
Turn the case light on. Then beep.
```

`set_case_light` and `beep` are `LOW` risk — no confirmation. If the light comes
on, the whole write path works: transport, auth (there is none), the policy gate,
the audit log. If it does not, nothing is at stake.

## Step 2 — heat, with someone watching

```text
Set the bed to 60 °C.
```

`THERMAL`, so it needs `confirm=true`. 60 °C is chosen because it is well inside
every ceiling, it is a temperature the bed reaches quickly, and a runaway would be
obvious long before it mattered.

Watch that:

- `wait_for_temperature` returns when it actually arrives, rather than hanging.
- `get_temperatures` tracks the climb.
- `cooldown` brings it back down. This one never consults capability discovery — turning hardware **off** is never refused on capability grounds.

Then try a refusal on purpose, because a limit you have never seen fire is a limit
you are trusting on faith:

```text
Set the extruder to 374 °C.
```

Expect `limit_exceeded` naming the 300 °C ceiling. The hardware `max_temp` is 375,
but that is the **MCU shutdown trip**, not a target.

## Step 3 — motion, on a clear bed

Clear the plate first. Then, in this order:

1. `home_axes` — establishes the reference the interlocks depend on. Until an axis is homed, absolute moves on it are refused.
2. `move_relative` with a small Z lift — `MOTION`, needs `confirm=true`.
3. `get_position` — confirms the machine agrees with where it was told to go.

Try `move_absolute` beyond the bed and watch it refuse against the real axis
limits (x −2…392, y −5…410, z −5…342). Then `motors_off`.

> **WARNING: Motion is where a wrong assumption becomes a crash**
>
> The envelope is checked against `printer.cfg`'s ranges, and it applies on the
> raw G-code path too — `M104 S374` and `G1 X999` are both refused. But bounds
> checking cannot know about a fixture you left clamped to the plate. Look at the
> machine.

## Step 4 — calibration

Calibration is slow and mechanical but not destructive, which makes it a good
rehearsal for long operations.

- `bed_mesh_calibrate`, then `get_bed_mesh` to read the result as numbers.
- `probe_accuracy` for probe repeatability.
- `z_tilt_adjust` for the dual-Z gantry.
- `input_shaper_calibrate` if you care about ringing — this one moves the toolhead hard, so watch it.

Two of these write to `printer.cfg` if you let them. This server **never issues
`SAVE_CONFIG` implicitly** — the deny-list covers `SAVE_CONFIG`, and also QIDI's
`SAVE_CONFIG_QD`, which saves without the restart that would make it obvious.

## Step 5 — a real print

Upload a small, short, known-good file. `start_print` is `PRINT` tier, so it needs
a token — and that token binds the file's **size and mtime**, not just its name,
so the bytes behind an approved filename cannot change inside the confirmation
window.

It also needs the **printer lease**, so the rest of the team can see the machine is
yours before it is committed for the next half hour:

```text
Claim the printer for the next 40 minutes — I'm printing the bracket.
```

That is `acquire_printer_lease`, and `release_printer_lease` when you are done. If
somebody else holds it you are refused, by name, with how long is left. The lease is
**cooperative** — it does not bind the machine's own touchscreen or Fluidd — so it
replaces none of the looking described below. See
[sharing the printer](sharing-the-printer.md).

> **WARNING: Check the file's material, not just its name**
>
> The first live print was nearly run from a file whose name said nothing about
> this: it was sliced for PETG, carrying `M109 S240` and a `HOTENDTEMP=280` purge.
> Sent to a machine loaded with PLA that degrades and clogs rather than failing
> cleanly. `get_file_metadata` reports the slicer's filament type and temperatures
> — read them before you approve the token, because the envelope cannot know which
> spool is actually loaded.
>
> The camera *does* work — `get_webcam_snapshot` via the nginx `/webcam/` proxy on
> port 80 — but it cannot tell you what is loaded, and the filament switch reports
> presence only. Look at the machine.

> **DANGER: A snapshot of an unlit chamber is a black rectangle, not a look at the printer**
>
> This printer turns its case light off whenever the touchscreen sleeps
> (`smart_output_pin caselight` follows `SCREEN`). The camera keeps serving
> perfectly well-formed 1920x1080 JPEGs; they are simply black. On 2026-09-16 an
> agent took three of them, described a clean plate and then a finished Benchy in
> convincing detail, and reported the print a success. The bed was empty — the job
> had extruded nothing for 22 minutes — and nobody knew for a day, because the only
> "observation" in the loop had been invented.
>
> `get_webcam_snapshot` now measures the frame and refuses one with no detail in it
> (`error: "frame_too_dark"`, with the case-light state and a pointer to
> `set_case_light`), so this specific trap is closed. The general rule it cost us is
> not: **call `set_case_light(on=true)` before you look, and never describe a frame
> you have not confirmed is exposed.**
>
> A lit frame runs about 0.08 compressed bytes/pixel on this camera; an unlit one
> about 0.02. If you are inspecting saved snapshots by hand, that ratio is the tell.

Before starting, worth having on:

```text
Enable noodle detection at MEDIUM before this print.
```

`set_ai_detection` turns on the Max 4's own spaghetti detection. Use the tool
rather than writing `enable_noodle_detection` with a raw `SAVE_VARIABLE` — it
range-checks the sensitivity and reports back whether the flag actually took.

> **WARNING: Nothing on this machine can tell you the filament stopped moving**
>
> `print_stats.filament_used` is *commanded* extrusion — arithmetic over the
> G-code's E moves, not a measurement. The only feed sensor is a presence switch,
> and a filament-motion sensor exists solely on Box-equipped machines
> (`box_count: 0` here). So a jam, a clog, or a drive gear chewing a flat into the
> filament produces a job that completes with a full filament total, correct
> temperatures throughout, and an empty plate. That is exactly what the 2026-09-16
> run was. If a print "succeeded" but there is no part, do not trust the numbers —
> heat the hotend and extrude 20 mm by hand while watching the nozzle.

And know the escape hatches, which are deliberately cheap to reach:

| If | Use | Gate |
|---|---|---|
| A heater is climbing wrongly | `abort_heating` | **none** |
| A macro is stuck or running away | `abort_macro` | one-step confirm |
| The print is bad but the machine is fine | `cancel_print` | token |
| Anything is physically wrong | `emergency_stop` | **never gated** |

> **TIP: Reach for the soft abort first**
>
> `emergency_stop` shuts Klipper down and needs a firmware restart to recover.
> `abort_heating` and `abort_macro` exist so the gentle option is always available
> — and `get_capabilities` advertises them so an agent knows they are there. The
> bug where `soft_abort` was reported absent mattered precisely because it hid
> them.

## A QIDI Box

The Box's read tools (`get_box_status`, `list_box_filament_options`) have run against a
real Box; none of its write tools (`set_box_option`, `set_box_slot_filaments`) has, and
`load_box_slot` refuses on the one firmware seen because it lacks `E_LOAD`. Before
writing, work through
**[The QIDI Box: first contact](box-first-contact.md)** — it starts read-only, tells
you what to record, and lists every assumption the code makes about the firmware.

## After going live

- **Read `get_audit_log`.** Every non-read action, including refusals, with the tier and arguments. It answers "what did the agent actually do to my printer?"
- **The log rotates itself.** At 5 MB it rolls to `audit.jsonl.1`, keeping three
  generations, so a server left running for weeks cannot fill the disk. Tune with
  `QIDI_AUDIT_MAX_BYTES` / `QIDI_AUDIT_KEEP`; set the former to `0` if you ship the
  file somewhere else and want it untouched.
- **Release the lease.** `release_printer_lease` when your job is done, rather than
  letting it lapse — the next person should not have to wait out your timeout, or
  decide whether breaking your claim is rude.
- **Report firmware-vs-mock divergences.** They are the valuable findings. Each of the four above became a fix plus a regression test, and two of them changed the mock so the next person's tests are honest — including teaching it to answer `405` where aiohttp would default to `404`.

## If something goes wrong

1. `emergency_stop` if it is physical. Do not deliberate.
2. `get_printer_status` — a degraded state is reported honestly rather than being forged into `standby`; an *unknown* print state is treated as possibly printing.
3. `get_gcode_log` — the Fluidd console history, where Klipper explains itself.
4. `restart_firmware` to recover from a shutdown. `DESTRUCTIVE`, so it needs a token, and it is reachable even in a degraded state — a recovery tool that requires a healthy printer is useless.
