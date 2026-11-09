# Audit: qidi_mcp/policy.py — confirmation-token mechanism

Scope: ConfirmationBroker (issue/redeem/fingerprint), SafetyPolicy.authorize() branches, AuditLog.
Method: every finding below was exercised with an executed script against the real code
(`.venv/bin/python`, repo root on sys.path). Scripts live in `docs/security/proofs/`.

Findings are appended as they are confirmed.

---
## VERIFIED-SOUND Single-use, cross-tool and cross-args token replay all refused
qidi_mcp/policy.py:118-131  |  `ticket = self._tickets.pop(token, None)` … `if ticket.tool != tool or ticket.fingerprint != self.fingerprint(tool, args): raise`
Why it matters on real hardware: a replayable or transferable token would let one "yes" to delete_file authorise reboot_printer, run_gcode M112, or a 350 °C hotend.
Proof (docs/security/proofs/t2_replay.py, actual output):
```
first redeem: CLEARED
replay: REFUSED(confirmation_required)
cross-tool (delete_file tok -> reboot_printer {}): REFUSED(...)
...and is the ticket burned after the mismatch? REFUSED(unknown, already used)   <- pop() happens BEFORE compare: a mismatched attempt burns the ticket (good, fail-closed)
diff root: REFUSED           run_gcode script swap: REFUSED
temp int->float 260 vs 260.0: REFUSED    temp 260 -> 350: REFUSED
MOTION w/ foreign token: confirmation_required   <- destructive token supplied to a MOTION tool is validated, not ignored
confirm-only destructive: TICKET   <- confirm=True without token does NOT clear a DESTRUCTIVE action
confirm-only + bogus token: REFUSED
```
Note: `tool` is part of both the fingerprint blob and a separate `ticket.tool` check (belt and braces). Token = `secrets.token_urlsafe(9)` = 72 bits of CSPRNG entropy; unguessable within a 120 s window.
Suggested fix: none required.

## VERIFIED-SOUND Redemption is atomic under concurrent asyncio tasks and OS threads
qidi_mcp/policy.py:120  |  `ticket = self._tickets.pop(token, None)`
Why it matters on real hardware: two coroutines redeeming one token would turn one confirmation into two destructive executions.
Proof (docs/security/proofs/t3_conc.py):
```
asyncio 4x redeem: ['CLEARED', 'confirmation_required', 'confirmation_required', 'confirmation_required']
threaded 8-way redeem x300, max successes/token: 1 min: 1
```
`dict.pop` is a single bytecode op under the GIL and the broker never awaits between pop and compare, so exactly-once holds. (Would NOT hold if the broker were ever moved to a multi-process/shared-store design — note for future maintainers.)
Suggested fix: none required.

## VERIFIED-SOUND Expiry is enforced at redeem time, not only by the sweep
qidi_mcp/policy.py:98-100, 118-119  |  `expired: monotonic() - created > ttl`; `redeem(): self._sweep(); pop(...)`
Why it matters on real hardware: a ticket that outlives its window could be redeemed minutes later after the printer's state (loaded file, temperature, homing) has changed.
Proof (docs/security/proofs/t4_expiry.py, monotonic clock patched):
```
t=119.999: CLEARED
t=120.000 (boundary, uses >): CLEARED      <- off-by-one inclusive boundary; 1 ms slack, not exploitable
t=120.001: REFUSED(confirmation_required)
expired ticket still in dict before any sweep: True
redeem after expiry, no intermediate sweep: REFUSED     <- _sweep() runs at the top of redeem(), so lingering entries are never redeemable
```
Suggested fix: none required (optionally `>=` for a strictly-less-than-TTL window).

## MEDIUM Failed token redemptions (unknown / replayed / mismatched-args) leave NO audit line
qidi_mcp/policy.py:118-131  |  `redeem()` raises `ConfirmationRequired` directly; neither branch calls `self.audit.record(...)`. In `authorize()` (policy.py:283-291) the two `self.broker.redeem(...)` call sites are likewise unlogged; only `ticket_issued`, `confirm_flag_missing`, `read_only_mode` and interlock refusals are recorded.
Why it matters on real hardware: an agent (or a compromised client) probing tokens, replaying a used token, or trying to bend a `delete_file a.gcode` ticket onto `delete_file printer.cfg` is exactly the behaviour an operator needs to see in the audit trail. Today it is invisible — the log shows a ticket issued and then silence, indistinguishable from "agent gave up".
Proof (docs/security/proofs/t5_audit.py):
```
after 5 bad-token attempts, audit tail: []
after mismatched redeem (token for path=a used with path=printer.cfg): [('ticket_issued', 'qidi-Gok1uT_ZhD7t')]   <- no 'refused' entry
```
Suggested fix: in `authorize()` wrap both `redeem()` calls: `except ConfirmationRequired: self.audit.record(tool, risk, args, "refused", "token_rejected"); raise`. Also record `"token_redeemed"` on success so the issue→redeem pair is reconstructable.

