# Audit: qidi_mcp/runtime.py + qidi_mcp/capabilities.py (interlocks, state cache, fallback)

Ground truth: https://github.com/QIDITECH/QIDI_MAX4/blob/47fc14cefe064dbd30b90582429fe650883ae900/config/printer.cfg (min_extrude_temp 135, extruder max_temp 375),
tests/mock_moonraker.py driven over loopback only (socket guard from tests/conftest.py kept
active in every script; the live printer was never contacted).
Scratch scripts live in /tmp/qidi_audit/ (not in the repo). Findings appended as confirmed.

## HIGH Interlocks are evaluated on a 0.75 s cached snapshot; a refused call primes the cache and the retry is authorised against stale state
qidi_mcp/runtime.py:49 `self._state_ttl = 0.75`
qidi_mcp/runtime.py:60-62
```
now = time.monotonic()
if not fresh and self._state is not None and (now - self._state_at) < self._state_ttl:
    return self._state
```
qidi_mcp/runtime.py:117 `state = await self.state()`   (authorize() never passes fresh=True)

Why it matters on real hardware: every `forbid_during_print` / `require_homed` decision for home_axes, move_absolute,
set_temperature, extrude_filament, motors_off is made on a snapshot up to 750 ms old. The docstring claims the TTL
"never mak[es] a safety decision on stale data" — that is false. Two realistic sequences defeat it:
 (a) Agent calls a MOTION tool without `confirm` → refused with `confirmation_required`, but authorize() has already
     populated `_state` with print_state=standby. A refusal does not invalidate the cache (only `rt.ok()` does). The
     agent immediately retries with confirm=True (LLM round-trips are routinely <750 ms on a cached prompt) — meanwhile a
     Fluidd/Klipperscreen user has pressed Print. The retry is authorised on the stale "standby" and G28 / G1 / M84 / M104
     is injected into a running print: G28 mid-print ploughs the toolhead through the part, M84 drops Z on the part,
     M104 S250 changes hotend temp under a PLA print.
 (b) homed_axes is cached across an out-of-band FIRMWARE_RESTART: the printer is unhomed (Klipper would refuse the G1
     with "Must home axis first" — that is the only thing that saves you) but the MCP layer authorises the move and its
     audit log records "ok".
The mock also shows the M112 variant is *accidentally* safe: the mock returns 503 on the gcode POST itself, not because
the gate saw the shutdown.

Proof: /tmp/qidi_audit/t1b_stale_realistic.py and t1c.py (loopback mock only). Output:
```
call1 (no confirm): confirmation_required | cache primed print_state= standby
call2 (confirm) 0.075s later: ok= True | G1 sent while printing: True
audit tail: [('move_absolute','refused','confirm_flag_missing'), ('move_absolute','ok',None)]

set_temperature    cached-standby, real=printing -> ok=True sent=['M104 S250']
home_axes          cached-standby, real=printing -> ok=True sent=['G28']
motors_off         cached-standby, real=printing -> ok=True sent=['M84']
extrude_filament   cached-standby, real=printing -> ok=True sent=['... G1 E10.000 F300 ...']
C move after external FIRMWARE_RESTART (unhomed, cached xyz): ok=True sent=['... G1 X100.000 F3000 ...']
```
Control: after `asyncio.sleep(0.8)` the same call is `interlock_blocked` (t1_stale.py case B), and move_relative — the
one tool that happens to call `rt.state(fresh=True)` before authorize — is blocked (case D). So the cache alone is the cause.

Suggested fix: `authorize()` must call `self.state(fresh=True)` — interlock evaluation is exactly the place a cache must
not be used; keep the cache only for post-action reporting. Additionally invalidate `_state` on every authorize() exit
path (refusal included) and on any QidiError from a client call. Consider also asking Klipper to do the check atomically
(e.g. prefix the script with a macro that raises if `printer.print_stats.state == "printing"`), since even a fresh
query leaves an unavoidable TOCTOU window between the GET and the gcode POST.

## HIGH Degraded-state fallback forges `print_state="standby"` from any HTTP 503, so `forbid_during_print` is bypassed on every `require_klippy_ready=False` tool
qidi_mcp/runtime.py:118-132
```
except KlippyNotReady as exc:
    ...
    if interlocks.require_klippy_ready:
        raise
    state = PrinterState(klippy_state="shutdown", klippy_message=str(exc.message),
                         print_state="standby", homed_axes="")
```
qidi_mcp/client.py:179-183 — *every* 503 becomes `KlippyNotReady`, regardless of body:
```
if resp.status_code == 503:
    raise KlippyNotReady(f"Klipper is not ready: {detail or 'service unavailable'}.", ...)
```
Affected tools (all declare `require_klippy_ready=False, forbid_during_print=True`): system.py:216 restart_firmware,
:252 restart_klipper_host, :497 restart_service, :525 reboot_printer, :554 shutdown_printer.

