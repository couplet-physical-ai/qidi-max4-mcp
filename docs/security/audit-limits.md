# Audit: qidi_mcp/limits.py + qidi_mcp/config.py

Ground truth: [QIDI's pinned `printer.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/printer.cfg) (extruder max_temp 375, bed 125, chamber 68,
min_extrude_temp 135; X -2..392, Y -5..410, Z -5..342; max_velocity 800),
[QIDI's pinned `heaters.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/heaters.cfg) (heater_temp_limit 370/120/65),
[Klipper's pinned parser](https://github.com/Klipper3d/klipper/blob/61c0c8d2ef40340781835dd53fb04cc7a454e37a/klippy/gcode.py) (optional fetch via `bash scripts/fetch_upstream_sources.sh`; original proof revision unknown).
Findings appended incrementally as confirmed.


---

## VERIFIED-SOUND clamp_envelope(): no env var can raise a heater ceiling above the hardware max
`qidi_mcp/limits.py:168-181` | `hi = min(h.max, hw_max)` … `max_move_feedrate=max(1.0, min(env.max_move_feedrate, hw.max_velocity * 60.0))`

Why it matters for the hardware: this is the load-bearing guarantee in the module docstring
("Nothing here can *raise* a limit past the hardware maximum"). If an env var could push the
ceiling past `printer.cfg`'s `max_temp`, the MCP would happily ask for a target the firmware
treats as a fault condition.

Evidence (script + real output): `docs/security/proofs/L1_clamp.py` — tried huge, exactly-at-ceiling,
epsilon-over, hex, underscored, whitespace-padded, inf/-inf/NaN, non-numeric and empty values.

```
  '1000'           -> min=0.0    max=375.0    confirm_above=260.0     <== above vendor macro cap 370
  '375'            -> min=0.0    max=375.0    confirm_above=260.0     <== above vendor macro cap 370
  '374.9'          -> min=0.0    max=374.9    confirm_above=260.0     <== above vendor macro cap 370
  '1e9'            -> min=0.0    max=375.0    confirm_above=260.0     <== above vendor macro cap 370
  '300.0000001'    -> min=0.0    max=300.0000001 confirm_above=260.0
  '0x1'            -> REFUSED: QIDI_MAX_EXTRUDER_TEMP must be a number, got '0x1'
  'inf'            -> REFUSED: QIDI_MAX_EXTRUDER_TEMP must be a finite number, got 'inf'.
  'nan'            -> REFUSED: QIDI_MAX_EXTRUDER_TEMP must be a finite number, got 'nan'.
  '1e400'          -> REFUSED: QIDI_MAX_EXTRUDER_TEMP must be a finite number, got '1e400'.
  QIDI_MAX_BED_TEMP=9999   -> max=125.0
  QIDI_MAX_CHAMBER_TEMP=9999   -> max=68.0
  QIDI_MAX_FEEDRATE         =1e9      -> feedrate=48000.0     (hardware max_velocity*60 = 48000)
```

No input raised a ceiling above hardware (375/125/68), and `inf`/`nan`/`1e400` are refused at
load time by `_env_float`'s `math.isfinite` check rather than poisoning a comparison later.
`float("1e400")` returning `inf` is caught by the same check — a nice detail that is easy to miss.
The feedrate clamp against `max_velocity * 60` also holds. Guard holds; I could not get past it.

Suggested fix: none needed for the hardware-ceiling property. But see the next two findings —
"cannot exceed hardware" is not the same as "safe", and the clamp has a fail-open direction it
does not cover.

---

## MEDIUM Ceiling clamps to printer.cfg max_temp, which is the MCU *shutdown* threshold, not a safe target
`qidi_mcp/limits.py:100-102` | `extruder_max_temp=375.0,  # [extruder] max_temp` / `bed_max_temp=125.0` / `chamber_max_temp=68.0`
also `qidi_mcp/limits.py:169` | `hi = min(h.max, hw_max)`

Why it matters for the hardware: `max_temp` in Klipper is not "the hottest you may ask for" — it
is the threshold outside which the microcontroller *shuts down*. The
[Klipper Config Reference](https://github.com/Klipper3d/klipper/blob/61c0c8d2ef40340781835dd53fb04cc7a454e37a/docs/Config_Reference.md)
describes this safety behavior. Fetch its full GPLv3 source with
`bash scripts/fetch_upstream_sources.sh` to check the exact wording.

So `QIDI_MAX_EXTRUDER_TEMP=375` is clamped to exactly 375 and accepted. A PID target of 375 with
any overshoot at all trips an MCU shutdown mid-print — the printer stops hard, the heaters drop,
and the job is lost. This is exactly why QIDI's own `heater_temp_limit`
([QIDI's `heaters.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/heaters.cfg)) caps at 370 / 120 / 65: 5 °C of headroom under the
shutdown trip on the extruder and bed, 3 °C on the chamber. Our clamp uses the trip point itself
as the ceiling, so the MCP's envelope is *wider than the vendor's own* at the top end.

Evidence (script + real output): `docs/security/proofs/L1_clamp.py`

```
hardware ceilings: extruder=375 bed=125 chamber=68
vendor macro caps : extruder=370 bed=120 chamber=65  (heaters.cfg)
  '375'            -> min=0.0    max=375.0    confirm_above=260.0     <== above vendor macro cap 370
  QIDI_MAX_BED_TEMP=125.0  -> max=125.0   <== above vendor cap 120
  QIDI_MAX_CHAMBER_TEMP=68.0   -> max=68.0    <== above vendor cap 65
```

The defaults (300/120/65) are only safe because they happen to sit under the trip; the *clamp*
permits 375/125/68 whenever the env vars are set. Note the shipped bed default (120.0) already
equals the vendor cap, so the bed has zero margin by default and the only thing keeping it off
the 125 trip is that nobody set `QIDI_MAX_BED_TEMP`.

Suggested fix: add explicit vendor-cap fields to `HardwareProfile` (e.g.
`extruder_safe_max=370.0, bed_safe_max=120.0, chamber_safe_max=65.0`, transcribed from
`heaters.cfg`) and clamp against those in `clamp_envelope`, keeping `*_max_temp` only for
documenting the trip point. That makes the MCP's ceiling equal to the firmware's own, not to the
fault threshold.

---

## HIGH A negative QIDI_MAX_*_TEMP makes turning the heater OFF impossible
`qidi_mcp/limits.py:168-171` | ```def fix(h: HeaterLimits, hw_max: float) -> HeaterLimits:
    hi = min(h.max, hw_max)
    lo = max(0.0, min(h.min, hi))
    return HeaterLimits(min=lo, max=hi, confirm_above=min(h.confirm_above, hi))```

Why it matters for the hardware: `clamp_envelope` clamps the *top* against hardware but never
re-establishes `min <= max`. With `QIDI_MAX_CHAMBER_TEMP=-1` the envelope becomes
`min=0.0, max=-1.0`. `HeaterLimits.contains()` is `self.min <= value <= self.max`, which is now
false for *every* real number — including `target=0`. `set_temperature(heater='chamber',
target=0)` is the one action that must never be refused: it is how the agent turns a ~300 W
chamber heater back off. A single mis-typed env var (a stray `-`) therefore produces a server
that can be pointed at a hot printer and cannot cool it down, which is the precise failure the
brief calls out ("leaving a chamber heater on").

Note `lo = max(0.0, min(h.min, hi))` looks like it guards this, but `h.min` is hard-coded to
`0.0` by `config.py:213/220/226`, so `min(0.0, -1.0) = -1.0`, then `max(0.0, -1.0) = 0.0`. The
`max(0.0, ...)` pins the floor at 0 and the inversion survives.

Evidence (script + real output):

```
$ .venv/bin/python -c "...p.check_temperature(heater, target)..."
QIDI_MAX_CHAMBER_TEMP=-1   heater=chamber  min=0.0   max=-1.0   target=0.0   -> REFUSED: 0.0 °C is outside the allowed range for chamber (0–-1 °C).
QIDI_MAX_CHAMBER_TEMP=-1   heater=chamber  min=0.0   max=-1.0   target=50.0  -> REFUSED: 50.0 °C is outside the allowed range for chamber (0–-1 °C).
QIDI_MAX_CHAMBER_TEMP=0    heater=chamber  min=0.0   max=0.0    target=0.0   -> ALLOWED
QIDI_MAX_EXTRUDER_TEMP=-1   heater=extruder min=0.0   max=-1.0   target=0.0   -> REFUSED: 0.0 °C is outside the allowed range for extruder (0–-1 °C).
```

`=0` is fine (only "off" is permitted — fail-closed and coherent). `=-1` is incoherent: the
range is empty. The error message "(0–-1 °C)" is itself the tell.

Suggested fix: reject a negative ceiling at load time — give `_env_float("MAX_*_TEMP", ...)` a
`minimum=0.0` — and make `clamp_envelope` defensive regardless, e.g. `hi = max(0.0, min(h.max,
hw_max))` so the range can degenerate to "off only" but never to "empty". `check_temperature`
should additionally always permit `target <= 0` for a heater the printer has.

---

## VERIFIED-SOUND No NaN/inf temperature target can reach the printer
`qidi_mcp/policy.py:445-447` | `if not limits.contains(target):` with `HeaterLimits.contains` = `self.min <= value <= self.max` (`qidi_mcp/limits.py:52-53`)

Why it matters for the hardware: the brief's specific worry. `nan > limit` is False, so a bounds
test written as "refuse if too big" lets NaN through. This one is written as "refuse unless
inside", which is the fail-*closed* spelling: `contains(nan)` is False, so the `not` makes it
refuse. Same for ±inf. I attacked this from both the env-var side and the tool-argument side and
could not get a non-finite target past it.

Evidence (script + real output): `docs/security/proofs/L2_nan.py` (guard level) and an end-to-end run
against the project's mock Moonraker (tool level, recording `printer.commands` — the literal
script POSTed to `/printer/gcode/script`):

```
  check_temperature(extruder  , nan   ) -> refused (limit_exceeded)
  check_temperature(extruder  , inf   ) -> refused (limit_exceeded)
  check_temperature(extruder  , -inf  ) -> refused (limit_exceeded)
  check_temperature(heater_bed, nan   ) -> refused (limit_exceeded)
  check_temperature(chamber   , nan   ) -> refused (limit_exceeded)

=== set_temperature(extruder, nan) (control: must be refused) ===
  result: "error": "limit_exceeded", "message": "nan °C is outside the allowed range for extruder (0–300 °C)."
  WIRE  : []
```

Nothing was sent. `check_position` is written the same fail-closed way and also holds:

```
  check_position(x, nan     ) -> refused (limit_exceeded)
  check_position(z, nan     ) -> refused (limit_exceeded)
=== move_relative(dx=nan) ===
  result: "error": "limit_exceeded", "message": "X=nan is outside the machine's travel (-2.0–392.0 mm)."
  WIRE  : []
```

Note `move_relative(dx=nan)` is caught by `check_position` on the *computed destination*
(`100.0 + nan = nan`), not by `check_jog` — see the next finding. The layering saved it.

Suggested fix: none. Worth a regression test pinning `contains()`'s fail-closed direction, since
"optimising" it to `value > self.max` would silently reopen this.

---

## HIGH check_jog / check_extrude / check_feedrate are fail-OPEN on NaN; `G1 Enan` and `Fnan` reach the wire
`qidi_mcp/policy.py:492` | `if abs(distance) > cap:`  (check_jog)
`qidi_mcp/policy.py:501` | `if abs(distance) > cap:`  (check_extrude)
`qidi_mcp/policy.py:510-513` | `if feedrate <= 0: raise ...` / `return min(feedrate, cap)`  (check_feedrate)

Why it matters for the hardware: these three are written in the "refuse if too big" direction the
brief warns about. `abs(nan) > 100.0` is False, so NaN is *not* refused — the guard silently
approves it. Unlike the temperature/position checks there is no second layer behind
`check_extrude` (there is no "extruder position" envelope to catch it), so a NaN distance goes
straight into the G-code string. `f"{nan:.3f}"` renders as `nan`, producing `G1 Enan F300`.
`check_feedrate` is worse: it *returns* the NaN, so the caller formats `Fnan` into the move.

What Klipper does with `G1 Enan`: `GCodeCommand.get_float()` calls `float("nan")`, which
succeeds, and the value flows into the kinematics as NaN. That is a parse-accepted,
physically-meaningless move — at best an immediate "Move out of range"/internal error that
shuts Klipper down mid-job, at worst a NaN propagated into the extruder's position tracking.
Either way it is an unplanned hard stop on a machine the agent was told it was safely driving,
and it is reached *after* the safety gate said yes, so the audit log records the call as cleared.

Evidence (script + real output): `docs/security/proofs/L2_nan.py` for the guards —

```
  check_jog(x, nan     )      -> PASSED THE GUARD   <== BYPASS
  check_jog(x, inf     )      -> refused (limit_exceeded)
  check_extrude(nan     )      -> PASSED THE GUARD   <== BYPASS
  check_extrude(1000000000.0)      -> refused (limit_exceeded)
  check_feedrate(nan     )     -> PASSED THE GUARD -> returned nan   <== BYPASS
  check_feedrate(inf     )     -> PASSED THE GUARD -> returned 12000.0
  check_feedrate(0.0     )     -> refused (limit_exceeded)
  min(nan, 12000) = nan   min(12000, nan) = 12000.0
```

That last line is the mechanism: `min(nan, cap)` returns NaN because `min` keeps the first
argument on a False comparison. (`inf` is handled correctly by luck of argument order —
`min(inf, 12000)` is 12000.)

And end-to-end against the mock, with the hotend hot enough to satisfy the extrude interlock
(`printer.extruder_temp = 250.0`), recording the literal wire bytes:

```
=== extrude_filament(distance=nan) ===
  result: {   "ok": true,   "action": "retracted",   "distance_mm": NaN,   "command_sent": ...
  WIRE  : ['SAVE_GCODE_STATE NAME=_qidi_mcp_extrude\nM83\nG1 Enan F300\nM400\nRESTORE_GCODE_STATE NAME=_qidi_mcp_extrude']
  <== 'nan' REACHED THE WIRE
=== extrude_filament(distance=5,feedrate=nan) ===
  WIRE  : ['SAVE_GCODE_STATE NAME=_qidi_mcp_extrude\nM83\nG1 E5.000 Fnan\nM400\nRESTORE_GCODE_STATE NAME=_qidi_mcp_extrude']
  <== 'nan' REACHED THE WIRE

=== move_relative(dx=1, feedrate=nan) ===
  result: {   "ok": true,   "jogged": {"x": 1.0}, ...
  WIRE  : ['SAVE_GCODE_STATE NAME=_qidi_mcp_jog\nG91\nG1 X1.000 Fnan\nM400\nRESTORE_GCODE_STATE NAME=_qidi_mcp_jog']
  <== 'nan' REACHED THE WIRE
```

Note the tool reported `"ok": true` in all three cases — the server believes it performed a
valid move.

Suggested fix: reject non-finite numbers at the guard, in the fail-closed direction, e.g. at the
top of each check: `if not math.isfinite(distance): raise LimitExceeded(...)`. `policy.py`
already imports `math` (for `ConfirmationBroker`'s TTL check), and `config.py:_env_float` already
does exactly this for env vars — the same discipline just needs to apply to tool arguments.
Cleanest is a single `_finite(name, value)` helper used by every numeric guard, so the property
holds for checks added later. Rewriting the comparisons as `not (abs(distance) <= cap)` would
also work but is easy to "simplify" back.

---

## VERIFIED-SOUND Motion bounds match printer.cfg exactly; no off-by-one or sign error at the negative minimums
`qidi_mcp/limits.py:95-97` | `x=AxisLimits(-2.0, 392.0),` / `y=AxisLimits(-5.0, 410.0),` / `z=AxisLimits(-5.0, 342.0)`
`qidi_mcp/limits.py:41-42` | `def contains(self, value: float) -> bool: return self.min <= value <= self.max`

Why it matters for the hardware: a sign error on `position_min` is how a toolhead gets driven into
the frame, and an off-by-one at the top is how it gets driven into the far endstop. The negative
minimums are the interesting case because `-2` and `-5` are *legal* off-bed service travel, so the
guard has to permit them while refusing anything beyond.

Evidence (script + real output): `docs/security/proofs/L3_motion.py`. Transcription checked against
[QIDI's `printer.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/printer.cfg) (`[stepper_x]` position_min -2 / position_max 392.0 at lines 183/185,
`[stepper_y]` -5.0/410 at 204/206, `[stepper_z]` -5/342 at 231/230), then boundaries probed at
±0.001 either side of every limit:

```
  X: profile=(-2.0, 392.0)  printer.cfg=(-2.0, 392.0)  MATCH
  Y: profile=(-5.0, 410.0)  printer.cfg=(-5.0, 410.0)  MATCH
  Z: profile=(-5.0, 342.0)  printer.cfg=(-5.0, 342.0)  MATCH

  check_position(x, -2.001    ) -> refused
  check_position(x, -2.0      ) -> allowed
  check_position(x, 392.0     ) -> allowed
  check_position(x, 392.001   ) -> refused
  check_position(y, -5.001    ) -> refused
  check_position(y, -5.0      ) -> allowed
  check_position(y, 410.0     ) -> allowed
  check_position(y, 410.001   ) -> refused
  check_position(z, 342.0     ) -> allowed
  check_position(z, 342.001   ) -> refused
```

Bounds are inclusive on both ends, matching Klipper's own `position_min`/`position_max` semantics.
No sign confusion: `-2.0` allowed and `-2.0001` refused on X; `+2.0` also allowed (it is inside
the range), which is correct rather than a mirror-image bug.

The Z floor composes correctly too — `check_position` uses
`floor = max(limits.min, self.settings.safety.min_jog_z)` (`policy.py:466`), so the hardware
minimum wins over a reckless env var:

```
  MIN_JOG_Z=0        -> effective floor=0.0       -5.0:refused  -4.0:refused  -0.001:refused  0.0:allowed
  MIN_JOG_Z=-1000    -> effective floor=-5.0      -5.0:allowed  -4.0:allowed  -0.001:allowed  0.0:allowed
```

`QIDI_MIN_JOG_Z=-1000` cannot drive the nozzle below the hardware's -5; it clamps to -5.0. (Note
`clamp_envelope` does *not* clamp `min_jog_z` itself — the value is stored raw, as the output
shows — but `check_position` recomputes the floor against `hw.z.min` on every call, so the
hardware limit is enforced where it matters. Worth a comment in `clamp_envelope` saying so, since
the omission looks like an oversight.)

An over-large `MAX_JOG_DISTANCE` is also contained, because `move_relative` range-checks the
computed destination:

```
  MAX_JOG_DISTANCE=1e9 -> cap=1000000000.0
    d=1000000000.0 -> jog allowed  ; destination 1000000100.0 refused
    d=-1000000000.0 -> jog allowed  ; destination -999999900.0 refused
```

`bed_mesh_min=(15.0, 15.0)` / `bed_mesh_max=(375.0, 375.0)` also match `printer.cfg:493-494`
(`mesh_min:15,15`, `mesh_max:375,375`), and both are inside the X/Y travel.

Suggested fix: none for the bounds themselves. Optionally clamp `min_jog_z` into `hw.z` inside
`clamp_envelope` for consistency, so the invariant is visible in one place instead of relying on
every caller to recompute it.

---

## MEDIUM set_z_offset's ±5 mm cap is fail-open on NaN — SET_GCODE_OFFSET Z_ADJUST=nan reaches the wire
`qidi_mcp/tools/motion.py:230` | `if abs(offset) > 5.0:`

Why it matters for the hardware: same fail-open comparison as `check_jog`/`check_extrude`, and
this one is *deliberately allowed mid-print* ("Deliberately allowed during a print: this is the
standard live fix", `motion.py:236`) with only `Interlocks()` — no during-print block. A NaN Z
offset applied with `MOVE=1` corrupts the live gcode offset while the machine is printing. The
tool then reports `"ok": true` and reads the offset back as `NaN`.

Reported here because it is the same root-cause class as the `limits.py`/`policy.py` NaN finding
and should be fixed in the same pass, even though the line lives in `tools/motion.py`.

Evidence (script + real output): end-to-end against the mock —

```
=== set_z_offset(offset=nan) ===
  result: {   "ok": true,   "command_sent": "SET_GCODE_OFFSET Z_ADJUST=nan MOVE=1",   "z_offset_now_mm": NaN }
  WIRE  : ['SET_GCODE_OFFSET Z_ADJUST=nan MOVE=1']
=== set_z_offset(offset=100 (control)) ===
  result: {   "ok": false,   "error": "invalid_request",   "message": "A Z offset change of 100.0 mm is implausible; the cap is ±5 mm.",
  WIRE  : []
```

The control shows the cap works for a large finite value; only NaN slips through.

Suggested fix: `if not math.isfinite(offset) or abs(offset) > 5.0:` — or route it through the
shared `_finite()` helper suggested in the NaN finding above.

---

## VERIFIED-SOUND klipper_command_name() agrees with the real parser on every single-line spelling I could construct
`qidi_mcp/limits.py:246-273` | ```parts = _KLIPPER_ARGS_RE.split(line.upper())
    if "".join(parts[:2]) == "N":
        return "".join(parts[3:5]).strip()
    return "".join(parts[:3]).strip()```

Why it matters for the hardware: if the screen names one command and the executor runs another,
the whole deny-list is decorative. This is the property the previous `N1 ` fix was about.

Evidence (script + real output): I did *not* reimplement the parser to compare against. The
oracle is the pinned, optionally fetched upstream file — `docs/security/proofs/_realparser.py` loads
Klipper's `klippy/gcode.py` after `bash scripts/fetch_upstream_sources.sh`, instantiates the real `GCodeDispatch` with a stub
printer/reactor, replaces every handler with a recorder, and calls the real
`_process_commands()`. The command name is therefore whatever Klipper's own code resolved and
dispatched. Sanity check that the harness is live:

```
'SAVE_CONFIG' real= 'SAVE_CONFIG' ours= 'SAVE_CONFIG'
'N1 SAVE_CONFIG' real= 'SAVE_CONFIG' ours= 'SAVE_CONFIG'
'M109S200' real= 'M109' ours= 'M109'
'  save_config  ' real= 'SAVE_CONFIG' ours= 'SAVE_CONFIG'
'M110 N1 SAVE_CONFIG' -> 'M110'
```

`docs/security/proofs/L4_fuzz.py` then compared **154,371** single-line inputs: a structured sweep of
30 prefixes x 24 suffixes x 5 case-manglings over every deny-listed command plus innocent
controls, unicode homoglyph/case-folding substitutions (dotless i `\u0131`, long s `\u017f`,
Kelvin sign `\u212a`, Cyrillic а/е/о/с, Roman numerals `\u216f`, `ff` ligature — all chosen
because they change identity under `.upper()`), BOM/NBSP/zero-width/NUL injection, the `*`
checksum form, `N`-line-number forms including `N-1`, `N1.5`, `N 1 `, `n1 `, `N1;`, and 60,000
random mutations:

```
inputs compared against the real parser: 154371

NAME DIVERGENCES (ours != Klipper's dispatched command)
  none

total name divergences: 0 (0 distinct shapes)

SAFETY BYPASSES (Klipper dispatches a deny-listed command; our screen said NOT dangerous)
  NONE FOUND

total bypasses: 0
```

This block is the historical transcript. With the separately fetched pinned
Klipper v0.13.0 parser and the current deny-list, a re-run checked **271,344**
single-line inputs with zero name divergences and zero bypasses. The differing
count reflects the changed input set, not an identical replay of the old parser.

The reason it holds is that the function is a transcription rather than an approximation: same
`strip()`, same `find(';')` truncation, same `([A-Z_]+|[A-Z*])` split on the same `.upper()`ed
string, same `parts[:3]` / `parts[3:5]` rejoin. Unicode tricks fail because *both* sides apply
the identical `.upper()` before splitting, so any transformation happens to both equally.

Suggested fix: none. I'd keep `docs/security/proofs/_realparser.py` and wire the differential into CI so
a future "cleanup" of this function has to prove it still matches upstream.

---

## LOW screen_gcode() misreports multi-line input — latent, not currently reachable
`qidi_mcp/limits.py:275` | `def screen_gcode(line: str) -> GcodeScreen:` ("Classify a single raw G-code line.")

Why it matters for the hardware: `klipper_command_name` resolves only the *first* line, but
Klipper executes every line. Given `"M105\nSAVE_CONFIG"` the screen returns `command='M105',
is_dangerous=False` while Klipper dispatches `['M105', 'SAVE_CONFIG']`. Any future caller that
passes a whole script to `screen_gcode` gets a false "safe" verdict.

Evidence: the following **historical transcript** screened the whole string in
one call and marked 10 multi-line cases as bypasses. The current
`docs/security/proofs/L4_diff.py` now models `run_gcode`'s real per-line screen:
with the fetched v0.13.0 parser it reports 10 name/classification divergences
but **zero reachable safety bypasses**. These excerpts are retained to document
the latent single-line API footgun, not to claim `run_gcode` accepts them:

```
'M105\nSAVE_CONFIG'            'M105'                 ['M105', 'SAVE_CONFIG']      False/True  <== BYPASS
'M105\r\nSAVE_CONFIG'          'M105'                 ['M105', 'SAVE_CONFIG']      False/True  <== BYPASS
'M105\nFORCE_MOVE'             'M105'                 ['M105', 'FORCE_MOVE']       False/True  <== BYPASS
'M105\nM502'                   'M105'                 ['M105', 'M502']             False/True  <== BYPASS
'M105\nSET_KINEMATIC_POSITION' 'M105'                 ['M105', 'SET_KINEMATIC_POSITION'] False/True  <== BYPASS
'M105\nPID_CALIBRATE'          'M105'                 ['M105', 'PID_CALIBRATE']    False/True  <== BYPASS
```

**Both live call sites are safe** *for command names*, which is why this was graded LOW and not
CRITICAL. I verified each end-to-end rather than assuming (but see the correction below — they
were not safe for parameter values):

```
### run_gcode multi-line (should be caught by splitlines)
  script='M105\nSAVE_CONFIG'    -> "error": "safety_violation"   WIRE: []
  script='M105\x0bSAVE_CONFIG'  -> "error": "safety_violation"   WIRE: []
  script='M105\rSAVE_CONFIG'    -> "error": "safety_violation"   WIRE: []

### run_macro with an embedded newline in the NAME
  macro='M105\nSAVE_CONFIG'   -> "error": "invalid_request", "no macro or command named ..."   WIRE: []
  macro='M105\rSAVE_CONFIG'   -> "error": "invalid_request"   WIRE: []
  macro='M105\x0bSAVE_CONFIG' -> "error": "invalid_request"   WIRE: []
```

`run_gcode` splits with `str.splitlines()` first (`tools/system.py:290`), and `splitlines()`
splits on a *superset* of what Klipper's `split('\n')` does (`\r`, `\x0b`, `\x0c`, `\x85`,
`\u2028`, `\u2029`). `run_macro` rejects a name that is not a known command.

> **Correction (second OSS-export review).** The original text here said the superset split
> "errs toward screening more lines, never fewer". That is true for *command names* and wrong
> for *values*: Klipper runs `M104 S100\rS374` as one line and keeps the last `S`, so it set
> 374 °C while the screen saw two harmless lines (`M104 S100`, `S374`). The same worked with
> `\x0c`, `\x1c`, `\u2028` and the rest, for heater targets, kinematic limits and coordinates.
> Fixed: `split_gcode_script()` in `limits.py` splits on `\n` only, as Klipper does, and refuses
> any other control character or Unicode line separator (tab, and `\r` ending a line, are
> allowed); `run_macro` applies the same rule to parameter values. Regression tests:
> `tests/test_envelope_bypasses.py` section 8.

---

## HIGH Deny-list gaps: SAVE_CONFIG_QD, SAVE_VARIABLE, RESET_HEATER_SCALING, SET_TMC_CURRENT-via-M4031, and more
`qidi_mcp/limits.py:192-217` | the `DANGEROUS_GCODE_PREFIXES` frozenset

Why it matters for the hardware: each of these does exactly what the deny-list exists to stop,
and each passes the screen today. Verified end-to-end through `run_gcode` with
`allow_dangerous=False` — "ALLOWED" below means the screen did not refuse it, so the caller only
needs the ordinary DESTRUCTIVE ticket it would need for a harmless command:

```
run_gcode(script=..., allow_dangerous=False)   VERDICT    note
-----------------------------------------------------------------------------------------------
SAVE_CONFIG_QD                             ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
SAVE_VARIABLE VARIABLE=z_offset VALUE=99   ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
RESET_HEATER_SCALING HEATER=extruder       ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
SET_HEATER_TEMPERATURE HEATER=extruder TAR ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
SET_VELOCITY_LIMIT ACCEL=100000            ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
STEPPER_BUZZ STEPPER=stepper_z             ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
M84                                        ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
BED_MESH_CALIBRATE                         ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
PROBE_CALIBRATE                            ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
SHAPER_CALIBRATE                           ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
M4029                                      ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
M4031                                      ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
M901                                       ALLOWED    passed screen -> ticket issued (redeemable)   <== NOT REFUSED
SAVE_CONFIG                                REFUSED    deny-list hit
```

The single most important one: **`SAVE_CONFIG_QD`**. It is QIDI's own "save config *without*
restarting" ([QIDI's `qd_macro.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/qd_macro.cfg#L171) — comment `保存配置不重启`, "save config no
restart"). It writes persistent config just like `SAVE_CONFIG`, which *is* denied, but under a
different name, and it does not even announce itself with a restart. The deny-list catches the
upstream spelling and misses the vendor's.

Category sweep against [Klipper's G-Codes reference](https://github.com/Klipper3d/klipper/blob/61c0c8d2ef40340781835dd53fb04cc7a454e37a/docs/G-Codes.md) (`docs/security/proofs/L5_denylist.py`; first run `bash scripts/fetch_upstream_sources.sh`), abridged:

```
WRITES PERSISTENT CONFIG:
    SAVE_CONFIG                documented  denied=True
    SAVE_VARIABLE              documented  denied=False   <== MISSING FROM DENY-LIST
    BED_MESH_PROFILE           documented  denied=False   <== MISSING FROM DENY-LIST
    Z_OFFSET_APPLY_PROBE       documented  denied=False   <== MISSING FROM DENY-LIST
    Z_OFFSET_APPLY_ENDSTOP     documented  denied=False   <== MISSING FROM DENY-LIST
    ENDSTOP_PHASE_CALIBRATE    documented  denied=False   <== MISSING FROM DENY-LIST
DISABLES ENDSTOPS / LIMITS / STEPPERS:
    SET_VELOCITY_LIMIT         documented  denied=False   <== MISSING FROM DENY-LIST
    STEPPER_BUZZ               documented  denied=False   <== MISSING FROM DENY-LIST
    M84 / M18                  documented  denied=False   <== MISSING FROM DENY-LIST
STARTS UNATTENDED HEATING:
    PID_CALIBRATE              documented  denied=True
    SET_HEATER_TEMPERATURE     documented  denied=False   <== MISSING FROM DENY-LIST
    MPC_CALIBRATE / M303       not-in-md   denied=False   <== MISSING FROM DENY-LIST
LONG UNATTENDED MOTION / CALIBRATION:
    SHAPER_CALIBRATE, TEST_RESONANCES, PROBE_CALIBRATE, MANUAL_PROBE,
    BED_MESH_CALIBRATE, Z_TILT_ADJUST, QUAD_GANTRY_LEVEL, TESTZ, G29/G32/G34
                               documented  denied=False   <== MISSING FROM DENY-LIST
```

Notes on the ones that matter most, beyond `SAVE_CONFIG_QD`:

* **`RESET_HEATER_SCALING`** — see the separate finding below; it re-widens the firmware's own
  temperature caps. `SET_HEATER_SCALING` is denied; its inverse is not.
* **`SET_HEATER_TEMPERATURE`** — the raw Klipper heater command. `thermal.py` deliberately avoids
  it in favour of QIDI's `M104`/`M140`/`M141` macros so the firmware's scaling clamp applies
  (`tools/thermal.py:7-12`), but `run_gcode` will send it directly, skipping both our envelope
  and the vendor clamp. `SET_HEATER_TEMPERATURE HEATER=extruder TARGET=374` passed the screen.
* **`SET_VELOCITY_LIMIT`** — raises `max_accel`/`max_velocity` above the profile's own figures,
  making the kinematic limits the rest of the envelope assumes untrue.
* **`M84`/`M18`** — drop stepper enable, losing position mid-print; `SET_STEPPER_ENABLE` is
  denied for exactly this reason but its traditional G-code spelling is not.

Also worth fixing: the set is *named* `..._PREFIXES` but matched with equality
(`token == bad or stem == bad`, `limits.py:290-293`). That is the safer choice — a real prefix
match would make `RESTART` swallow `RESTART_SERVICE` — but the name invites someone to add
`"SET_TMC"` expecting it to cover the family:

```
  screen_gcode('SAVE_CONFIG_QD'      ).is_dangerous = False  matched=None
  screen_gcode('SET_TMC'             ).is_dangerous = False  matched=None
  screen_gcode('SET_TMC_CURRENT'     ).is_dangerous = True   matched='SET_TMC_CURRENT'
  screen_gcode('RESET_HEATER_SCALING').is_dangerous = False  matched=None
```

Suggested fix: add at minimum `SAVE_CONFIG_QD`, `SAVE_VARIABLE`, `RESET_HEATER_SCALING`,
`SET_HEATER_TEMPERATURE`, `SET_VELOCITY_LIMIT`, `STEPPER_BUZZ`, `M84`, `M18`, `M303`,
`Z_OFFSET_APPLY_PROBE`, `Z_OFFSET_APPLY_ENDSTOP`, `ENDSTOP_PHASE_CALIBRATE`, `MPC_CALIBRATE`, and
the unattended-calibration family (`SHAPER_CALIBRATE`, `TEST_RESONANCES`, `PROBE_CALIBRATE`,
`MANUAL_PROBE`, `BED_MESH_CALIBRATE`, `Z_TILT_ADJUST`, `QUAD_GANTRY_LEVEL`, `TESTZ`, `G29`,
`G32`, `G34`). Rename the constant to `DANGEROUS_GCODE_COMMANDS` to match its equality semantics.
Better still, invert the model for the raw escape hatch: allow-list the commands known to be safe
and require `allow_dangerous=True` for everything else, so a vendor-specific name nobody
anticipated fails closed instead of open.

---

## HIGH Vendor macro RESET_HEATER_SCALING re-widens the firmware's own temperature caps under an innocuous name
`qidi_mcp/limits.py:205-206` | `"SET_HEATER_SCALING",       # would widen the firmware's own temp caps` / `"HEATER_TEMP_LIMIT",`
(the gap: `RESET_HEATER_SCALING` is absent)

Why it matters for the hardware: this is the vendor macro asked for in check 5 — a restricted
operation wearing a harmless name. [QIDI's `heaters.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/heaters.cfg#L283-L296):

The upstream `reset_heater_scaling` macro reads `min_temp` and `max_temp`
from the configured heater and calls `SET_HEATER_SCALING` with scale 1.0 and
bump 0.0. Follow the pinned upstream link above or fetch the config locally
using `bash scripts/fetch_upstream_sources.sh` to inspect its exact source.

`MAXIMUM={settings.max_temp}` is read from `printer.cfg`, i.e. **375** for the extruder, **125**
for the bed, **68** for the chamber. `heater_temp_limit` had set those clamps to 370 / 120 / 65.
So `RESET_HEATER_SCALING HEATER=extruder` moves the firmware's own ceiling from 370 up to 375 —
the MCU shutdown threshold — and it does it by calling `SET_HEATER_SCALING`, which the deny-list
blocks when invoked directly. The comment on line 205 states the intent ("would widen the
firmware's own temp caps") and the list even includes `HEATER_TEMP_LIMIT`, the macro that *sets*
the caps; only the one that *clears* them was missed.

The consequence is compound: `thermal.py` treats QIDI's macro clamp as one of three independent
layers ("a request passes *three* independent checks: ours, QIDI's macro, and Klipper's
min_temp/max_temp" — `tools/thermal.py:9-12`). One un-denied command removes the middle layer for
the rest of the session, silently, and nothing re-applies it until `heater_temp_limit` runs again.

Evidence (script + real output): `docs/security/proofs/L5_denylist.py` part (b) walks all 98 vendor
macros and reports which reach a restricted primitive:

```
vendor macros found: 98

  G29                          bed_mesh.cfg           denied=False calls: SAVE_CONFIG_QD, SAVE_VARIABLE, SET_STEPPER_ENABLE   <== macro NOT denied
  M4029                        qd_macro.cfg           denied=False calls: DISABLE_ALL_SENSOR, SAVE_CONFIG, SAVE_CONFIG_QD, SAVE_VARIABLE   <== macro NOT denied
  M4031                        qd_macro.cfg           denied=False calls: SET_STEPPER_ENABLE, SET_TMC_CURRENT   <== macro NOT denied
  M901                         qd_macro.cfg           denied=False calls: SAVE_CONFIG, SAVE_CONFIG_QD   <== macro NOT denied
  RESET_HEATER_SCALING         heaters.cfg            denied=False calls: HEATER_TEMP_LIMIT, RESET_HEATER_SCALING, SET_HEATER_SCALING   <== macro NOT denied
  M201 / M203 / M205           velocity.cfg           denied=False calls: SET_VELOCITY_LIMIT   <== macro NOT denied
  HEATER_TEMP_LIMIT            heaters.cfg            denied=True  calls: SET_HEATER_SCALING
  SET_HEATER_SCALING           heaters.cfg            denied=True  calls: HEATER_TEMP_LIMIT, SET_HEATER_SCALING

macros that reach a restricted primitive but are NOT on the deny-list: 20
```

and end-to-end: `RESET_HEATER_SCALING HEATER=extruder` → `ALLOWED / passed screen`.

Three more from that table deserve naming, because a deny-list of primitives cannot catch a macro
that wraps them:

* **`M4031`** issues `SET_TMC_CURRENT` (denied directly) to run the Z steppers at 0.8x current,
  then restores it — [QIDI's `qd_macro.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/qd_macro.cfg#L186-L187). Motor-current control via a
  four-digit M-code.
* **`M4029`** is `run_macro`'s own advertised example of a friendly routine ("M4029 bed
  levelling", `tools/system.py:369`). Its body disables every sensor, heats the hotend to 260 via
  `CLEAR_NOZZLE HOTEND=260`, runs `Z_TILT_ADJUST` + `BED_MESH_CALIBRATE`, and finishes with
  `SAVE_CONFIG` (restart) or `SAVE_CONFIG_QD` — a long unattended heat-and-move that also writes
  persistent config. [QIDI's `qd_macro.cfg`](https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/klipper-macros-qd/qd_macro.cfg#L131-L176).
* **`M901`** runs `SHAPER_CALIBRATE` then `SAVE_CONFIG`/`SAVE_CONFIG_QD`.

Suggested fix: add `RESET_HEATER_SCALING` and `SAVE_CONFIG_QD` to the deny-list now — they are
the two that defeat existing entries. Then treat "macro that wraps a denied primitive" as its own
problem: screening a macro *name* can never be complete, because the body is on the printer, not
here. Since `capabilities.py` already fetches the macro inventory, the honest fix is to resolve a
macro's body via `configfile.settings` at call time and screen *that*, refusing when a macro
expands to a denied primitive. Failing that, deny the known-bad vendor names explicitly
(`M4029`, `M4031`, `M901`, `G29`, `M201`, `M203`, `M205`) and say in `run_macro`'s description
that a macro body is not screened.

---

## HIGH A target above the safety ceiling DOES reach the printer, via run_gcode's raw path
`qidi_mcp/limits.py:192-217` (no heater command in `DANGEROUS_GCODE_PREFIXES`) + `qidi_mcp/tools/system.py:291-295` (screen checks the deny-list only, never the thermal envelope)

Why it matters for the hardware: check 2 asks me to confirm no target above the ceiling can reach
the printer. It can. `check_temperature` is only consulted by the *dedicated* thermal tools; the
raw G-code path screens for deny-listed command names and never range-checks a temperature
argument. With the default envelope (extruder max 300), 374 °C is refused by `set_temperature`
and accepted by `run_gcode` in the same server, same session, same settings. 374 °C sits 1 °C
under the MCU shutdown trip and far above any filament's safe range — that is a scorched hotend
and possibly a fire, and it is reached without ever setting an env var.

Evidence (script + real output): both paths in one run, against the mock, showing the literal
wire bytes:

```
envelope extruder max = 300.0

A) dedicated tool: set_temperature(extruder, 374)
   -> {   "ok": false,   "error": "limit_exceeded",   "message": "374.0 °C is outside the allowed range for extruder (0–300 °C
   WIRE: []

B) raw path: run_gcode('SET_HEATER_TEMPERATURE HEATER=extruder TARGET=374') then redeem ticket
   ticket: qidi-4u9tWK5wQ9zK
   -> {   "ok": true,   "lines_sent": 1,   "commands": [     "SET_HEATER_TEMPERATURE"   ],   "dangerous_commands_allowed": [],
   WIRE: ['SET_HEATER_TEMPERATURE HEATER=extruder TARGET=374']
```

Note `"dangerous_commands_allowed": []` — the server does not consider this a dangerous call, so
it is not even flagged in the audit line as such. `allow_dangerous` was never needed; only the
ordinary DESTRUCTIVE ticket that any raw G-code requires.

The same hole applies to `M104`/`M109`/`M140`/`M190`/`M141`/`M191` through `run_gcode`. Those at
least pass through QIDI's scaling macros (capping at 370/120/65 *if* `heater_temp_limit` has run
and has not been undone by `RESET_HEATER_SCALING` — see the finding above), so
`SET_HEATER_TEMPERATURE` is the worst case: it bypasses our envelope *and* the vendor clamp,
leaving only Klipper's 375 shutdown trip.

I am not calling this CRITICAL only because reaching it takes a deliberate two-step ticket
redemption on a tool documented as an escape hatch — an agent cannot stumble into it in one call.
It is still a complete bypass of the numeric envelope that `policy.py`'s docstring calls a
load-bearing guarantee ("The envelope is enforced numerically, not by prose... before a single
byte reaches Moonraker", `policy.py:24-27`). That claim is only true of the dedicated tools.

Suggested fix: screen *arguments*, not just command names, on the raw path. Parse the resolved
command's params (the vendored parser's own `params` dict shows how) and, for any heater-setting
command, run the target through `policy.check_temperature` before sending; likewise feed
coordinates in `G0`/`G1` through `check_position`. Any heater/motion command whose arguments
cannot be parsed should require `allow_dangerous=True`. At minimum, add the heater commands to
the deny-list so the raw path demands `allow_dangerous=True` and records the fact.
