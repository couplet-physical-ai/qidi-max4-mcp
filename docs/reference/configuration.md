# Configuration

All configuration is environment variables, set in the MCP server entry. There is
no config file to manage.

## Essential

| Variable | Purpose |
|---|---|
| `QIDI_PRINTER_URL` | **Required.** Moonraker base URL, e.g. `http://<printer-ip>:7125`. Port 7125 is added if you omit it. |
| `QIDI_READ_ONLY` | Set to `1` to refuse every mutating tool before any network call. The recommended starting point. |
| `QIDI_AUDIT_LOG` | Path for the JSON-lines audit trail. Unset means in-memory only. |
| `QIDI_LEASE_ENFORCE` | Refuse mutating actions while another agent holds the printer lease. Default `1`. |
| `QIDI_REQUIRE_LEASE_FOR_PRINT` | Require holding the lease before starting a print or releasing the queue. Default `1`. |
| `QIDI_LEASE_MINUTES` | Default lease duration when a caller does not say. Default `60`. |
| `QIDI_AGENT_NAME` | How you appear in the lease to other agents. Defaults to `user@host`. |
| `QIDI_LEASE_HOLDER_KEY` | Secret that makes successive server processes **one** lease holder, so a CLI that runs one process per command (or a restarted agent session) can keep and release its own lease. Unset means every process is its own holder. Give each concurrently-running agent its own key. |
| `QIDI_AUDIT_MAX_BYTES` | Rotate the audit log at this size. Default `5000000`; `0` disables rotation. |
| `QIDI_AUDIT_KEEP` | Rotated generations to keep (`audit.jsonl.1` … `.N`). Default `3`. |
| `QIDI_API_KEY` | Moonraker API key or bearer token. Not needed on a stock Max 4, which is unauthenticated on the LAN. |
| `QIDI_PRINTER_ID` | A short label for this printer, used only in audit-log entries. Default `max4`. |

## Identity and roles (optional, bring-your-own auth)

This server is single-user by default: one process, one laptop, one person, and
the printer's owner decides by setting `QIDI_READ_ONLY`. The `Role`/`RoleTable`
machinery in `qidi_mcp/identity.py` is still there and usable if you front this
server with your own authenticating reverse proxy (it reads a verified caller
from request headers you populate):

| Variable | Purpose |
|---|---|
| `QIDI_IDENTITY_ENFORCE` | Require a verified caller and apply roles. Default `0`, i.e. off. |
| `QIDI_OPERATORS` | Emails allowed to move, heat, print and upload. Comma- or space-separated, case-insensitive. |
| `QIDI_MAINTAINERS` | Emails additionally allowed irreversible work: delete, config writes, calibration, restarts. |

Three roles, and the default is the safe one:

| Role | May | Source |
|---|---|---|
| `observer` | read anything | every authenticated caller |
| `operator` | + move, heat, print, upload | `QIDI_OPERATORS` |
| `maintainer` | + irreversible actions | `QIDI_MAINTAINERS` |

> **`emergency_stop` is never role-gated.** Every authenticated caller can stop
> the machine, whatever their role — and an unidentified one can too. An escape
> hatch that checks a list before working is not an escape hatch; the person who
> can see something going wrong is whoever happens to be looking at it.

Identity is never taken from a tool argument. `resolve_caller()` reads headers
your proxy sets and, if an auth adapter you supply (a module exposing
`user_from_headers(headers)`; not shipped) is importable, verifies with it;
otherwise it returns `None` and `QIDI_IDENTITY_ENFORCE` has no effect — which is
why it is safe to leave this code in place for a plain stdio checkout.

## Safety envelope

Each of these can only make the envelope *tighter*. A value above the hardware
ceiling is clamped down to it; a non-finite or negative value is refused at load.