Why it matters on real hardware: the comment assumes "Klipper is down, so nothing is printing". That inference is only
valid when the 503 really means Klipper is in shutdown. Moonraker also answers 503 "Klippy Host not connected" whenever
*Moonraker's* unix socket to Klipper is absent — for the first seconds after a Moonraker restart/crash (Klipper keeps
printing through a Moonraker restart), or from any reverse proxy in front of :7125. In that window the interlock is
not "unknown → refuse", it is "unknown → assume idle", and the one class of tools that ends a print or powers the host
off (reboot / shutdown / RESTART / systemctl restart klipper / FIRMWARE_RESTART) is authorised mid-print. On a Max4 a
FIRMWARE_RESTART during an ABS print leaves a 340 °C-capable hotend parked in the part until Klipper reloads and
`idle_timeout` fires; a host shutdown leaves the heaters under MCU control with no host watchdog and needs a physical
power cycle. Nothing in the audit log records that the decision was made on a synthetic state.

Proof: /tmp/qidi_audit/t3_fallback.py — mock with `print_state="printing"`, klippy_state="ready", and
/printer/objects/query patched to return Moonraker's exact 503 body. Output:
```
state() raises: KlippyNotReady
reboot_printer         while PRINTING + query 503 -> step2 ok=True
shutdown_printer       while PRINTING + query 503 -> step2 ok=True
restart_klipper_host   while PRINTING + query 503 -> step2 ok=True
restart_service klipper while PRINTING + query 503 -> ok=True
restart_firmware       while PRINTING + query 503 -> ok=True  (firmware_restart POST reached mock: 1)
mock counters: reboots=1 shutdowns=1 host_restarts=1 service_actions=[('restart','klipper')]
CONTROL reboot_printer / shutdown_printer / restart_firmware  printing, query OK -> interlock_blocked
```
Note the fallback state is not written to `_state` (good) — the bypass is per-call, not sticky.

Suggested fix: never synthesise a permissive state. On KlippyNotReady with `require_klippy_ready=False`, corroborate
before clearing `forbid_during_print`: call `/server/info` (works without Klippy) and only treat the job as inactive if
`klippy_state in {"shutdown","error"}`; if `klippy_connected` is false with state unknown, refuse with a hint to retry, or
require an explicit `override_unknown_state=True` argument that is logged. At minimum record `detail="fallback_state"`
in the audit entry so the decision is visible after the fact.

## HIGH A partial capability discovery (Klipper not ready at discovery time) is cached for 300 s as "printer has no chamber heater" — `cooldown` then leaves the chamber heater on
qidi_mcp/capabilities.py:200-212
```
async def get(self, *, refresh: bool = False) -> Capabilities:
    fresh = (self._caps is not None and not refresh
             and (time.monotonic() - self._caps.discovered_at) < self.ttl)
    ...
    self._caps = await self._discover()
```
qidi_mcp/capabilities.py:257-260 / 284-288 — object list and configfile failures are swallowed into `warnings`,
leaving `caps.objects == ()`, `caps.heaters == {}`, `has_heated_chamber=False`, `fans == {}`. `_discover()` still
returns a *successful-looking* Capabilities that `get()` caches with a full 300 s TTL.
Consumers: tools/thermal.py:222-230 `cooldown` only appends `M141 S0` `if caps.has_heated_chamber`;
tools/thermal.py:86-90 `set_temperature(chamber, …)` raises NotSupported when `not caps.has_heated_chamber`.

Why it matters on real hardware: the natural time for the first `capabilities()` call (or a TTL re-discovery) to land is
right after an e-stop / FIRMWARE_RESTART / power cycle — exactly when Klipper answers 503 for /printer/objects/list. The
Max4 has `[heater_generic chamber]` (printer.cfg:302, max_temp 68) driving a ~300 W chamber heater. With the poisoned
cache, `cooldown` — documented as "the right thing to call when you are unsure of the machine's state" — silently omits
`M141 S0`, and the explicit `set_temperature heater=chamber target=0` is *refused* with not_supported. For the next five
minutes the server has no way to turn the chamber heater off except emergency_stop or run_gcode. The same poisoned cache
also empties `fans`, `output_pins`, `filament_sensors`, so every device tool reports the hardware as absent.