## LOW Live confirmation tokens are written in plaintext to the audit log
qidi_mcp/policy.py:297  |  `self.audit.record(tool, risk, args, "ticket_issued", ticket.token)`  vs.  `_redact()` (policy.py:236) which redacts any *arg key* containing "token".
Why it matters on real hardware: the redaction logic shows intent to keep tokens out of the log, but the `detail` field is not redacted. Anyone/anything that can read `QIDI_AUDIT_LOG` (or call the `get_audit_log` status tool — status.py:586 returns `audit.tail()` verbatim, i.e. a READ tool with no gate) can lift a still-valid token and redeem the destructive action within the 120 s window, without having seen the `arguments_locked_to` / `about_to_print` payload the ticket was meant to force a review of.
Proof (docs/security/proofs/t5_audit.py):
```
ticket_issued detail field on disk: qidi-4RWpRbJ1Zjot | redeemable? -> True
args redaction check: {'confirm_token': '<redacted>'}     <- args ARE redacted; detail is not
```
Suggested fix: log a short non-reversible prefix or hash (`ticket.token[:9] + "…"` or sha256 hex[:12]) in `detail`; redeem() can log the same digest so issue and redeem still correlate.

## LOW Degenerate QIDI_CONFIRM_TTL values are accepted; `inf`/`nan` crash mid-issue and leave an immortal ticket
qidi_mcp/policy.py:106-111, 299  |  `Ticket(ttl=self.ttl)` … `"expires_in_seconds": int(ticket.ttl)`  — `confirm_ttl` comes straight from `float(env)` (config.py:44-53, 204) with no bounds check.
Why it matters on real hardware: `QIDI_CONFIRM_TTL=inf` (or `1e308`) makes every destructive ticket valid forever, defeating the whole "short-lived" property; a ticket issued during a review can be redeemed hours later on a printer in a different state. With `inf`/`nan` the `int()` at line 299 raises *after* the ticket has been stored and its token written to the audit log, so the ticket exists and never expires yet the caller gets an `internal_error`. `0`/negative makes every destructive tool permanently unusable (availability, not safety).
Proof (docs/security/proofs/t4_expiry.py, clock advanced 1 year between issue and redeem):
```
TTL=inf: issue -> OverflowError: cannot convert float infinity to integer; ticket count in broker: 1; audit tail: ['ticket_issued']
TTL=nan: issue -> ValueError: cannot convert float NaN to integer; ticket count in broker: 1; audit tail: ['ticket_issued']
TTL=1e308: issue ok ..., redeem after 1 year: CLEARED
TTL=0: redeem -> REFUSED (dead on arrival)    TTL=-5: same
```
Suggested fix: in `load_settings` clamp `confirm_ttl` to a finite range (e.g. 5 ≤ ttl ≤ 3600) and reject non-finite values; in `ConfirmationBroker.__init__` assert `math.isfinite(ttl) and ttl > 0`.

