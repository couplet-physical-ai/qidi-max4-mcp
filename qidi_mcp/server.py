"""MCP server entry point.

Runs over stdio, which is what an MCP client's server config launches. Startup
is deliberately *non-blocking on the printer*: the server registers its tools
and starts serving even if the printer is unreachable, so an agent gets a clear
"cannot reach the printer" error from a tool call rather than a server that
refuses to start. Capability discovery happens lazily on first use.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import Settings, load_settings
from .errors import QidiError
from .policy import Risk
from .runtime import Runtime

log = logging.getLogger("qidi_mcp")

INSTRUCTIONS = """\
Control a QIDI Max4 3D printer (Klipper/Moonraker) safely.

Orientation, in order:
  1. `get_capabilities` — what this specific machine has (heaters, fans, sensors,
     features, QIDI-specific modules). Never assume a feature exists.
  2. `get_printer_status` — the complete current picture in one call.
  3. `get_safety_policy` — the limits being enforced, and why a call was refused.

Safety model you must expect:
  * Read tools are always free.
  * Motion, thermal and print tools need `confirm=true`.
  * Destructive tools (delete, config writes, restarts, calibration, print start,
    print cancel, filament load/unload) return a `confirm_token` on the first
    call; re-call the same tool with that token, and identical arguments, to
    proceed. The token is single-use and expires.
  * `emergency_stop` is never gated. Use it immediately if something is wrong.

Practical notes for this machine:
  * The chamber is actively heated (max 65 °C by policy) — that is what makes
    ABS/ASA/PA work. See `chamber_heater`.
  * The hotend cannot extrude below 135 °C; heat it first.
  * Filament load is a 3-stage sequence (`load_filament` heat → push → purge),
    matching what the printer's own screen does.
  * A print in progress blocks motion and thermal changes by default. `set_z_offset`,
    `set_speed_factor` and `set_flow_factor` are the exceptions, and are the right
    way to fix a first layer live.
  * Use `get_webcam_snapshot` to actually look at the print before concluding
    anything about quality.
  * A QIDI Box (four-spool switcher) may be fitted. `get_box_status` says whether it
    is genuinely usable and, if not, which condition is failing. `load_box_slot` and
    `set_box_option` have never been run against real hardware: read the ticket's
    warnings, and see docs/guides/box-first-contact.md before relying on them.
  * `run_gcode` exists for anything not covered, but the dedicated tools add range
    checks and interlocks that raw G-code does not.
"""


#: Tools that must remain callable even in read-only mode. An emergency stop is
#: a safety device: a configuration flag must never be what prevents one.
ALWAYS_ALLOWED = frozenset({"emergency_stop"})


def _shield_read_only(server: MCPServer, rt: Runtime) -> None:
    """Refuse mutating tools in read-only mode before they touch the network.

    The policy layer already refuses them, but only once a tool has run far
    enough to call ``authorize`` — and most tools legitimately query
    capabilities first. That means a read-only server pointed at an unreachable
    printer would answer "cannot reach printer" when the real reason is
    "this server cannot do that at all", which misleads the agent about what to
    try next.

    Wrapping registration lets the declared annotations do the work: anything not
    marked read-only is shielded, with no per-tool bookkeeping to forget.
    """
    original = server.tool

    def tool(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        annotations = kwargs.get("annotations")
        name = kwargs.get("name")
        mutating = bool(annotations) and not getattr(annotations, "read_only_hint", False)
        decorator = original(*args, **kwargs)

        if not (mutating and name not in ALWAYS_ALLOWED):
            return decorator

        def wrap(fn):  # type: ignore[no-untyped-def]
            @functools.wraps(fn)
            async def guarded_for_read_only(*a: Any, **kw: Any) -> Any:
                if rt.settings.read_only:
                    rt.policy.audit.record(
                        str(name), Risk.LOW, kw, "refused", "read_only_mode"
                    )
                    return {
                        "ok": False,
                        "error": "read_only_mode",
                        "message": (
                            f"{name} changes printer state, but this server is running in "
                            "read-only mode."
                        ),
                        "hint": (
                            "Set QIDI_READ_ONLY=0 in the MCP server's env and restart it to "
                            "allow control. Read tools and emergency_stop work regardless."
                        ),
                    }
                return await fn(*a, **kw)

            return decorator(guarded_for_read_only)

        return wrap

    server.tool = tool  # type: ignore[method-assign]


def build_server(
    settings: Settings | None = None,
    *,
    middleware: Sequence[Any] | None = None,
) -> tuple[MCPServer, Runtime]:
    """Construct the server and its runtime without starting a transport.

    ``middleware`` is how a shared deployment injects caller identity: it wraps every
    inbound request, so the 101 tools need no signature change to become
    identity-aware. The stdio path passes none -- there is no edge to
    authenticate against on a laptop.
    """
    cfg = settings or load_settings()
    rt = Runtime(cfg)

    server = MCPServer(
        name="qidi",
        title="QIDI Max4 Printer",
        middleware=list(middleware) if middleware else None,
        # Read from package metadata so the version an MCP client sees can never
        # drift from the one that was released.
        version=__version__,
        instructions=INSTRUCTIONS,
        # Tools already return structured error payloads; duplicate-name warnings
        # would only be noise on a stdio transport.
        warn_on_duplicate_tools=True,
    )

    from . import tools  # imported here so a config error surfaces first

    _shield_read_only(server, rt)
    for module in tools.MODULES:
        module.register(server, rt)

    return server, rt


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("QIDI_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,  # stdout is the MCP transport; never write to it
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        server, rt = build_server()
    except QidiError as exc:
        # A misconfigured server must fail loudly and legibly, not silently serve
        # tools that can never work.
        print(f"qidi-mcp: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  hint: {exc.hint}", file=sys.stderr)
        raise SystemExit(2) from exc

    log.info(
        "qidi-mcp serving printer=%s read_only=%s strict_confirm=%s",
        rt.settings.base_url,
        rt.settings.read_only,
        rt.settings.strict_confirm,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