Proof: /tmp/qidi_audit/t4_caps.py — mock in klippy_state="startup" during discovery, then ready with chamber target 60:
```
discovered during startup: objects=0 heaters=[] has_heated_chamber=False fans=0 warnings=(... 'Could not list printer objects ...')
cooldown result: {'ok': True, 'heaters_off': ['extruder', 'heater_bed'], 'commands_sent': ['M104 S0', 'M140 S0']}
after cooldown targets: extruder=0.0 bed=0.0 chamber=60.0
set_temperature chamber 0 -> not_supported | chamber target still 60.0
caps still cached (age 0.1s, ttl 300s): has_heated_chamber=False
```
Suggested fix: (1) if object_list / configfile failed, mark the snapshot degraded and do NOT cache it (or cache for ≤5 s);
(2) `cooldown` must not depend on discovery at all — send `M141 S0` unconditionally (QIDI's macro is a no-op without a
chamber) or use `TURN_OFF_HEATERS`, which Klipper always provides; (3) "heater off" requests (target<=0) should never be
refused on a capability check.

## MEDIUM Odd-one-out interlock sets: `run_gcode` / `run_macro` carry no `forbid_during_print`, so every motion/thermal interlock is trivially bypassed mid-print by the same agent
qidi_mcp/tools/system.py:318-333 (run_gcode)
```
interlocks=Interlocks(require_klippy_ready=False),
```
qidi_mcp/tools/system.py:407-414 (run_macro)
```
interlocks=Interlocks(),
```
Compare: motion.py:63,116,180,288,384 and thermal.py:105 all set `forbid_during_print=True`; calibration.py and qidi.py
tools set it too. The two generic escape hatches are the only tools that can move axes / set heaters and do not.
`run_macro` additionally performs no `check_temperature` (thermal.py:93 does) — `M104 S=300`/`M140 S=120` skip the
numeric envelope; only QIDI's macro-level `heater_temp_limit` (370/120/65) remains.

Why it matters on real hardware: the whole point of the print interlock is that an LLM agent that has just been told
"interlock_blocked: a print job is printing" does not get to inject G28 / G1 Z-50 / M84 / M104 S300 into the running
job. Because the agent controls the tool choice, and run_macro/run_gcode share the same two-step ticket that every
other DESTRUCTIVE tool uses (no extra `allow_during_print` flag), the dedicated tools' interlock is advisory. G28 mid-print
drags the nozzle across the part at 340 °C; M84 mid-print drops the gantry.

Proof: /tmp/qidi_audit/t2c_oddone.py — mock with print_state="printing", ticket redeemed on second call:
```
PRINTING run_macro {'macro': 'G28'}                        -> ok=True sent=['G28']
PRINTING run_macro {'macro': 'M104', 'params': {'S': 300}} -> ok=True sent=['M104 S=300']
PRINTING run_macro {'macro': 'M4029'}                      -> ok=True sent=['M4029']
PRINTING run_gcode {'script': 'G28'}                       -> ok=True sent=['G28']
PRINTING run_gcode {'script': 'M104 S300\nM140 S120\nM141 S65'} -> ok=True
PRINTING run_gcode {'script': 'G91\nG1 Z-50 F600'}         -> ok=True
PRINTING run_gcode {'script': 'M84'}                       -> ok=True sent=['M84']
```
(Control from t2_interlocks.py: home_axes / move_absolute / motors_off / set_temperature → interlock_blocked in the same state.)

Suggested fix: give run_gcode/run_macro `forbid_during_print=True` by default, with an explicit, audited
`allow_during_print: bool = False` argument (mirroring `allow_dangerous`) for the legitimate mid-print cases
(M117, M220, SET_GCODE_OFFSET, custom PAUSE variants). Run the same `screen_gcode` classifier to auto-allow a
whitelist of print-safe commands instead of blanket-permitting.

## MEDIUM Unlocked shared `_state` cache: a slow, older query reply overwrites a newer snapshot (lost-update race), then authorises motion mid-print
qidi_mcp/runtime.py:60-62, 92 — `state()` has no lock/sequence check; whichever response arrives *last* wins:
```
self._state, self._state_at = state, now     # `now` was captured BEFORE the await, so an old snapshot is stored with an old-but-still-valid timestamp
```
qidi_mcp/runtime.py:44-49 — `_state`, `_state_at` are plain attributes shared by every concurrent tool task; MCP
serves tool calls concurrently and nothing in server.py serialises mutating tools.