## HIGH start_print ticket fingerprint binds only the *filename*; file contents can be swapped by an unconfirmed LOW-tier upload/move/copy between issue and redeem (TOCTOU)
qidi_mcp/policy.py:105-111 `fingerprint()` hashes only the `args` the tool passes; tools/jobs.py:50 `args = {"filename": name}`; tools/files.py:263/371/398 `upload_gcode` (with `overwrite=True`), `move_file`, `copy_file` are `Risk.LOW` → no confirm, no ticket.
Why it matters on real hardware: the two-step ticket for start_print exists so the agent reviews the job (`about_to_print` metadata: temps, time, filament) and then commits to *that* job. But the ticket is keyed on the name string, not on the content (size/modified/checksum). Within the 120 s window an unconfirmed `upload_gcode(overwrite=True)` / `move_file` / `copy_file` can replace `benign.gcode` with arbitrary G-code (M104 S350, bed 120 °C, Z-5 plunge, SAVE_CONFIG…) and the pre-issued token still redeems. Confirmation was given for 15 bytes of "G28 / M117 hello"; 330 bytes of hostile G-code printed. run_gcode's screen_gcode dangerous-command filter is not applied to file contents, so this is also the cheapest bypass of that filter. Realistic path: prompt-injected agent, or two agents/clients sharing one server.
Proof (docs/security/proofs/t6_toctou_start_print.py, against the in-repo mock Moonraker through the real MCP server):
```
upload benign: True | size 15
ticket: confirmation_required {'filename': 'benign.gcode'} size_bytes shown: 15
overwrite w/ hostile content, no confirm needed: True | replaced_existing: True | size 330
redeem: {'ok': True, 'started': 'benign.gcode', 'print_state': 'printing', ...}
MOCK PRINTER STATE: printing benign.gcode | file size now 330
audit tail: [('upload_gcode','low','ok'), ('start_print','destructive','ticket_issued'), ('upload_gcode','low','ok'), ('start_print','print','ok')]
```
Suggested fix (policy-level, generic): let tools pass a `binding` dict that is folded into the fingerprint but not into user-facing `arguments_locked_to` — for start_print include `meta["size"]`, `meta["modified"]` (or a content hash) fetched at BOTH issue and redeem. Cheaper alternative in jobs.py: include `size`/`modified` in `args`. Additionally, `upload_gcode(overwrite=True)`, `move_file`, `copy_file` onto an existing path should be at least `NEEDS_CONFIRM` tier — overwriting a print file is not "reversible, non-thermal, non-motion".

## VERIFIED-SOUND All authorize() branches: DESTRUCTIVE never clears on confirm=True alone; strict_confirm escalates MOTION/THERMAL/PRINT to tickets; read-only beats a valid token; EMERGENCY is never gated
qidi_mcp/policy.py:254-321
Proof (docs/security/proofs/t7_branches.py):
```
strict motion/thermal/print/destructive confirm=True no token -> TICKET (all four)      strict low -> CLEARED
strict MOTION redeem: CLEARED    strict MOTION replay: REFUSED
non-strict: low nothing->CLEARED | motion/thermal/print nothing->REFUSED(confirm_flag_missing) confirm->CLEARED | destructive nothing->TICKET confirm->TICKET
any tier + garbage token -> REFUSED   (token is validated even for LOW; never silently ignored)
read_only DESTRUCTIVE w/ token: REFUSED(read_only_mode) | EMERGENCY: CLEARED
```
Also verified: interlocks are re-evaluated at *redeem* time with live state (issued-homed → redeem-unhomed = REFUSED; redeem-while-printing = REFUSED). Because `_check_interlocks` runs before `redeem()`, an interlock refusal does not burn the ticket — it stays redeemable until TTL. That is acceptable (args are still locked) but note it interacts with the TOCTOU finding above.
Suggested fix: none required.

## LOW No audit line is written for a CLEARED gate decision inside authorize(); the tier logged on success can differ from the tier that was gated
qidi_mcp/policy.py:254-321 (authorize returns None silently); the only success record is `Runtime.ok()` (runtime.py:154) which logs whatever `risk` the tool hands it — e.g. tools/thermal.py:120 logs `risk` (THERMAL) although the gate ran at DESTRUCTIVE; tools/jobs.py:110 logs `Risk.PRINT` for a DESTRUCTIVE-gated start_print.
Why it matters on real hardware: the docstring promises "everything mutating is logged with the tier". If a tool crashes between `authorize()` and `rt.ok()` (network error after the G-code was sent, exception in response building) the action *happened* but no log line exists — the only trace is a `ticket_issued` line with no matching outcome. Auditing by tier ('destructive') also under-reports: t6 output shows `('start_print','print','ok')` for a ticketed action.
Proof (docs/security/proofs/t7_branches.py):
```
audit lines after 2 CLEARED confirm=True calls (home_axes MOTION, set_temperature THERMAL): []
```
and t6: `('start_print', 'destructive', 'ticket_issued') … ('start_print', 'print', 'ok')`.
Suggested fix: record `"cleared"` (with `detail="token_redeemed"` / `"confirm_flag"`) in `authorize()` before returning None, so every gate decision has a line regardless of what the tool does next; have `Runtime.ok()` log the gated tier (or both).

