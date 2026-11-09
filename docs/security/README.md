# Security review

This directory holds the adversarial review of the safety layer, the evidence
behind it, and the record of what was fixed.

It is here because the safety argument for this server is not "we were careful."
It is "we tried to break it, wrote down how, and the attempts are reproducible."
A reader who does not trust the code should be able to re-run the proofs.

## Why this server needs a safety layer at all

The Max 4 runs stock Moonraker with `trusted_clients` covering `172.16.0.0/12`
and no `force_logins`. Any host on the LAN can already command the printer with
no credential. Nothing in this repo makes that better or worse — but it does mean
**this server's safety envelope is the only thing constraining the agent that
uses it**, not a second line of defence behind an authentication boundary.

That is why the guards are enforced numerically, in code, before a byte reaches
Moonraker — and why they are reviewed the way they are here.

## The reviews

| Document | Scope |
| --- | --- |
| [`audit-policy.md`](audit-policy.md) | Confirmation tokens: forgery, replay, canonicalisation, TOCTOU, risk tiers, audit log |
| [`audit-limits.md`](audit-limits.md) | The numeric envelope: clamping, temperature/motion bounds, G-code parsing, deny-list completeness |
| [`audit-interlocks.md`](audit-interlocks.md) | State freshness, interlock coverage, degraded-state handling, capability inference, concurrency |

Each finding carries the file:line it applies to, why it matters *for the
hardware*, an executed proof, and a suggested fix. Findings the reviewers tried
to break and could not are recorded as `VERIFIED-SOUND`, because knowing which
guards hold is as useful as knowing which do not.

[`proofs/`](proofs/README.md) contains the scripts. See [`proofs/README.md`](proofs/README.md).

## Findings and their resolution