Why it matters on real hardware: two agent calls (or one agent + the TTL) overlap: call 1's `/printer/objects/query`
is answered by Moonraker while idle but the reply is delayed ~400 ms (Moonraker on the Max4's Cortex-A53 under a
print + camera load routinely does this); a print starts; call 2 queries fresh and is correctly refused. Call 1's
stale reply then lands and *replaces* the newer "printing" snapshot with "standby". For the next ≤750 ms every
`forbid_during_print` tool passes. A refused call (case t5c: call 1 lacked `confirm`) still writes the cache because
refusal paths never invalidate. Combined with finding #1 this makes the interlock fail in exactly the loaded
conditions where a human is likely to be pressing Print on the touchscreen.

Proof: /tmp/qidi_audit/t5b.py, t5c.py — mock's objects/query computes the snapshot then sleeps 0.4 s before replying:
```
C newer snapshot cached: printing
C after late reply: cached print_state = standby  _state_at age = 0.406s
C home_axes authorised on cache -> ok=True sent=['G28']
t1 (slow, no confirm) err=confirmation_required | t2 err=interlock_blocked | cache after both: print_state=standby
t3 home_axes -> ok=True sent=['G28']  (real printer print_state=printing)
```
Other concurrency checks that held (VERIFIED-SOUND): concurrent e-stop during an in-flight motion authorize → the move
was refused with klippy_not_ready and no G-code was posted after the stop (t5_concurrency.py case B); `sync_limits()`
replaces `policy.settings` atomically by object swap, readers see either old or new envelope, both valid (case D).