## VERIFIED-SOUND Argument canonicalisation: no type-confusion collisions that change the emitted command; end-to-end int/float behaviour is safe
qidi_mcp/policy.py:105-109  |  `json.dumps({"tool": tool, "args": material}, sort_keys=True, default=str)`
Tried (docs/security/proofs/t1_canon.py, t8_e2e_types.py):
```
int vs float ({"a":1} / {"a":1.0}): distinct     int vs str: distinct     bool vs int: distinct     None vs "None": distinct
dict order / nested dict order: COLLIDE (intended: sort_keys — same call, harmless)
1e2 vs 100.0: COLLIDE (same float value — harmless)
tuple vs list: COLLIDE ; set vs str(set), Enum vs str, bytes vs str: COLLIDE via default=str  -> only reachable if a tool put such objects in args; no tool does (all args are JSON scalars/dicts from the MCP layer)
extra key / case / path traversal: distinct
tool name in blob AND checked separately: distinct across tools
E2E: set_temperature issued target=300 (JSON int) -> pydantic coerces to 300.0 before the tool sees it; redeem with 300.0 works, and M104 S300 is what is sent. The value locked == value executed.
E2E: run_macro TARGET=250 vs TARGET="250" -> same G-code script, different fingerprint => REFUSED. Strict (fail-closed), not a bypass.
```
Hash collision: the "fingerprint" is the full canonical JSON string, not a digest, so there is nothing to collide — equality is structural. Sound.
Suggested fix: none required. (Optional hardening: `default=` should raise instead of `str` so an accidental non-JSON arg fails loudly rather than being flattened.)

## LOW Unbounded ticket store; `_sweep()` is O(n) on every issue/redeem → quadratic slowdown under a ticket flood
qidi_mcp/policy.py:98,113,134-136  |  `self._tickets: dict`; `_sweep()` iterates every ticket on every `issue()`/`redeem()`; nothing caps outstanding tickets per tool.
Why it matters on real hardware: not a safety bypass, but a misbehaving agent looping on a destructive tool without redeeming (common failure mode when a model doesn't understand the two-step) makes the gate itself progressively slower — which delays *every* gated call including recovery tools (restart_firmware etc.). Only emergency_stop bypasses the broker.
Proof (docs/security/proofs/t9_flood.py):
```
50k unredeemed tickets: 26126 live (TTL expired the rest mid-run), 9.2 MB, 165.40 s total  (~3.3 ms/issue, growing linearly with live tickets)
```
Suggested fix: keep one outstanding ticket per (tool, fingerprint) — re-issuing for identical args should return/replace the existing ticket — and/or cap the store (e.g. 256) evicting oldest; sweep lazily (only when size > cap or on a timer).

## VERIFIED-SOUND Token supplied to a non-ticket tool is consumed and validated, never ignored; a mismatched attempt burns the ticket (fail-closed)
qidi_mcp/policy.py:283-291  |  `if confirm_token and not needs_ticket: self.broker.redeem(confirm_token, tool, args)`
Proof: t2 "MOTION w/ foreign token: confirmation_required"; t7 "any tier + garbage token -> REFUSED"; t2 "...ticket burned after mismatch? REFUSED(unknown)". An attacker cannot use a mismatch as an oracle to probe args without destroying the ticket.
Suggested fix: none required.

---
## Summary
| Sev | Finding |
|---|---|
| HIGH | start_print (and by construction any file-referencing destructive tool) ticket binds the *name* only; LOW-tier upload/move/copy swaps the content inside the TTL window — confirmation reviewed one file, printer runs another. |
| MEDIUM | Rejected token redemptions (unknown/replayed/mismatched) are never audit-logged. |
| LOW | Live tokens written in plaintext to audit `detail` (readable via the ungated `get_audit_log` READ tool). |
| LOW | `QIDI_CONFIRM_TTL` unbounded: `inf`/`1e308` → immortal tickets; `inf`/`nan` crash after storing the ticket. |
| LOW | No audit line for a CLEARED gate; success tier logged by tools can differ from the gated tier. |
| LOW | Unbounded ticket store + O(n) sweep on every call. |
| SOUND | Single-use, cross-tool, cross-args replay; atomic redeem under asyncio/threads; expiry enforced at redeem; all authorize() tier branches; canonicalisation (no dangerous type collisions); read-only precedence; emergency ungated. |

Scripts: docs/security/proofs/t1_canon.py t2_replay.py t3_conc.py t4_expiry.py t5_audit.py t6_toctou_start_print.py t7_branches.py t8_e2e_types.py t9_flood.py (run from repo root or audit/scripts with `.venv/bin/python`).
No files outside audit/ were modified.
