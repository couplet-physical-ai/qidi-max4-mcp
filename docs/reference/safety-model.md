# Safety model

> **Note.** This page tracks the tools the server registers; it is hand-maintained
> in this release (the internal `scripts/build_mkdocs.py` generator that kept it in
> sync automatically is not part of this export). If you change a tool's risk tier
> or the deny-list, update this page too.

The envelope is enforced numerically, in code, before a byte reaches Moonraker —
not by asking the model nicely in a prompt.

## Why prose is not enough

An instruction like "do not exceed 300 °C" is advice a model may follow. A
comparison in `policy.py` that raises before the HTTP call is a property of the
system. Everything below is the second kind.

## Risk tiers

| Tier | Requirement | Examples |
|---|---|---|
| read | none | `get_printer_status`, `list_gcode_files` |
| low | none | `set_case_light`, `beep`, `abort_heating` |
| motion | `confirm=true` | `home_axes`, `move_relative`, `motors_off` |
| thermal | `confirm=true` | `set_temperature`, `chamber_heater` |
| print | token | `start_print`, `cancel_print` |
| destructive | token | `delete_file`, `restart_firmware`, `run_gcode` |
| emergency | **none, ever** | `emergency_stop` |

### Tokens are bound to arguments

A `confirm_token` is a hash of the tool name and the exact arguments, single-use,
expiring in 120 s. So it cannot be replayed, cannot be reused for a different
file, and cannot be issued for one action and spent on another. For `start_print`
the token additionally binds the file's **size and mtime** — otherwise the bytes
behind an approved filename could change inside the confirmation window.

## The numeric envelope

| Limit | Default | Override |
|---|---|---|
| Extruder | 300 °C (confirm above 260) | `QIDI_MAX_EXTRUDER_TEMP` |
| Bed | 120 °C (confirm above 100) | `QIDI_MAX_BED_TEMP` |
| Chamber | 65 °C (confirm above 55) | `QIDI_MAX_CHAMBER_TEMP` |
| Single jog | 100 mm | `QIDI_MAX_JOG_DISTANCE` |
| Single extrude | 100 mm | `QIDI_MAX_EXTRUDE_DISTANCE` |
| Feedrate | 12000 mm/min | `QIDI_MAX_FEEDRATE` |
| Velocity limit (`M203`) | 800 mm/s | `QIDI_MAX_VELOCITY` |
| Accel limit (`M201`/`M204`) | 30000 mm/s² | `QIDI_MAX_ACCEL` |
| Square corner velocity (`M205`) | 8 mm/s | `QIDI_MAX_SQUARE_CORNER_VELOCITY` |

The three kinematic ceilings default to the printer's own `printer.cfg` values.
Klipper lets `M201`/`M203`/`M204`/`M205` raise the toolhead's limits *above* what
was configured, so raw G-code is held to them.

An override can only **lower** a ceiling, never raise it above what the hardware
config allows — verified by an audit that tried `inf`, `nan` and `1e400`, all
refused at load.

## Interlocks

| Interlock | Blocks |
|---|---|
| `forbid_during_print` | Motion, heater changes, raw G-code and host restarts while a job runs. |
| `require_homed` | Absolute moves on an axis that has not been homed. |
| `require_extrude_temp` | Extrusion below a safe hotend temperature. |

These are evaluated against **freshly queried** state. They used to read a 0.75 s
cache, which meant a refused call primed the cache and the agent's retry was
authorised against pre-refusal state.

## G-code screening

`run_gcode` is the escape hatch, and the only reason it is safe is that it
resolves each line's command *exactly as Klipper does* — including skipping a
leading `N<number>` and handling space-less forms like `M109S200`.

> **The rule this came from.** A screen that parses its input differently from the executor is not a screen.
> `N1 SAVE_CONFIG` used to be screened as command `N1` — not deny-listed, so
> allowed — while Klipper executed `SAVE_CONFIG`. All classification now goes
> through `klipper_command_name()`, verified against upstream's real parser
> across 154,371 spellings with zero divergences.