Suggested fix: make `state()` monotonic — only store a snapshot if its *request start time* is newer than `_state_at`
(or use a per-Runtime `asyncio.Lock` around query+store, and have `authorize()` bypass the cache entirely per finding #1).
Also invalidate the cache in `authorize()` whenever the policy raises.

## VERIFIED-SOUND capabilities.py `has_*` inference never reads a MISSING capability as PRESENT; degraded discovery fails in the strict direction for limits
Tried (/tmp/qidi_audit/t4b_caps.py):
- Full Max4 object list (mirrors printer.cfg: [probe_air], [bed_mesh], [z_tilt], [heater_generic chamber], [input_shaper],
  [resonance_tester], multi_color_controller): has_probe/has_bed_mesh/has_z_tilt/has_input_shaper/has_resonance_tester/
  has_heated_chamber/has_multi_color = True, has_quad_gantry_level = False (correct: no [quad_gantry_level] in printer.cfg),
  box_count = 0 from save_variables. `has_probe` recognises QIDI's `probe_air` (printer.cfg:463) — correct.
- Empty `configfile.settings` (503 or QIDI fork variant): heaters get max_temp 0.0 → `effective_envelope` narrows the
  policy to 0–0 °C, so `set_temperature extruder 200` is **refused** (`limit_exceeded ... (0–0 °C)`); `min_extrude_temp`
  falls back to 170 (stricter than the real 135); axis_limits empty → motion still gated by the static HardwareProfile.
  All fail-closed. (Usability bug — a transient discovery failure disables heating for 300 s — but not a safety bug.)
- Upstream Moonraker (no /printer/list_endpoints): `has_soft_abort=False`, `abort_heating` → not_supported (never
  pretends breakheater exists).
- Empty gcode_help: `has_gcode("M603")` still True via the macros list (that macro IS present), `has_gcode("SET_INPUT_SHAPER")`
  False but `has_input_shaper` True via `"input_shaper" in cfg` — consistent with printer.cfg.
- `state()` min_extrude_temp: 135 before discovery (hard-coded default matches printer.cfg:95), 135 after discovery.
Numeric envelope is only ever *narrowed* (`narrow()` uses `min(current.max, hi)`; clamp_envelope caps to hardware).

## LOW `state()` silently accepts a missing/short `toolhead.position` and defaults it to (0,0,0,0)
qidi_mcp/runtime.py:78-81
```
position = toolhead.get("position") or [0.0, 0.0, 0.0, 0.0]
while len(position) < 4: position.append(0.0)
```
Why it matters: `move_relative` computes `destination = current + delta` from this (motion.py:163-165). If Klipper omits
`toolhead` (object not in query result — e.g. QIDI fork quirk or a query during startup that returns partial status),
current becomes (0,0,0) and a `dz=-3` jog is bounds-checked as Z=-3 → refused by min_jog_z, fine; but `dz=+5` from a
real Z=340 is bounds-checked as Z=5 and **passed** — Klipper would then clip/error at 342. Klipper's own soft limits
catch it, so LOW. Proven on the mock with `toolhead.position` stripped from the reply (/tmp/qidi_audit/t6_pos.py):
```
state.position with toolhead.position missing: (0.0, 0.0, 0.0, 0.0)
move_relative dz=+5 from real Z=340 -> ok=True computed_to={'z': 5.0} sent=['... G91 G1 Z5.000 F3000 ...']
```
The tool reports `from: {z: 0.0}` / `to: {z: 5.0}` to the agent — wrong by 340 mm. Fix: refuse when `toolhead`/`position`
is absent instead of substituting zeros.

## VERIFIED-SOUND Fallback scope is limited to HTTP 503; other failure modes fail closed
Tried (/tmp/qidi_audit/t3b.py, t1_stale.py case C, t2b.py):
- Moonraker unreachable (host off / power-cycle): `ConnectionFailed` is not caught by authorize() → restart_firmware,
  reboot_printer, run_gcode all return `connection_failed`; no synthetic state.
- objects/query returns 404 (proxy) → `NotSupported` propagates; both restart_firmware and move_absolute refused.
- Fallback state has `homed_axes=""`, `extruder_temp=0` → `require_homed` and `require_extrude_temp` still refuse even
  with `require_klippy_ready=False` (only `forbid_during_print` is the weak field — see HIGH finding above).
- Fallback is never written to `_state`, so it cannot poison later calls.
- With Klipper truly in shutdown (webhooks.state="shutdown" returned via a *200* query, which is real Klipper behaviour —
  the mock's blanket 503 is less realistic), every `require_klippy_ready=True` tool is `interlock_blocked` with the
  shutdown message, `restart_firmware` proceeds, and `cooldown` (no authorize) still sends M104/M140 S0.
- After an out-of-band M112 with the state cached "ready+homed": the gcode POST itself gets 503 → `klippy_not_ready`,
  nothing executed (safe by accident of Moonraker's behaviour, not by the gate — see HIGH #1).

## VERIFIED-SOUND Each declared interlock blocks when evaluated on fresh state
/tmp/qidi_audit/t2_interlocks.py, fresh Runtime per scenario (no cache priming):
- klippy shutdown → move_absolute, set_temperature, home_axes, extrude_filament, set_z_offset, set_speed_factor all
  `klippy_not_ready`; zero G-code posted.
- printing → move_absolute, move_relative, set_temperature, home_axes, extrude_filament, motors_off all `interlock_blocked`;
  paused → move_absolute, set_temperature blocked (job_active covers paused). Zero G-code posted.
- unhomed → move_absolute/move_relative blocked; homed "xy" → move z blocked, move x allowed (per-axis check correct).
- min_extrude_temp: extrude at 24.5 and 134.9 blocked, 135.0 allowed (matches printer.cfg:95 `min_extrude_temp: 135`);
  cold *retract* (negative distance) also blocked — correct, Klipper refuses both directions.
- `require_job_active`: pause/resume/cancel refuse in standby (existing test suite covers this).

## LOW `set_temperature(target=0)` is blocked mid-print while `cooldown` is not — the "heater off is safety-positive" rule is applied inconsistently
qidi_mcp/tools/thermal.py:96 `risk = Risk.LOW if target <= 0 else Risk.THERMAL` (comment: "Turning a heater OFF is a
safety-positive act: allow without confirm") but line 105 still passes `Interlocks(forbid_during_print=True)`.
Why it matters: an agent that sees smoke from the chamber heater and calls `set_temperature heater=chamber target=0`
during a print gets `interlock_blocked` and must know to call `cooldown` (all heaters) or `emergency_stop` instead. Not a
bypass, but a refusal on the safe path costs seconds in the one scenario where seconds matter.
Proof (/tmp/qidi_audit/t7_off.py): `printing: set_temperature extruder 0 -> interlock_blocked | target still 250`;
`printing: cooldown -> True | targets 0.0 0.0 0.0`.
Suggested fix: when `target <= 0`, pass `Interlocks()` (klippy-ready only) so a single heater can always be turned off.
Also verified: read-only mode blocks `cooldown` and `set_temperature 0` but `emergency_stop` still works — acceptable
because e-stop is always reachable, though allowing heater-off in read-only would be strictly safer.

---
## Summary of scratch scripts (all loopback-only; socket guard active; no repo files modified)
/tmp/qidi_audit/harness.py, t1_stale.py, t1b_stale_realistic.py, t1c.py, t2_interlocks.py, t2b.py, t2c_oddone.py,
t3_fallback.py, t3b.py, t4_caps.py, t4b_caps.py, t5_concurrency.py, t5b.py, t5c.py, t6_pos.py, t7_off.py
