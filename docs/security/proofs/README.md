# Proof scripts

The scripts the reviewers wrote to demonstrate each finding. They are kept
because a security claim you cannot re-run is a claim you have to take on faith.

Most were written to prove a bug that **has since been fixed**, so the expected
result today is the opposite of what the review reports: the script that once
showed `nan` reaching the wire should now show the guard refusing it. That
inversion is the regression check.

## Running them

From the repository root, first `uv sync --extra dev`. For proofs that need the
Klipper parser or QIDI's config (`L4_diff.py`, `L4_fuzz.py`, `L5_denylist.py`),
run `bash scripts/fetch_upstream_sources.sh` first. It downloads pinned GPLv3
sources into Git-ignored `research/upstream/`; see
[the source inventory](../../../research/upstream/README.md). Without them the
proof exits non-zero with a fetch instruction. Then:

```bash
uv run python docs/security/proofs/L2_nan.py
uv run python docs/security/proofs/L4_diff.py
```

They put the repo root on `sys.path` and drive either the safety layer directly
or the in-process mock Moonraker from `tests/mock_moonraker.py`. **None of them
contact a real printer** — `tests/conftest.py` installs a socket guard that
refuses non-loopback connections, and the server refuses to start pointed at a
non-loopback address in test mode.

## What each one probes

### The numeric envelope (`audit-limits.md`)

| Script | Probes |
| --- | --- |
| `L1_clamp.py` | Whether any env var can *raise* a hardware ceiling |
| `L2_nan.py` | NaN/inf against every numeric guard |
| `L2_wire.py` | Whether a bad value reaches the wire end to end, printing literal G-code |
| `L3_motion.py` | Axis bounds vs `printer.cfg`, especially the negative minimums |
| `L4_diff.py` | Our command-name extraction vs Klipper's real parser |
| `L4_fuzz.py` | The same, over ~320k generated single-line spellings (current deny-list) |
| `L5_denylist.py` | Deny-list completeness, plus a sweep of vendor macros for ones reaching a restricted primitive (each must be denied or value-checked; asserted by `tests/test_envelope_bypasses.py`), an informational list of macros that reach one only through another macro, (e) a parameter-flow trace of every stock macro parameter to the heater/limit/motion/offset primitive it reaches (0 uncovered, and 0 covered by the deny-list alone — which the operator switch would reopen — both asserted), (e2) every `for k in params` forwarding loop, with a probe that padded or quoted parameter names are refused (asserted), and (f) every `rename_existing` target denied (requires fetched sources) |
| `_realparser.py` | Harness that drives an optionally fetched upstream `GCodeDispatch` as the oracle |
| `_h.py` | Shared helpers (settings, mock wiring) |

### Confirmation tokens (`audit-policy.md`)

| Script | Probes |
| --- | --- |
| `t1_canon.py` | Argument canonicalisation — can two different calls hash alike |
| `t2_replay.py` | Token replay and reuse |
| `t3_conc.py` | Concurrent redemption of one token |
| `t4_expiry.py` | TTL boundaries and clock behaviour |
| `t5_audit.py` | Whether every decision reaches the audit log, including refusals |
| `t6_toctou_start_print.py` | Swapping file contents inside the confirmation window |
| `t7_branches.py` | Risk-tier routing per tool |
| `t8_e2e_types.py` | Type coercion across the MCP boundary |
| `t9_flood.py` | Ticket-store exhaustion |

## The oracle approach

`L4_diff.py` and `L4_fuzz.py` are the ones worth imitating. Rather than
reimplementing Klipper's parser in the test and comparing two transcriptions of
the same belief — which passes when both are wrong — they load the **optionally fetched upstream source** (the parser fetched by
`bash scripts/fetch_upstream_sources.sh` from the pinned Klipper v0.13.0 tag) and
use it as the oracle. The original private source's exact parser revision was
not recorded. A re-run with pinned v0.13.0 checked 320,424 spellings (current
deny-list, up from 271,344 before the safety-envelope-bypass fixes) with zero
name divergences and zero safety bypasses; the historical 154,371-case
transcript in `audit-limits.md` remains an earlier snapshot.

That is what caught the `N1 SAVE_CONFIG` bypass, and what supports the claim of
zero divergences across single-line inputs. Any future change to command screening
should be re-checked this way.