`run_gcode` and `run_macro` share one screen: a macro call is assembled into its
G-code line and checked exactly as raw text would be. A script is split on newlines
only, as Klipper splits it; any other control character or Unicode line separator is
refused, because Python's `splitlines()` used to read `M104 S100
S374` as two lines
while Klipper ran one with `S=374`. Screening covers six things, because a name alone
is not enough:

- **The command name**, against the deny-list below. That includes the raw built-ins
  QIDI's macros keep reachable with `rename_existing` (`M104.6245197`, `G28.6245197`,
  `_BED_MESH_CALIBRATE`, `_KM_PAUSE_BASE`, …), the stock list plus any found in the
  connected printer's config, and every decimal G/M code the stock config does not define.
- **Heater targets** — `M104 S374`, `M109 R374`, `SET_HEATER_TEMPERATURE_SCALED TARGET=374`
  and the temperature parameters of QIDI's macros (`PRINT_START`, `PRINT_START_SET`
  HOTEND/HOTEND_TEMP/BED/BED_TEMP/CHAMBER, `CLEAR_NOZZLE`, `M600 R`,
  `EXTRUSION_AND_FLUSH HOTEND`, `M4029 S`, `M603`/`M604 S`, …) and of the Box
  (`E_LOAD S`, `BOX_PRINT_START HOTENDTEMP`) are refused by the same envelope that
  governs `set_temperature`. Values are checked before the deny-list, so a
  deny-listed macro's temperature is still bounded with the operator switch on: the
  planted `PRINT_START_SET HOTEND=374` + `_PRINT_START_PHASE_EXTRUDER` cannot reopen.
  The Box's other firmware commands (`MULTI_COLOR_*`, `EXTRUDER_LOAD`, …) have
  unpublished parameters: any whose name could hold a temperature (`S`, `R`, or
  containing TEMP, HOT, HEAT or TARGET) is held to the extruder envelope on the
  load/unload commands and refused on the rest. A decimal spelling is checked as its stem, so
  `M104.6245197 S374` meets the M104 check even with the operator switch on. `M109` and
  `M190` accept only `S`/`R`/`T`, because their macro forwards every other parameter
  to an inner one that would act on it. QIDI's heater macros scale the target; the
  screen reads the live scaling, checks the scaled value too, and refuses if the
  scaling cannot be read.
- **Kinematic limits** — `M201`/`M203`/`M204`/`M205` values are held to the velocity,
  acceleration and square-corner-velocity ceilings above.
- **Coordinates and offsets** — `X`/`Y`/`Z` of `G0`–`G3` against the axis envelope in
  absolute mode, tracked line by line (a `G90` after a `G91` restores the check);
  `X`/`Y`/`Z` of `PARK`/`G27`/`M600`/`PAUSE` always, Z floor included, because they
  switch to `G90` themselves; and `SET_GCODE_OFFSET`/`M290` within ±5 mm.
- **Parameter names** — every extended-command name must be letters, digits and
  underscores. Klipper keeps a quoted name verbatim and QIDI's print-start macros
  re-render their parameters, so `PRINT_START HOTEND=200 " HOTEND"=374` used to reach
  `M109 S374`. A name given more than once is accepted by `run_gcode` and checked at
  its last value, which is the one Klipper uses; `run_macro` refuses a repeated name.
- **Classic values** — on a letter-and-number command (`G27`, `M600`, `M104`, …) no
  value may contain `=`, a quote or a space, and an empty value on a checked
  parameter is refused. Klipper keeps `=20` in `G27 P1 Z=20` as the value;
  its own handlers reject it, but `G27`/`M600` forward it to `PARK`, whose `|float`
  reads it as 0, so the toolhead went to Z=0 while the screen had approved Z=20.
  `M117`/`M118` are exempt: their text is a message. `run_macro` writes these values
  straight after the letter (`M600 Z20`).
- **Argument-sensitive commands** — `BED_MESH_PROFILE LOAD=` is routine; `SAVE=`/`REMOVE=` are not.
  `SET_FILAMENT_SENSOR ENABLE=1` is fine; `ENABLE=0`, the primitive behind
  `DISABLE_ALL_SENSOR`, is not.

### Refused unless the operator allows it (102 commands)

These need **both** `allow_dangerous=true` from the agent **and**
`QIDI_ALLOW_DANGEROUS_GCODE=1` set by whoever starts the server, then the two-step
token. The agent's flag alone is not enough: it sits behind a token the agent redeems
itself, so without the operator switch it was a gate the agent could open on its own.
`run_macro` refuses them always.

`BED_MESH_CALIBRATE` `BEEP_OFF` `BEEP_ON` `DELTA_CALIBRATE` `DISABLE_ALL_SENSOR` `ENDSTOP_PHASE_CALIBRATE` `EXIT_ENFORCE_MODE` `FIRMWARE_RESTART` `FORCE_MOVE` `G28.6245197` `G29` `G32` `G34` `GCODE_ON_PRINT_STATUS` `HEATER_TEMP_LIMIT` `INIT_TMC` `M0` `M1002` `M104.6245197` `M109.6245197` `M140.6245197` `M18` `M190.6245197` `M23` `M24` `M24.6245197` `M25` `M25.6245197` `M301` `M303` `M304` `M32` `M4029` `M4031` `M502` `M601` `M603` `M604` `M84` `M900` `M901` `MANUAL_PROBE` `MCU_RESET` `MPC_CALIBRATE` `PAUSE` `PID_CALIBRATE` `PRINT_END` `PRINT_START_SET` `PROBE_ACCURACY` `PROBE_CALIBRATE` `QUAD_GANTRY_LEVEL` `RESET_HEATER_SCALING` `RESTART` `REVERSE_HOMING` `SAVE_CONFIG` `SAVE_CONFIG_QD` `SAVE_VARIABLE` `SCREWS_TILT_CALCULATE` `SDCARD_PRINT_FILE` `SET_AI_DETECT_RESULT` `SET_EXTRUDER_ROTATION_DISTANCE` `SET_EXTRUDER_STEP_DISTANCE` `SET_GCODE_VARIABLE` `SET_HEATER_SCALING` `SET_HEATER_TEMPERATURE` `SET_HOMING_MODE` `SET_HOMING_STATE` `SET_KINEMATIC_POSITION` `SET_OPERATING_CURRENT` `SET_PRESSURE_ADVANCE` `SET_STEPPER_ENABLE` `SET_TMC_CURRENT` `SET_TMC_FIELD` `SET_VELOCITY_LIMIT` `SHAPER_CALIBRATE` `STEPPER_BUZZ` `SYNC_EXTRUDER_MOTION` `TEMPERATURE_WAIT_SCALED` `TESTZ` `TEST_RESONANCES` `Z_OFFSET_APPLY_ENDSTOP` `Z_OFFSET_APPLY_PROBE` `Z_TILT_ADJUST` `_BED_MESH_CALIBRATE` `_CHECK_HEATER_PARAMS` `_GCODE_WAIT_WRAPPER` `_GCODE_WAIT_WRAPPER_INNER` `_KM_APPLY_PRINT_OFFSET` `_KM_CANCEL_PRINT_BASE` `_KM_CLEAR_PAUSE` `_KM_PAUSE_BASE` `_KM_PRINT_STATUS` `_KM_RESTORE_GCODE_STATE` `_KM_RESUME_BASE` `_KM_SAVE_GCODE_STATE` `_PARK_INNER` `_PRINT_START_BOX_PREPAR` `_PRINT_START_PHASE_CHECK` `_PRINT_START_PHASE_EXTRUDER` `_PRINT_START_PHASE_INIT` `_PRINT_START_PHASE_PREHEAT` `_PRINT_START_PHASE_PROBING`

**Never unlocked (9):** `GCODE_ON_PRINT_STATUS`, `HEATER_TEMP_LIMIT`, `RESET_HEATER_SCALING`, `SET_GCODE_VARIABLE`, `SET_HEATER_SCALING`, `_CHECK_HEATER_PARAMS`, `_GCODE_WAIT_WRAPPER`, `_GCODE_WAIT_WRAPPER_INNER`, `_KM_PRINT_STATUS`. The operator switch does not reach
these, because each defeats the value checks themselves: they rewrite the heater
scaling the temperature check reads, plant values in macro state that later macros
send unchecked, store a command to run later, or route one heater's target to
another's raw built-in.

Starting a print from the raw path (`SDCARD_PRINT_FILE`, `M23`, `M24`, `M32`) and
`SET_FILAMENT_SENSOR … ENABLE=0` are not in that list. The print starts are in the
list above and the sensor switch-off is refused the same way, so
`QIDI_ALLOW_DANGEROUS_GCODE=1` plus `allow_dangerous=true` (and the token) does
reopen them; `run_macro` never does. Without the switch, use `start_print`, which
claims the lease and binds the job to the checked file.

Note the vendor spellings. QIDI ships `SAVE_CONFIG_QD` — "save config without
restarting" — which rewrites `printer.cfg` exactly as the denied `SAVE_CONFIG`
does, minus the restart that would make it obvious. A deny-list that only knows
upstream's names is not a deny-list.

The dedicated tools are unaffected: `motors_off` still sends `M84` and
`bed_mesh_calibrate` still runs a calibration. The deny-list governs the *raw*
path, where those commands would arrive without interlocks, progress reporting or
a timeout.

## Read-only mode

With `QIDI_READ_ONLY=1`, refusal happens **at tool registration** — driven by the
tool's own annotations, before any network I/O. So a mutating tool that forgot its
own check is still refused, and no pointless HTTP call is made before saying no.
`emergency_stop` is the single exemption.

## A tier can be chosen from the arguments

Most tools sit on one tier. Ten do not: they pick a tier per call, so the *same*
tool can be waved through or held for a two-step token depending on what it was
asked to do. `set_temperature` is `low` when the target is 0 (turning a heater off
is safety-positive and never gated), `thermal` for an ordinary target, and
escalates to `destructive` once the target crosses the confirm threshold.
`run_gcode` spans `read`, `low` and `destructive` by what the line actually does.
`upload_gcode`, `move_file` and `copy_file` escalate when they would overwrite
something.

So a flat count of "tools that need a token" is a simplification in both
directions, and the numbers in these docs say *always*: 22 tools always require
one, while several others reach that tier only for a specific argument.

## The printer lease

Every agent runs its own copy of this server, so there is no shared process in which
a lock could live: an in-process mutex protects an agent from itself, and a file lock
protects one laptop. The one thing all agents can see is **the printer**, so the claim
lives there — in a namespace of Moonraker's own key-value database.

Holding it refuses another agent's mutating calls, by name, with the holder's stated
reason and time remaining. `start_print` and releasing the job queue require holding
it *explicitly*, even on an idle machine, so committing hours of machine time is never
anonymous.

| Never blocked | Why |
|---|---|
| All reads | An agent that cannot see the machine is more dangerous than one that can. |
| `emergency_stop`, `abort_heating`, `abort_macro` | An escape hatch another agent can lock is not an escape hatch. |
| Pausing the job queue | It only prevents a *future* automatic start — the safe direction. |

`cooldown` and `cancel_print` are deliberately **not** exempt: harmless to the machine,
ruinous to somebody else's print, which is the takeover being prevented.

> **Cooperative, not a lock.** It binds agents running this server. It does **not** bind the printer's own
> touchscreen, Fluidd, an older build, or anyone calling `break_printer_lease`.
> Moonraker offers no compare-and-set, so acquisition writes and then reads back to
> catch a simultaneous claim; that closes the window that actually occurs without
> claiming a guarantee the transport cannot give. Calling it a lock would invite an
> agent to skip looking at the machine, which is the check that always works.
>
> A missing `database` component **fails open**: coordination reports itself
> unavailable and every other gate still applies, because an absent convenience
> should not become an outage.

Leases expire (default 60 min, renewable) and can be broken, so a holder who goes home
cannot strand the printer — and every break is audited with who broke whose. See
[sharing the printer](../guides/sharing-the-printer.md).

## What adversarial review found

Independent agents were told to break this and to prove every claim by running
code. **Twelve real bugs** came out of it, each fixed with a regression test that
fails when the fix is reverted (going live then found four more — see [known
limitations](#known-limitations) below, and note that they are a different class).
Full findings, evidence and reproducible proof scripts:
[the security review](../security/README.md).

The recurring theme is the useful part: **each bug was a place where the code
guessed instead of knowing.** It guessed how Klipper parses a line, guessed that
cached state was still true, guessed what a 503 meant, guessed that undiscovered
hardware was absent, guessed that a filename identified a file. Each fix replaces
the guess with a real query, or with an explicit unknown that fails safe:

- `job_active` treats an *unknown* print state as possibly printing.
- `cooldown` does not consult capability discovery at all.
- Turning hardware **off** is never refused on capability grounds.
- `require_finite()` runs before any comparison NaN could defeat — `abs(nan) > cap` is `False`, so the obvious spelling *approves* NaN.
- Anything that cannot be parsed well enough to check is refused, not sent.

## Known limitations

> **The proofs are mock proofs.** Every test and safety proof runs against an in-process fake Moonraker, so they
> demonstrate that the *logic* is right, not that a given firmware behaves as the
> mock does. Going live found **four bugs of exactly this class**, all now fixed
> and regression-tested:
>
> | Found live | Why the mock could not catch it |
> |---|---|
> | `httpx` honoured `http_proxy` by default, so printer control requests would have gone to an external host | The mock is reached over loopback, which no proxy config touches |
> | Soft abort reported absent on a machine that has it — discovery searched Klipper's endpoint list for routes that live on Moonraker | Both namespaces are the same object in the mock |
> | `.gcode.3mf`, the printer's own native format, rejected as "not a G-code file" | No test uploaded the format the slicer actually produces |
> | `SET_AI_DETECT_RESULT` screened as harmless, though it can forge a spaghetti verdict or suppress a real one | The mock had no AI-detection module to forge a verdict *to* |
>
> The proxy one is worth internalising: it was not a printer bug at all, but the
> dev machine's environment quietly redirecting hardware control. Set
> `trust_env=False` on any client that talks to lab hardware.
>
> Expect more of this class on a machine that differs from the lab's.

- **The audit log rotates at 5 MB**, keeping three generations
  (`QIDI_AUDIT_MAX_BYTES`, `QIDI_AUDIT_KEEP`). Rotation happens before the append,
  so the entry that trips the limit lands in the fresh file, and a rotation failure
  is swallowed — a problem writing the *record* of an action must never break the
  action.
- **Moonraker itself is unauthenticated on the LAN.** This server constrains *this client*; it is not a boundary in front of the printer.
- **Severity grading assumes `run_gcode` keeps its two-step ticket and `QIDI_ALLOW_DANGEROUS_GCODE` stays off.** If either default changes, the deny-list findings need re-grading.
- **Macros are screened by name and parameters, not expanded.** Their bodies are Jinja templates Klipper evaluates. `docs/security/proofs/L5_denylist.py` follows every parameter of the stock Max 4 macros (through assignments, forwarded parameters and macro state written by one macro and read by another) to the heater, limit, motion and offset primitives it reaches, and reports none uncovered: each is value-checked or, where it carries a command or rewrites the heater scaling, refused even with the operator switch (none rests on the deny-list alone). It also confirms that every parameter name a forwarding loop re-renders is validated. That is a static analysis of the vendored stock config only; a user-defined macro is screened by its name and nothing else.
- **Not range-checked at all:** `G0`–`G3` coordinates after a `G91` (until the next `G90`; a script is assumed to start in absolute mode), arc centres (`I`/`J`), `G92` re-basing the coordinate frame, repeated `Z_ADJUST` steps adding up past ±5 mm, extrusion lengths inside macros, and values macros take from their own variables (`_km_globals`, set in config; `SET_GCODE_VARIABLE` is refused even with the operator switch). For these the firmware's own limits (`position_min`/`max`, `max_extrude_only_distance`, `max_temp`) are the backstop.