Nine defects were proven and fixed. Every fix has a regression test that fails
when the fix is reverted — verified by actually reverting each one. A later
review, during the open-source export, found five more ways past the same
gates; they are listed [below](#envelope-bypasses-found-during-the-oss-export-review),
followed by [what a second review found left over](#second-review-bypasses-left-after-the-first-fix).

| Severity | Finding | Fixed in |
| --- | --- | --- |
| HIGH | **G-code deny-list bypass via line numbers.** `N1 SAVE_CONFIG` was screened as command `N1`, because the screen split on whitespace while Klipper *skips* a leading `N<number>`. 7 of 15 probe lines bypassed. | `limits.py` — `klipper_command_name()`, a faithful port of upstream's extraction |
| HIGH | **The envelope was enforced only by the dedicated tools.** `run_gcode` screened command names but never argument values, so `M104 S374` reached the printer from the same server that refused `set_temperature(374)`. | `tools/system.py` — parameters parsed as Klipper does, then run through `check_temperature`/`check_position` |
| HIGH | **Three guards were fail-open on NaN.** Written `if abs(x) > cap`; every comparison against NaN is False, so NaN was *approved*. `min(nan, cap)` returns NaN, so the clamp handed the value back and `G1 Enan F300` reached the wire with `ok: true`. | `policy.py` — one `require_finite()` ahead of every numeric guard |
| HIGH | **The deny-list only knew upstream's spellings.** QIDI ships `SAVE_CONFIG_QD` (save config *without* restarting) and `RESET_HEATER_SCALING` (re-widens the firmware's own 370 °C cap to the 375 °C MCU trip). 20 vendor macros reached restricted primitives undenied. | `limits.py` — vendor spellings added; renamed to `DANGEROUS_GCODE_COMMANDS` |
| HIGH | **Interlocks were judged on a 0.75 s cache.** A refusal primed the cache without invalidating it, so the agent's retry was authorised against pre-refusal state — injecting `G28`/`M84`/`M104` into a print that had just started. | `runtime.py` — `authorize()` always calls `state(fresh=True)` |
| HIGH | **Any 503 was read as "nothing is printing."** Moonraker returns the same 503 when *it* restarts while the print continues, so reboot/shutdown/firmware-restart passed mid-print. | `runtime.py` — `_degraded_state()` consults `/server/info`; otherwise print state is `unknown`, which `job_active` treats as active |
| HIGH | **A failed capability discovery was cached 300 s as "hardware absent."** `cooldown` then skipped `M141 S0` and `set_temperature(chamber, 0)` was *refused*, leaving a ~300 W heater on. | `capabilities.py` — `degraded` flag, 5 s TTL; `cooldown` no longer consults discovery; turning a heater **off** is never refused |
| HIGH | **`start_print`'s token bound only the filename.** Unconfirmed overwrites let the bytes change inside the confirmation window: approve one job, print another — also bypassing the G-code screen, since file contents are never screened. | `tools/jobs.py` — token binds size + mtime; overwrites are `DESTRUCTIVE` |
| HIGH | **A negative `QIDI_MAX_*_TEMP` inverted the range** (`min=0, max=-1`), which contains no value — so one mistyped env var produced a server that could not turn a hot heater **off**. | `limits.py` + `config.py` — refused at load; clamping can degenerate to "off only", never to empty |
| MEDIUM | **`run_gcode`/`run_macro` had no during-print interlock**, making every other interlock advisory — a refused move could just be sent as raw G-code. | `tools/system.py` — `forbid_during_print=True` |

## Envelope bypasses found during the OSS export review

An independent review drove the mock printer through each of these **with the
two-step token redeemed**, and every one reached the wire. Regression tests live in
`tests/test_envelope_bypasses.py`; each fails against the code before the fix.

| Severity | Finding | Fixed in |
| --- | --- | --- |
| HIGH | **`run_macro` screened only the macro name.** `run_macro("M104", {"S": 374})` sent `M104 S=374`; the same applied to every heater, motion or vendor macro parameter. | `tools/system.py` — one `_screen_script()` used by both `run_gcode` and `run_macro`, so the assembled line gets the deny-list, temperature, kinematic and axis checks |
| HIGH | **QIDI's scaled heater setter was not a heater command.** `SET_HEATER_TEMPERATURE_SCALED HEATER=extruder TARGET=374` was unchecked. Separately, extended commands were parsed by the letter-splitter, so a `HEATER=heater_bed` name was lost and every `SET_HEATER_TEMPERATURE*` line was checked against the *extruder's* ceiling; `M109`/`M190`/`M191 R<temp>` (QIDI's wait wrapper falls back to `R`) and `_GCODE_WAIT_WRAPPER S<temp>` were unchecked too. | `limits.py` — `HEATER_COMMANDS` lists every temperature parameter, including `PRINT_START HOTEND/BED/CHAMBER` and `CLEAR_NOZZLE HOTEND`; `command_params()` parses extended commands as Klipper's `_get_extended_params` does. `tools/system.py` also reads the live `set_heater_scaling.scaled_heaters` and checks the scaled target when scaling is not stock |
| HIGH | **Kinematic limits were unchecked.** `M203 X99999 Y99999` raised max velocity a hundredfold; `M201`, `M204` and `M205` likewise. Klipper does not cap these at the `printer.cfg` values. | `limits.py` — `LIMIT_COMMANDS`; `policy.py` — `check_kinematic_limit()` against `QIDI_MAX_VELOCITY` / `QIDI_MAX_ACCEL` / `QIDI_MAX_SQUARE_CORNER_VELOCITY`, defaulting to the `printer.cfg` values (800 mm/s, 30000 mm/s², 8 mm/s) and clamped to them |
| HIGH | **`allow_dangerous` was agent-controlled.** `SAVE_CONFIG` with `allow_dangerous=true` was sent once the token was redeemed — but the flag is an argument the agent supplies, behind a token the agent redeems, so no operator was ever in the loop. | `config.py` — `QIDI_ALLOW_DANGEROUS_GCODE` (default `0`). Without it deny-listed commands are refused before a token is issued, whatever the agent passes |
| MEDIUM | **Fifteen stock vendor macros reached restricted primitives undenied** (`proofs/L5_denylist.py`, section b), e.g. `M603`/`M604` (`DISABLE_ALL_SENSOR` and an unchecked `M104 S<param>`), `BEEP_ON` (`SAVE_VARIABLE`), `M203` (`SET_VELOCITY_LIMIT`). Separately, `SET_GCODE_VARIABLE` could rewrite the heater scaling QIDI's macros apply. | `limits.py` — eleven denied by name, four value-checked instead (table below); `SET_GCODE_VARIABLE` and `DISABLE_ALL_SENSOR` denied. L5 now reports 0 uncovered |

How each of the fifteen is handled:

| Macro | Reaches | Handling |
| --- | --- | --- |
| `M201` | `SET_VELOCITY_LIMIT` (via the accel cap `M204` honours) | value-checked: `X`/`Y` ≤ accel ceiling |
| `M203` | `SET_VELOCITY_LIMIT VELOCITY=` | value-checked: `X`/`Y` ≤ velocity ceiling |
| `M205` | `SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=` | value-checked: `X`/`Y` ≤ square-corner-velocity ceiling |
| `SET_HEATER_TEMPERATURE_SCALED` | `SET_HEATER_TEMPERATURE` with QIDI's scaling | value-checked: `TARGET` and the live-scaled target against the named heater's ceiling |
| `BEEP_ON`, `BEEP_OFF` | `SAVE_VARIABLE` | denied (a persistent setting with no dedicated tool; the `beep` tool only sounds the beeper) |
| `M603`, `M604` | `DISABLE_ALL_SENSOR`, `M104 S<param>` | denied (use `unload_filament` / `load_filament`) |
| `PAUSE` | `DISABLE_ALL_SENSOR`, `SET_STEPPER_ENABLE` | denied (use `pause_print`) |
| `PRINT_END` | `DISABLE_ALL_SENSOR` | denied (part of the print sequence) |
| `TEMPERATURE_WAIT_SCALED`, `_CHECK_HEATER_PARAMS` | heater scaling | denied (internal to QIDI's heater macros) |
| `_KM_APPLY_PRINT_OFFSET`, `_PRINT_START_BOX_PREPAR` | `SAVE_VARIABLE` | denied (internal print-start phases) |
| `_PRINT_START_PHASE_INIT` | `DISABLE_ALL_SENSOR` | denied (internal print-start phase) |

The dedicated tools are unaffected: `load_filament`, `unload_filament` and
`pause_print` send their macros directly, with their own checks and interlocks.

## Second review: bypasses left after the first fix

A second independent review used the same method (token redeemed,
`QIDI_ALLOW_DANGEROUS_GCODE` unset, against `tests/mock_moonraker.py`) on the
fixed code. Each case below reached the wire; each is a regression test in
`tests/test_envelope_bypasses.py` (sections 7–9) that fails against the first fix.

| Severity | Finding | Fixed in |
| --- | --- | --- |
| HIGH | **Renamed built-ins.** QIDI's macros keep the originals reachable with `rename_existing`: `M104.6245197 S374`, `M109.6245197 S374`, `M140.6245197 S124`, `M190.6245197 S124` were sent unchecked (past even QIDI's own 370 °C clamp, which lives in the wrapper), and `_BED_MESH_CALIBRATE` / `_KM_PAUSE_BASE` were accepted though `BED_MESH_CALIBRATE` / `PAUSE` are denied. The value tables were looked up by exact name; only the deny-list compared the stem before the `.`. | `limits.py` — `STOCK_RENAMED_BUILTINS` denied, `table_key()` checks a decimal code as its stem, unknown decimal G/M codes refused; `capabilities.py` adds every `rename_existing` in the printer's own config |
| HIGH | **Line separators.** `str.splitlines()` also breaks on `\r`, `\x0b`, `\x0c`, `\x1c`–`\x1e`, `\x85`, `\u2028`, `\u2029`; Klipper splits on `\n` only and keeps the *last* value of a repeated parameter. `M104 S100\rS374`, `M140 S60\x0cS200`, `M203 X100\u2028X99999` and `G1 X10\x1cX9999` were each screened as two harmless lines and run as one with the larger value. | `limits.py` — `split_gcode_script()` splits as Klipper does and refuses other control characters; `run_macro` values likewise. [`audit-limits.md`](audit-limits.md) corrected |
| HIGH | **Stock-macro temperature parameters not covered.** `M600 R374` (→ `M109 S{params.R}`), `EXTRUSION_AND_FLUSH HOTEND=374` (→ `M109`), and `PRINT_START_SET HOTEND=374` followed by `_PRINT_START_PHASE_EXTRUDER` (which reads the stored value back into `M109`). L5 section (b) only looked for restricted command *names* and missed all three. | `limits.py` — `M600 R`, `EXTRUSION_AND_FLUSH HOTEND`, `BOX_PRINT_START HOTENDTEMP` value-checked; `PRINT_START_SET` and `_PRINT_START_PHASE_{PREHEAT,PROBING,EXTRUDER,CHECK}` denied; L5 section (e) |
| HIGH | **Parameter forwarding into a dynamic command** (found by the new L5 section (e)). QIDI's `M109`/`M190` forward every parameter to `_GCODE_WAIT_WRAPPER_INNER`, which sends `M{params.M}.6245197 S{target}`. Because Klipper's own parser keeps a repeated letter's last value, `M109 S200 R1 M140` set the extruder to a checked 200 °C and then sent `M140.6245197 S200` to the **bed**. | `tools/system.py` — `M109`/`M190` accept only `S`/`R`/`T` (and `M` only as their own number); both wrapper internals denied |
| MEDIUM | **Deferred and laundered commands** (L5 section (e)). `GCODE_ON_PRINT_STATUS ... COMMAND=SAVE_CONFIG` stores a command and runs it later, unscreened; `PARK`/`G27`/`M600` `X`/`Y`/`Z` reach `G0` bounded only by the firmware's travel; `M290 Z` and `SET_GCODE_OFFSET` were unbounded; `M0`/`M25`/`M601` are `PAUSE`, `M1002 S1` writes `z_offset` with `SAVE_VARIABLE`, `M900` is `SET_PRESSURE_ADVANCE`; QIDI's closed-loop stepper commands (`SET_OPERATING_CURRENT`, `SET_HOMING_STATE`, …) set motor current. | `limits.py` — denied, or value-checked (`MOTION_MACROS`, `OFFSET_COMMANDS` ±5 mm) |
| LOW | **A failed heater-scaling read was treated as stock.** | `tools/system.py` — refused instead; "stock" is assumed only when the printer answers and has no scaling macro |

L5 section (e) is the systematic part: it follows every parameter of every stock
macro through Jinja assignments, aliases, forwarded parameters and macro state to
the heater, limit, motion and offset primitives, and reports **78 flows, 0
uncovered**. Section (f) checks every `rename_existing` target is denied.

## The pattern

Each bug was a place where the code **guessed instead of knowing**: how Klipper
parses a line, whether cached state still held, what a 503 meant, whether
undiscovered hardware was absent, whether a filename identified a file.

So the fixes share a shape — replace the guess with a real query, or with an
explicit *unknown that fails safe*:

- `job_active` treats an unknown print state as **possibly printing**.
- `cooldown` does not consult capability discovery at all.
- Turning hardware **off** is never refused on capability grounds.
- `require_finite()` runs *before* any comparison that NaN could defeat.
- Anything that cannot be parsed well enough to check is **refused**, not sent.

Two design rules came out of this and are worth keeping:

1. **A screen that parses its input differently from the executor is not a
   screen.** All G-code classification goes through `klipper_command_name()`.
   Never re-split the text.
2. **A guarantee that holds only on the paths you remembered to guard is not a
   guarantee.** The raw escape hatch applies the envelope to every heater, limit,
   motion and offset command it can parse, and to the stock macro parameters L5
   section (e) traces to one. The limitations below are what it does not cover.

## What the reviews confirmed sound

Worth recording, since it bounds what is still unverified:

- **The parser matches upstream.** One reviewer drove Klipper's real
  `GCodeDispatch` as an oracle over **154,371 generated spellings** — extra
  whitespace, tabs, missing separators, comments, mixed case, non-ASCII that
  changes under `.upper()`, the `*` checksum form — with **zero divergences** in
  the resolved command name.
- **No env var can raise a hardware ceiling**; `inf`/`nan`/`1e400` are refused at load.
- **Axis bounds match `printer.cfg` exactly**, inclusive, with no sign error at
  the negative minimums.
- **NaN/inf temperature and position targets fail closed** via `contains()`.

## Reviewer accuracy

One reported finding was **overstated**, and it matters that this is written down.

`audit-limits.md` reports `G1 Enan` executing with NaN in the kinematics. Checked
against the real parser: Klipper's `args_r` regex splits `Enan` into the single
token `ENAN`, so the `E` parameter has *no value* and the move is dropped. The
guard was still fail-open and still wrong — but the hazard was a malformed move,
not NaN reaching the motion planner.

## Third review: the operator switch, Box temperatures and parameter names

A re-review of #48 (token redeemed, against `tests/mock_moonraker.py`, with
`QIDI_ALLOW_DANGEROUS_GCODE` both unset and set) found the cases below. Each is a
regression test in `tests/test_envelope_bypasses.py` (section 10) that fails on
`124349b` and passes now.

| Severity | Finding | Fixed in |
| --- | --- | --- |
| HIGH | **Padded parameter names.** `PRINT_START HOTEND=200 " HOTEND"=374 BED=60` (also a tab, or `" BED"`) was accepted. Klipper keeps the quoted key as `" HOTEND"`, which no table names; `_print_start_phase_init` re-renders every parameter as `%s="%s"`, escaping values but not keys, so it became `HOTEND="374"`, the last `HOTEND` won, and `_PRINT_START_PHASE_EXTRUDER` sent `M109 S374`. | `limits.py` — `klipper_extended_params` refuses any name that is not `[A-Z0-9_]+`, and the screen refuses every extended-command line it cannot read that way; `run_macro` refuses padded or repeated names. L5 section (e2) lists the 9 forwarding loops and probes the validation. |
| HIGH | **`E_LOAD SLOT=0 S=374` reached the wire** (also `SLOT=16`, the form `M604` uses). | `limits.py` — `E_LOAD S` is in `HEATER_COMMANDS`. Other Box firmware commands (`MULTI_COLOR_*`, `EXTRUDER_LOAD`, `E_UNLOAD`, …) have unpublished parameters, so any whose name could hold a temperature is held to the extruder envelope on the load/unload family and refused on the rest (`BOX_FIRMWARE_COMMANDS`). |
| HIGH | **The operator switch reopened planted temperatures.** With `QIDI_ALLOW_DANGEROUS_GCODE=1`, `PRINT_START_SET HOTEND=374` then `_PRINT_START_PHASE_EXTRUDER` was protected only by the deny-list, as were `M4029 S`, `M603`/`M604 S`, `_PRINT_START_PHASE_INIT`, `PAUSE`/`_PARK_INNER` X/Y/Z, and `SET_GCODE_VARIABLE MACRO=print_start_set …`. | `limits.py` — those parameters are value-checked whatever the switch says; the commands that defeat the value checks (`SET_GCODE_VARIABLE`, the heater-scaling macros, the deferred-command macros, the wait-wrapper internals) are in `ALWAYS_DENIED_GCODE_COMMANDS`, which the switch cannot unlock. L5 now reports **0 flows covered by the deny-list alone**. |
| MEDIUM | **`SET_FILAMENT_SENSOR … ENABLE=0` was accepted** although `DISABLE_ALL_SENSOR`, which wraps it, is denied. | `limits.py` — `VALUE_SENSITIVE_COMMANDS`: `ENABLE` other than 1 is denied. |
| MEDIUM | **Raw print starts.** `SDCARD_PRINT_FILE`, `M23`+`M24` and `M32` started a print without `start_print`'s lease claim and file binding. | `limits.py` — deny-listed; the refusal points to `start_print` / `resume_print`. |
| LOW | **`PARK`/`G27`/`M600` lost the Z floor after a `G91`**: any `G91` in a script switched the axis envelope off for every line. | `tools/system.py` — the mode is tracked line by line, and `PARK`/`G27`/`M600`/`PAUSE` are always checked, since they switch to `G90` themselves. |

The fix stands; the severity rationale was corrected. An overstated finding that
goes unchallenged becomes folklore, and folklore is not a safety argument.

## Follow-up: classic values Klipper keeps verbatim

Found after #49. Each case is a regression test in `tests/test_envelope_bypasses.py`
(section 11) that fails on `9a50176` and passes now; a parity test runs the vendored
Klipper `_process_commands` and compares its command and parameters with the screen's.

| Severity | Finding | Fixed in |
| --- | --- | --- |
| HIGH | **`=`-prefixed classic values.** `G27 P1 Z=20` (and `M600 Z=20`, `M600 X=100 Y=100 Z=20`) passed a raised `QIDI_MIN_JOG_Z` floor. The screen stripped the `=` and checked Z=20; Klipper keeps the value `=20`, `G27`/`M600` forward it to `PARK` as `Z="=20"`, and `\|float` reads it as 0.0, so the toolhead went to Z=0 (X/Y collapse the same way). | `limits.py` — `klipper_params` returns values exactly as Klipper keeps them, and `classic_value_problem` refuses any classic value with `=`, a quote or a space (`M117`/`M118` exempt). |
| HIGH | **Empty and quoted values.** `G27 P1 Z`, `M600 Z` and `PARK Z=` were skipped as "no value", but the macros read `""` as 0.0; `G27 P1 Z"20"` was checked as 20 because the screen stripped quotes `\|float` does not. | `tools/system.py` — an empty value on a checked parameter is refused, and values are no longer unquoted before the check. |
| MEDIUM | **`run_macro` wrote classic values as `K=V`.** `run_macro("M600", {"Z": 20})` sent `M600 Z=20`, which parks at Z=0 (and `M104 S=220` set 0). | `tools/system.py` — classic commands are written `M600 Z20`, with letter-only names and plain-number values. |

## Known limitations

- **The proofs and regression tests run against the in-process mock**
  (`tests/mock_moonraker.py`), not the printer. They show the screening logic is
  right for the commands as Klipper parses them; the macro analysis reads QIDI's
  *published* config in `https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900/config/`, so a printer whose config differs is
  covered only by the runtime `rename_existing` read and the name screen.
  Live findings so far are listed in the
  [safety model](../reference/safety-model.md).
- `audit-limits.md` grades severity assuming the raw G-code path keeps its
  two-step ticket and `QIDI_ALLOW_DANGEROUS_GCODE` stays off. If either default
  changes, re-grade the deny-list findings. With the switch **on**, deny-listed
  commands are again gated only by the agent's own `allow_dangerous=true` plus a
  token the agent redeems — turn it on only for supervised maintenance. The value
  checks still hold with it on, and the commands in `ALWAYS_DENIED_GCODE_COMMANDS`
  stay refused.
- **Macros are screened, not expanded.** Their bodies are Jinja templates Klipper
  evaluates at run time. The server checks a macro by its name and by the
  parameters listed in `HEATER_COMMANDS` / `LIMIT_COMMANDS` / `MOTION_MACROS` /
  `OFFSET_COMMANDS`; L5 section (e) shows that list covers every parameter of the
  **stock Max 4 config** in `https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/` that reaches a primitive.
    - A **user-defined or modified macro** is screened only by name. A new macro
      that wraps `SAVE_CONFIG`, or passes a temperature in a parameter not listed,
      is not caught.
    - **Transitive reach to non-value primitives** is not screened: `CANCEL_PRINT`
      reaches `DISABLE_ALL_SENSOR` via `PRINT_END`, `RESUME` re-heats to the saved
      target via `RESUME_PRINT`. They act only on a paused or running print, which
      the raw path's `forbid_during_print` interlock already refuses.
- **Not range-checked at all:** `G0`–`G3` coordinates after a `G91` (until the
  next `G90`; a script is assumed to start in absolute mode, so a previous
  script's `G91` makes the check stricter, not looser), Box parameters whose names
  do not look like a temperature, arc centres
  (`I`/`J`), `G92` re-basing the coordinate frame, repeated `Z_ADJUST` steps adding
  up past ±5 mm, extrusion lengths inside macros, and values macros read from
  their own config variables (`_km_globals`). The firmware's `position_min`/`max`,
  `max_extrude_only_distance` and `max_temp` are the backstop there.
- **Heater scaling changed outside this server.** If the scaling read fails the
  screen refuses; if `gcode_macro set_heater_scaling` is absent it assumes stock
  (`scale=1, bump=0, minimum=0`). Every command that could change it is
  deny-listed here, but the touchscreen, Fluidd or a G-code file can still change
  it, and the screen reads it once per call.
- **Print files are not screened.** `start_print` binds the file's size and mtime,
  but its G-code runs without the line screen; the envelope governs this server's
  raw path, not what a slicer wrote.
- Audit-log rotation **is** implemented (`AuditLog._rotate_if_needed`, size and
  generation count set by `QIDI_AUDIT_MAX_BYTES`/`QIDI_AUDIT_KEEP`); an earlier
  draft of this page said otherwise. See [the safety model](../reference/safety-model.md#known-limitations).