| Variable | Default |
|---|---|
| `QIDI_MAX_EXTRUDER_TEMP` | `extruder.max` |
| `QIDI_CONFIRM_EXTRUDER_TEMP` | `extruder.confirm_above` |
| `QIDI_MAX_BED_TEMP` | `bed.max` |
| `QIDI_CONFIRM_BED_TEMP` | `bed.confirm_above` |
| `QIDI_MAX_CHAMBER_TEMP` | `chamber.max` |
| `QIDI_CONFIRM_CHAMBER_TEMP` | `chamber.confirm_above` |
| `QIDI_MIN_JOG_Z` | `min_jog_z` |
| `QIDI_MAX_JOG_DISTANCE` | `max_jog_distance` |
| `QIDI_MAX_EXTRUDE_DISTANCE` | `max_extrude_distance` |
| `QIDI_MAX_FEEDRATE` | `max_move_feedrate` |
| `QIDI_MAX_VELOCITY` | `max_velocity` |
| `QIDI_MAX_ACCEL` | `max_accel` |
| `QIDI_MAX_SQUARE_CORNER_VELOCITY` | `max_square_corner_velocity` |

The defaults column shows the field a value is read from rather than a fixed
number, because `confirm_above` and the ceilings come from the hardware profile.
`get_safety_policy` reports the resolved values for a given machine.

## Behaviour

| Variable | Purpose |
|---|---|
| `QIDI_CONFIRM_TTL` | Token lifetime in seconds, bounded 5–3600. Default 120. |
| `QIDI_STRICT_CONFIRM` | Require a two-step token for *every* mutating tier, not just destructive ones. |
| `QIDI_ALLOW_DURING_PRINT` | Permit motion/thermal changes while a print is active. Default off. |
| `QIDI_ALLOW_DANGEROUS_GCODE` | Operator switch for deny-listed raw G-code (`SAVE_CONFIG`, `FORCE_MOVE`, …). Default `0`: refused even when the agent passes `allow_dangerous=true` with a valid token. With `1`, `run_gcode` still needs `allow_dangerous=true` and the two-step token. `run_macro` never runs them. |
| `QIDI_TEST_MODE` | Refuse to start against a non-loopback address. Used by the test suite and the mock quickstart; also a useful guard while developing. |

## Everything else

Timeouts, the webcam override, and the remaining switches.

| Variable | Default |
|---|---|
| `QIDI_WEBCAM_URL` | `—` (derived from the printer's own host) |
| `QIDI_BEARER_TOKEN` | `—` |
| `QIDI_HTTP_TIMEOUT` | `10.0` |
| `QIDI_LONG_TIMEOUT` | `120.0` |
| `QIDI_MAX_READ_BYTES` | `200_000` |
| `QIDI_MAX_UPLOAD_BYTES` | `300_000_000` |

## Proxy environment variables are ignored, deliberately

> **This one nearly sent printer commands to an external host.** `httpx`
> defaults to `trust_env=True`, so both the Moonraker client and the webcam
> client would honour `http_proxy`/`https_proxy`/`all_proxy`. On a development
> machine with all three exported (pointed at some unrelated proxy) and the
> printer's address not in `no_proxy`, the first live connection would not
> merely have failed — it would have sent heater targets, motion commands and
> print starts to an unrelated external service.
>
> Both clients now set `trust_env=False`. The printer is on your LAN; a proxy can
> only break or misdirect that connection. Reaching a printer through a proxy is
> deliberately a code change, not an env var.

## Worked example

A conservative setup for a shared printer — read-only off, but ceilings suited to
PLA/PETG only, a short token lifetime, and everything logged.

```json
{
  "qidi": {
    "type": "stdio",
    "command": "/path/to/qidi-max4-mcp/.venv/bin/python",
    "args": [
      "-m",
      "qidi_mcp.server"
    ],
    "cwd": "/path/to/qidi-max4-mcp",
    "env": {
      "QIDI_PRINTER_URL": "http://<printer-ip>:7125",
      "QIDI_MAX_EXTRUDER_TEMP": "260",
      "QIDI_MAX_BED_TEMP": "80",
      "QIDI_MAX_JOG_DISTANCE": "50",
      "QIDI_CONFIRM_TTL": "60",
      "QIDI_AUDIT_LOG": "/path/to/qidi-max4-mcp/audit.jsonl"
    }
  }
}
```

With those values a request for 300 °C is refused with `limit_exceeded` and the
reason, so the agent is told the ceiling rather than left to guess.
