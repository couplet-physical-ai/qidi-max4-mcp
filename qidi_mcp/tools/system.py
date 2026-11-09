"""System-level control: e-stop, restarts, services, host power, raw G-code, logs.

This is the module with the sharpest edges, so the gating is strictest:

*   ``emergency_stop`` is the one tool that is **never** gated — not by
    confirmation, not by read-only mode, not by interlocks. If an agent (or a
    human reading its output) decides the machine must stop, nothing this server
    does may stand in the way. It is also idempotent and safe to spam.
*   ``run_gcode`` is the escape hatch that makes "expose everything" true. It is
    screened line by line against a deny-list (``SAVE_CONFIG``, ``FORCE_MOVE``,
    ``SET_KINEMATIC_POSITION``, TMC current writes, ``PID_CALIBRATE``, …). Pure
    queries pass with a single confirm; anything else needs the token; the
    deny-list additionally requires ``allow_dangerous=True`` from the agent AND
    ``QIDI_ALLOW_DANGEROUS_GCODE=1`` from the operator, and is logged loudly.
    Heater targets, kinematic limits and coordinates in the script are
    range-checked against the envelope. ``run_macro`` shares the same screen.
*   Reboot/shutdown of the *host* and service restarts are two-step, because
    pulling the OS out from under a running print corrupts the SD state.
*   ``RESTART`` vs ``FIRMWARE_RESTART`` is a distinction agents get wrong, so
    each tool spells out which one it is and what that means.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import InvalidRequest, QidiError, SafetyViolation
from ..limits import (
    ALWAYS_DENIED_GCODE_COMMANDS,
    CLASSIC_VALUE_RE,
    EXTENDED_KEY_RE,
    HEATER_ALLOWED_PARAMS,
    HEATER_COMMANDS,
    LIMIT_COMMANDS,
    MOTION_AXES,
    MOTION_COMMANDS,
    MOTION_MACROS,
    OFFSET_COMMANDS,
    PRINT_START_COMMANDS,
    GcodeScreen,
    box_temperature_params,
    classic_value_problem,
    command_params,
    is_box_firmware_command,
    is_illegal_script_char,
    is_traditional_gcode,
    klipper_extended_params,
    klipper_params,
    screen_gcode,
    split_gcode_script,
    table_key,
)
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)



def _normalise_heater(name: str) -> str:
    """Map a G-code heater name onto the safety-policy key.

    The policy knows "extruder"/"bed"/"chamber"; G-code and Klipper objects say
    "heater_bed" and "extruder1". Anything unrecognised is returned unchanged so
    check_temperature refuses it by name rather than silently checking the wrong
    ceiling -- guessing here would apply the extruder's 300 C limit to something
    that might be a bed.
    """
    key = name.strip().lower()
    if key in {"heater_bed", "bed"}:
        return "bed"
    if key in {"chamber", "heater_generic chamber"}:
        return "chamber"
    if key.startswith("extruder"):
        return "extruder"
    return key


#: Where QIDI's heater macros keep their live clamp (heaters.cfg).
_SCALING_OBJECT = "gcode_macro set_heater_scaling"
#: The policy's heater key -> the key QIDI's scaled_heaters dict uses.
_SCALING_KEY = {"extruder": "extruder", "bed": "heater_bed", "chamber": "chamber"}


#: A classic command's parameter name: Klipper's letter parser splits on these.
_CLASSIC_KEY_RE = re.compile(r"[A-Z_]+")


def _number(raw: str, value: str, what: str) -> float:
    # The value is read exactly as Klipper hands it on. Quotes are not stripped:
    # a macro's |float reads '"20"' as 0.0, so the screen must not read it as 20.
    # An empty value ("G27 Z", "PARK Z=") is refused for the same reason.
    try:
        return float(value.strip())
    except ValueError:
        raise InvalidRequest(
            f"Could not read the {what} in {raw!r}; refusing rather than sending a "
            "command whose value cannot be checked."
        ) from None


async def _live_scaling(rt: Runtime) -> dict[str, Any]:
    """The firmware's current heater scaling, or {} when it cannot be read.

    {} means "assume stock" (scale 1, bump 0, minimum 0), which every
    scaling-changing command being deny-listed is what keeps true. That is only
    assumed when the printer answers and has no scaling macro; a failed read
    fails closed, since the scaling it hides could multiply the checked target.
    """
    try:
        status = await rt.client.query({_SCALING_OBJECT: ["scaled_heaters"]})
    except QidiError as exc:
        raise SafetyViolation(
            "Could not read the printer's heater scaling, so the temperature the "
            "heater would actually receive cannot be checked; refusing.",
            hint=f"Retry once the printer answers status queries ({exc.message}).",
        ) from None
    scaled = (status.get(_SCALING_OBJECT) or {}).get("scaled_heaters")
    return scaled if isinstance(scaled, dict) else {}


def _scaled_target(target: float, scales: Any) -> float:
    """Mirror SET_HEATER_TEMPERATURE_SCALED: what the heater is actually set to."""
    if target <= 0 or not isinstance(scales, dict):
        return target
    try:
        scale = float(scales.get("scale", 1.0))
        bump = float(scales.get("bump", 0.0))
        lo = float(scales.get("minimum", 0.0))
        hi = float(scales.get("maximum", float("inf")))
    except (TypeError, ValueError):
        # A scaling entry we cannot read is one we cannot bound.
        return float("inf")
    return min(max((target + bump) * scale, lo), hi)


async def _screen_script(
    rt: Runtime, lines: list[str], *, allow_dangerous: bool
) -> list[GcodeScreen]:
    """The one screening path every raw G-code sender goes through.

    run_gcode and run_macro both end in a G-code script Klipper will execute, so
    both get the same checks, in this order:

    1. command names against the deny-list (:func:`screen_gcode`), plus every
       ``rename_existing`` target in the printer's own config;
    2. heater targets against the temperature envelope, including what QIDI's
       live heater scaling would turn them into;
    3. velocity/accel/square-corner-velocity changes against their ceilings;
    4. motion coordinates (G0-G3, PARK/G27/M600 X/Y/Z) against the axis
       envelope, and G-code offsets (SET_GCODE_OFFSET, M290) against their cap;
    5. deny-listed commands refused unless the agent passed allow_dangerous=true
       AND the operator started the server with QIDI_ALLOW_DANGEROUS_GCODE=1;
       ALWAYS_DENIED_GCODE_COMMANDS, which would defeat checks 2-4, never pass.

    Every extended-command line must also parse into KEY=value pairs with plain
    [A-Z0-9_] names, and the values of 2-4 are checked before 5, so a deny-listed
    macro's temperature is bounded even when the operator switch lets it through.

    Screening names alone was not enough: run_macro used to check only the
    macro name, so run_macro("M104", {"S": 374}) put ``M104 S=374`` on the wire.

    Tables are looked up by :func:`table_key`, so a decimal spelling of a G/M
    code (``M104.6245197``, the raw built-in behind QIDI's M104) is checked as
    its stem even when the operator switch lets the name itself through.
    """
    renamed: frozenset[str] = frozenset()
    # The stock targets are hard-coded in the deny-list either way.
    with contextlib.suppress(QidiError):
        renamed = frozenset((await rt.capabilities()).renamed_commands)
    screens = [screen_gcode(ln, extra_denied=renamed) for ln in lines]
    await rt.sync_limits()
    # G90/G91 are tracked line by line. The mode the printer is in when the
    # script starts is taken to be absolute, which is the stricter reading for
    # G0-G3 (every coordinate is held to the envelope until the script says G91).
    relative = False
    scaling: dict[str, Any] | None = None

    for raw, screen in zip(lines, screens, strict=True):
        key = table_key(screen.command)
        stem = key.split(".", 1)[0]
        if stem == "G91":
            relative = True
        elif stem == "G90":
            relative = False
        elif key == "RESTORE_GCODE_STATE":
            # Restores whichever mode was saved; assume absolute (checked).
            relative = False
        traditional = is_traditional_gcode(screen.command)
        if screen.command and not traditional and klipper_extended_params(raw) is None:
            # Klipper would refuse a line it cannot split into KEY=value pairs,
            # but a key it keeps verbatim (" HOTEND"=374) slips past a lookup of
            # HOTEND and is re-rendered as HOTEND=374 by macros that forward
            # their parameters. Either way, nothing unreadable is sent.
            raise InvalidRequest(
                f"Could not read the parameters of {raw!r}: every parameter must be "
                "KEY=value with a key of letters, digits and underscores only.",
                hint="Quoted or padded parameter names are refused.",
            )
        if traditional:
            problem = classic_value_problem(key, klipper_params(raw))
            if problem is not None:
                # Klipper keeps "=20" in G27 P1 Z=20 as the value; its own handlers
                # reject it, and the G27/M600 macros forward it to PARK, whose
                # |float reads it as 0.0. Refused instead of guessed.
                raise InvalidRequest(
                    f"Could not read {problem!r} in {raw!r}: a letter-and-number command "
                    "takes its values without '=', quotes or spaces (G27 P1 Z20, not "
                    "Z=20). Klipper would not read it as the number it appears to be.",
                    hint="Write the value straight after the letter, e.g. M104 S200.",
                )
        heater_spec = HEATER_COMMANDS.get(key)
        limit_spec = LIMIT_COMMANDS.get(key)
        offset_spec = OFFSET_COMMANDS.get(key)
        is_macro_motion = key in MOTION_MACROS
        motion_axes: tuple[str, ...] = (
            MOTION_AXES if key in MOTION_COMMANDS else MOTION_MACROS.get(key, ())
        )
        box = is_box_firmware_command(key)
        if (
            heater_spec is None
            and limit_spec is None
            and offset_spec is None
            and not motion_axes
            and not box
        ):
            continue
        params = command_params(raw)
        if params is None:
            raise InvalidRequest(
                f"Could not parse the parameters of {raw!r} the way Klipper would; "
                "refusing rather than sending a command whose values cannot be checked."
            )

        allowed = HEATER_ALLOWED_PARAMS.get(key)
        if allowed is not None:
            # Klipper's traditional parser keeps the command's own letter in the
            # parameters (M109 -> M="109"), and a line number as N; the inner
            # macro uses M to pick the raw built-in, so M may only repeat the
            # command's own number. A second M on the line wins, which is the attack.
            extra = sorted(set(params) - allowed - {"M", "N"})
            own = screen.command[1:]
            if params.get("M", own).strip() != own:
                extra.append("a second M")
            if extra:
                raise SafetyViolation(
                    f"{screen.command} does not take {', '.join(extra)}: its stock macro "
                    "forwards every parameter to an inner macro that would act on it "
                    "unchecked.",
                    hint=f"{key} accepts only {', '.join(sorted(allowed))}.",
                    refused_commands=[screen.command],
                )

        temperature_params: dict[str, str | None] = dict(heater_spec or {})
        for name, implied in box_temperature_params(key, params).items():
            if implied is None:
                raise SafetyViolation(
                    f"{screen.command} {name}= looks like a temperature, and this Box "
                    "command's parameters are not published, so there is no envelope "
                    "to check it against.",
                    hint="Use the printer's own screen for Box drying and heating.",
                    refused_commands=[screen.command],
                )
            temperature_params[name] = implied

        for name, implied in temperature_params.items():
            value = params.get(name)
            if value is None:
                continue
            target = _number(raw, value, "temperature")
            heater = _normalise_heater(implied or params.get("HEATER") or "")
            # Same envelope, same ceilings as set_temperature. Raises
            # LimitExceeded if out of range, SafetyViolation for an unknown heater.
            rt.policy.check_temperature(heater, target)
            if scaling is None:
                scaling = await _live_scaling(rt)
            effective = _scaled_target(target, scaling.get(_SCALING_KEY.get(heater, heater)))
            if effective != target:
                rt.policy.check_temperature(heater, effective)

        for name, kind in (limit_spec or {}).items():
            value = params.get(name)
            if value is None:
                continue
            rt.policy.check_kinematic_limit(
                kind, _number(raw, value, name), command=screen.command
            )

        for name in sorted(offset_spec or ()):
            value = params.get(name)
            if value is not None:
                rt.policy.check_gcode_offset(
                    name, _number(raw, value, name), command=screen.command
                )

        if motion_axes:
            # G0-G3 coordinates are only bounds-checkable in absolute mode; after
            # a G91 the same number is a delta, so only finiteness is enforced
            # until a G90. PARK/G27/M600/PAUSE switch to G90 themselves, so their
            # coordinates are always held to the envelope and the Z floor.
            for axis in motion_axes:
                value = params.get(axis)
                if value is None:
                    continue
                coord = _number(raw, value, f"{axis} coordinate")
                rt.policy.require_finite(f"{axis} in {screen.command}", coord)
                if is_macro_motion or not relative:
                    rt.policy.check_position(axis, coord)
            for extra in ("E", "F") if key in MOTION_COMMANDS else ():
                value = params.get(extra)
                if value is not None:
                    rt.policy.require_finite(
                        f"{extra} in {screen.command}", _number(raw, value, extra)
                    )

    locked = sorted(
        {
            s.matched or s.command
            for s in screens
            if s.is_dangerous and table_key(s.command) in ALWAYS_DENIED_GCODE_COMMANDS
        }
    )
    if locked and allow_dangerous and rt.settings.allow_dangerous_gcode:
        # Checked only once the operator switch would otherwise apply, so the
        # usual refusal (and its hint) is what an agent sees without it.
        raise SafetyViolation(
            "These commands are refused even with QIDI_ALLOW_DANGEROUS_GCODE=1: "
            + ", ".join(locked)
            + ". They rewrite the heater scaling or macro state the temperature "
            "checks rely on, or store a command to run later where it is never screened.",
            hint="Use the dedicated tools; there is no raw path for these.",
            refused_commands=locked,
        )

    dangerous = [s for s in screens if s.is_dangerous]
    if dangerous:
        names = sorted({s.matched or s.command for s in dangerous})
        if not allow_dangerous:
            raise SafetyViolation(
                "This script contains commands that bypass the printer's safety systems "
                "or rewrite its configuration: " + ", ".join(names) + ".",
                hint=(
                    ("Start a print with start_print, which claims the lease and checks "
                     "the file; resume one with resume_print. "
                     if set(names) & PRINT_START_COMMANDS else "")
                    + "Use the dedicated tool instead (e.g. bed_mesh_profile instead of "
                    "SAVE_CONFIG, pause_print instead of PAUSE). run_gcode accepts these "
                    "only with allow_dangerous=true on a server whose operator set "
                    "QIDI_ALLOW_DANGEROUS_GCODE=1."
                ),
                refused_commands=names,
            )
        if not rt.settings.allow_dangerous_gcode:
            # allow_dangerous is the agent's own argument, behind a token the agent
            # redeems itself. Without an operator-side switch it was a gate the
            # agent could open on its own. (@guarded records the refusal.)
            raise SafetyViolation(
                "Deny-listed commands are disabled on this server: " + ", ".join(names) + ".",
                hint=(
                    "allow_dangerous=true is not enough on its own. The operator must "
                    "start the server with QIDI_ALLOW_DANGEROUS_GCODE=1 to permit them."
                ),
                refused_commands=names,
            )
    return screens


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="emergency_stop",
        description=(
            "EMERGENCY STOP. Immediately halts the printer: all heaters off, all motion "
            "stopped, Klipper enters a shutdown state (this is M112). Any running print is "
            "lost. Never blocked — no confirmation, and it works even in read-only mode. Use "
            "it the moment something looks wrong. Recovery afterwards needs restart_firmware."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
    )
    @guarded
    async def emergency_stop(reason: str | None = None) -> dict[str, Any]:
        args = {"reason": reason or "not stated"}
        await rt.authorize("emergency_stop", Risk.EMERGENCY, args)
        await rt.client.emergency_stop()
        rt.invalidate_state()
        rt.policy.audit.record("emergency_stop", Risk.EMERGENCY, args, "ok", reason)
        return {
            "ok": True,
            "stopped": True,
            "reason": reason,
            "printer_state": "shutdown",
            "recovery": (
                "Klipper is now in a shutdown state and will refuse commands. Fix the physical "
                "cause first, then call restart_firmware to bring it back."
            ),
        }

    @server.tool(
        name="abort_heating",
        description=(
            "Abort a heater WAIT that is blocking the queue (QIDI's breakheater). If the "
            "printer is stuck in M109/M190/M191 waiting to reach a temperature it will never "
            "reach, this releases the wait without an emergency stop — so no firmware restart "
            "is needed afterwards. Heaters keep their targets; use cooldown to actually turn "
            "them off. QIDI-specific: not available on stock Klipper."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True
        ),
    )
    @guarded
    async def abort_heating() -> dict[str, Any]:
        args: dict[str, Any] = {}
        # Deliberately ungated beyond the audit trail: this is an abort. Making an
        # agent negotiate a token to escape a stuck wait would be perverse.
        await rt.authorize(
            "abort_heating",
            Risk.LOW,
            args,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        await rt.client.break_heater()
        rt.invalidate_state()
        return rt.ok(
            "abort_heating",
            Risk.LOW,
            args,
            {
                "note": (
                    "Heater wait released. Targets are unchanged — call cooldown to turn the "
                    "heaters off, or set_temperature to pick a reachable target."
                )
            },
        )

    @server.tool(
        name="abort_macro",
        description=(
            "Abort a running macro (QIDI's breakmacro). Use it when a macro is stuck or is "
            "doing something you want stopped — a levelling routine, a filament change, a "
            "purge — without the emergency stop's shutdown and firmware restart. The macro "
            "stops wherever it is, so the printer may be left mid-sequence (parked oddly, "
            "heaters on, filament part-loaded): check get_printer_status afterwards. "
            "QIDI-specific: not available on stock Klipper."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    @guarded
    async def abort_macro(confirm: bool = False) -> dict[str, Any]:
        args: dict[str, Any] = {}
        # One-step confirm, not a token: an abort must stay cheap to reach, but
        # it does leave the machine in an unknown state, so it is not free.
        await rt.authorize(
            "abort_macro",
            Risk.MOTION,
            args,
            confirm=confirm,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        await rt.client.break_macro()
        rt.invalidate_state()
        state = await rt.state(fresh=True)
        return rt.ok(
            "abort_macro",
            Risk.MOTION,
            args,
            {
                "printer_state": state.print_state,
                "klipper_state": state.klippy_state,
                "warning": (
                    "The macro stopped mid-sequence. The toolhead may be parked somewhere "
                    "unexpected and heaters may still be on — verify before continuing."
                ),
            },
        )

    @server.tool(
        name="resume_after_power_loss",
        description=(
            "Resume the print that was interrupted by a power loss (QIDI's power-loss "
            "recovery, 断电续打). Moonraker rebuilds recovery G-code from its saved state and "
            "STARTS PRINTING from where it stopped. Only meaningful when the printer's saved "
            "state says a print was interrupted — check get_saved_variables for "
            "'was_interrupted' first. Two-step confirmation, because it begins a real print. "
            "QIDI-specific: not available on stock Klipper."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def resume_after_power_loss(
        confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "resume_after_power_loss",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            # Tell the agent whether there is anything to resume, so it does not
            # confirm a recovery on a printer that was never interrupted.
            interrupted: Any = "unknown"
            try:
                status = await rt.client.query({"save_variables": None})
                variables = (status.get("save_variables") or {}).get("variables") or {}
                interrupted = variables.get("was_interrupted", "unknown")
            except QidiError:
                pass
            ticket["was_interrupted"] = interrupted
            ticket["what_will_happen"] = (
                "The printer heats up and resumes printing from the saved position. If "
                "was_interrupted is False, there is probably nothing to resume and this will "
                "do something unexpected — do not confirm."
            )
            return ticket
        await rt.client.power_loss_resume()
        rt.invalidate_state()
        return rt.ok(
            "resume_after_power_loss",
            Risk.DESTRUCTIVE,
            args,
            {"note": "Recovery G-code sent. Watch the first layer closely."},
        )

    @server.tool(
        name="restart_firmware",
        description=(
            "FIRMWARE_RESTART: reset the microcontrollers and reload Klipper's config. This "
            "is how you recover from a shutdown or an e-stop, and how config changes take "
            "effect. Loses homing and any in-memory tuning. Requires confirm=true; refuses "
            "while a print is running."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def restart_firmware(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "restart_firmware",
            Risk.PRINT,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            # Deliberately no klippy-ready requirement: this is the recovery path.
            interlocks=Interlocks(require_klippy_ready=False, forbid_during_print=True),
        )
        if ticket is not None:
            return ticket
        await rt.client.restart_firmware()
        rt.invalidate_state()
        return rt.ok(
            "restart_firmware",
            Risk.PRINT,
            args,
            {
                "note": (
                    "MCUs reset and config reloaded. The printer is unhomed; give it a few "
                    "seconds, then check get_printer_status."
                )
            },
        )

    @server.tool(
        name="restart_klipper_host",
        description=(
            "RESTART: restart the Klipper host process (not the MCUs, not the OS). Reloads "
            "config without resetting the microcontrollers — a lighter action than "
            "restart_firmware, but it still ends any print. Two-step confirmation."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def restart_klipper_host(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "restart_klipper_host",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False, forbid_during_print=True),
        )
        if ticket is not None:
            ticket["difference_from_firmware_restart"] = (
                "RESTART reloads the Klipper host process and config. FIRMWARE_RESTART also "
                "resets the MCUs and is what clears a shutdown state."
            )
            return ticket
        await rt.client.restart_host()
        rt.invalidate_state()
        return rt.ok("restart_klipper_host", Risk.DESTRUCTIVE, args, {"note": "Klipper host restarting."})

    @server.tool(
        name="run_gcode",
        description=(
            "Run raw G-code on the printer — the escape hatch for anything the dedicated "
            "tools do not cover. Every line is screened: pure queries (M105, QUERY_ENDSTOPS, "
            "DUMP_TMC…) run with no confirmation, anything that acts needs confirm=true or the two-step token, "
            "and commands that bypass safety systems or rewrite config (SAVE_CONFIG, "
            "FORCE_MOVE, SET_KINEMATIC_POSITION, SET_TMC_CURRENT, PID_CALIBRATE…) are refused "
            "unless allow_dangerous=true AND the operator started the server with "
            "QIDI_ALLOW_DANGEROUS_GCODE=1. Heater targets, velocity/accel limits and "
            "coordinates are range-checked against the safety envelope. Prefer the dedicated "
            "tools — they add interlocks this cannot."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    )
    @guarded
    async def run_gcode(
        script: str,
        allow_dangerous: bool = False,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        text = (script or "").strip()
        if not text:
            raise InvalidRequest("script is required.")
        try:
            lines = split_gcode_script(text)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        if len(lines) > 100:
            raise InvalidRequest("At most 100 G-code lines per call.")

        screens = await _screen_script(rt, lines, allow_dangerous=allow_dangerous)
        dangerous = [s for s in screens if s.is_dangerous]
        all_readonly = all(s.is_readonly for s in screens)

        args = {
            "script": text,
            "lines": len(lines),
            "allow_dangerous": allow_dangerous,
        }
        risk = Risk.READ if all_readonly else Risk.DESTRUCTIVE
        if all_readonly:
            # A pure query still goes through the gate for the audit trail, but
            # at MOTION tier it would be needlessly annoying; treat as LOW.
            ticket = await rt.authorize(
                "run_gcode",
                Risk.LOW,
                args,
                interlocks=Interlocks(require_klippy_ready=False),
            )
        else:
            # Raw G-code that ACTS must respect the same during-print interlock as
            # the dedicated tools. Without it, run_gcode was a trivial way to do
            # mid-print what move_absolute/set_temperature refuse to do, making
            # every other interlock advisory. Pure queries (above) stay unblocked.
            ticket = await rt.authorize(
                "run_gcode",
                risk,
                args,
                confirm=confirm,
                confirm_token=confirm_token,
                interlocks=Interlocks(
                    require_klippy_ready=False, forbid_during_print=True
                ),
            )
        if ticket is not None:
            ticket["screened_commands"] = [
                {
                    "command": s.command,
                    "read_only": s.is_readonly,
                    "flagged_dangerous": s.is_dangerous,
                }
                for s in screens
            ]
            return ticket

        await rt.client.gcode(text, timeout=max(rt.settings.long_timeout, 300.0))
        log = await rt.client.gcode_store(count=max(10, len(lines) * 3))
        entries = (log or {}).get("gcode_store", [])
        return rt.ok(
            "run_gcode",
            risk if not all_readonly else Risk.LOW,
            args,
            {
                "lines_sent": len(lines),
                "commands": [s.command for s in screens],
                "dangerous_commands_allowed": sorted({s.matched for s in dangerous if s.matched}),
                "recent_console": entries[-min(len(entries), 15) :],
            },
            detail=("DANGEROUS: " + text[:200]) if dangerous else text[:200],
        )

    @server.tool(
        name="run_macro",
        description=(
            "Run one of the printer's defined macros by name, with optional parameters. This "
            "is how you reach QIDI's own routines (e.g. M4029 bed levelling, CLEAR_NOZZLE, "
            "PRINT_START). Use list_gcode_commands to see what exists. Two-step confirmation, "
            "because a macro can do anything. The assembled command is screened exactly like "
            "run_gcode (deny-list, temperature and velocity/accel envelope); deny-listed "
            "macros are always refused here."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    )
    @guarded
    async def run_macro(
        macro: str,
        params: dict[str, Any] | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        name = (macro or "").strip().upper()
        if not name or " " in name:
            raise InvalidRequest("macro must be a single macro name, with params passed separately.")
        caps = await rt.capabilities()
        if not caps.has_gcode(name):
            close = [m for m in caps.macros if name in m or m in name][:8]
            raise InvalidRequest(
                f"This printer has no macro or command named {name!r}.",
                hint=(f"Close matches: {close}" if close else "Use list_gcode_commands to browse."),
            )

        parts = [name]
        traditional = is_traditional_gcode(name)
        seen: set[str] = set()
        for key, value in (params or {}).items():
            k = str(key).upper()
            # Letters, digits and underscores only, unpadded: a key like
            # " HOTEND" survives Klipper's parser verbatim and is turned back
            # into HOTEND by macros that re-render their parameters.
            if not EXTENDED_KEY_RE.fullmatch(k) or k in seen:
                raise InvalidRequest(
                    f"Invalid parameter name {key!r}: use letters, digits and "
                    "underscores only, each name once."
                )
            seen.add(k)
            v = str(value)
            # One line, as Klipper will split it: no newline, no other control or
            # line-separator character (see check_script_text), no comment start,
            # and no quote that could close the quoting added below.
            bad = next((ch for ch in v if ch in '\n\r;"' or is_illegal_script_char(ch)), None)
            if bad is not None:
                raise InvalidRequest(
                    f"Parameter {k} contains an illegal character (U+{ord(bad):04X})."
                )
            if traditional:
                # Klipper splits a letter-and-number command on its letters and
                # keeps whatever follows a name, '=' included: M104 S=220 hands
                # the macro "=220", which |float reads as 0. So the value goes
                # straight after the name (M104 S220), and must be a number.
                if not _CLASSIC_KEY_RE.fullmatch(k) or not CLASSIC_VALUE_RE.fullmatch(v):
                    raise InvalidRequest(
                        f"{name} is a letter-and-number command: parameter {k} needs a "
                        f"letter-only name and a plain number, not {v!r}."
                    )
                parts.append(f"{k}{v}")
            else:
                parts.append(f"{k}={v}" if " " not in v else f'{k}="{v}"')
        script = " ".join(parts)

        # The assembled line goes through exactly the screen run_gcode uses, so
        # a parameter is range-checked whether it arrives as raw text or as a
        # macro param. run_macro has no allow_dangerous: deny-listed names are
        # always refused here.
        await _screen_script(rt, [script], allow_dangerous=False)

        args = {"macro": name, "params": params or {}}
        ticket = await rt.authorize(
            "run_macro",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            # Same reasoning as run_gcode: a macro can move axes and heat, so it
            # must not be a way around the during-print interlock.
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["will_send"] = script
            ticket["why"] = (
                "A macro can move axes, heat, or start a print. Check the command above."
            )
            return ticket

        await rt.client.gcode(script, timeout=max(rt.settings.long_timeout, 600.0))
        log = await rt.client.gcode_store(count=20)
        return rt.ok(
            "run_macro",
            Risk.DESTRUCTIVE,
            args,
            {"command_sent": script, "recent_console": (log or {}).get("gcode_store", [])[-10:]},
            detail=script[:200],
        )

    @server.tool(
        name="get_klipper_log",
        description=(
            "Read the tail of klippy.log — the definitive place to find why Klipper shut "
            "down, what an MCU error was, or what a failed print reported. Truncated to a "
            "byte budget."
        ),
        annotations=READ,
    )
    @guarded
    async def get_klipper_log(max_bytes: int = 40_000, which: str = "klippy") -> dict[str, Any]:
        which_l = which.strip().lower()
        if which_l not in {"klippy", "moonraker"}:
            raise InvalidRequest("which must be 'klippy' or 'moonraker'.")
        budget = max(1_000, min(int(max_bytes), 400_000))
        path = f"/server/files/{which_l}.log"
        raw = await rt.client.request("GET", path, expect_json=False, timeout=rt.settings.long_timeout)
        blob = raw if isinstance(raw, bytes) else str(raw).encode()
        tail = blob[-budget:]
        return {
            "ok": True,
            "log": f"{which_l}.log",
            "total_bytes": len(blob),
            "bytes_returned": len(tail),
            "truncated": len(blob) > budget,
            "content": tail.decode("utf-8", errors="replace"),
        }

    @server.tool(
        name="restart_service",
        description=(
            "Restart, stop, or start a systemd service on the printer (klipper, moonraker, "
            "crowsnest, …). Two-step confirmation. Restarting klipper or moonraker will drop "
            "this connection briefly and ends any print."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def restart_service(
        service: str,
        action: str = "restart",
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        name = (service or "").strip()
        act = action.strip().lower()
        if act not in {"restart", "start", "stop"}:
            raise InvalidRequest("action must be 'restart', 'start', or 'stop'.")
        if not name or not all(c.isalnum() or c in "-_." for c in name):
            raise InvalidRequest(f"Invalid service name {service!r}.")

        allowed = await _allowed_services(rt)
        if allowed and name not in allowed:
            raise InvalidRequest(
                f"{name!r} is not a service Moonraker manages on this printer.",
                hint=f"Available: {sorted(allowed)}",
            )

        args = {"service": name, "action": act}
        ticket = await rt.authorize(
            "restart_service",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False, forbid_during_print=True),
        )
        if ticket is not None:
            ticket["warning"] = (
                "Restarting moonraker drops this MCP connection until it comes back; "
                "restarting klipper ends any print."
            )
            return ticket
        result = await rt.client.service_action(act, name)
        return rt.ok("restart_service", Risk.DESTRUCTIVE, args, {"result": result})

    @server.tool(
        name="reboot_printer",
        description=(
            "Reboot the printer's host computer (the whole OS, not just Klipper). Everything "
            "goes down for a minute or two. Two-step confirmation; refused during a print."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def reboot_printer(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "reboot_printer",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False, forbid_during_print=True),
        )
        if ticket is not None:
            ticket["warning"] = (
                "The printer's host OS reboots. Klipper, Moonraker, Fluidd and the camera all "
                "go away for a minute or two, and this MCP server will report connection "
                "failures until it returns."
            )
            return ticket
        await rt.client.machine_reboot()
        return rt.ok("reboot_printer", Risk.DESTRUCTIVE, args, {"note": "Host rebooting."})

    @server.tool(
        name="shutdown_printer",
        description=(
            "Shut down the printer's host computer. It will NOT come back until someone power-"
            "cycles the machine physically. Two-step confirmation; refused during a print."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def shutdown_printer(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "shutdown_printer",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False, forbid_during_print=True),
        )
        if ticket is not None:
            ticket["warning"] = (
                "This powers off the host OS. Nobody can restart it remotely — it needs "
                "physical access to the machine."
            )
            return ticket
        await rt.client.machine_shutdown()
        return rt.ok(
            "shutdown_printer",
            Risk.DESTRUCTIVE,
            args,
            {"note": "Host shutting down; physical power-cycle required to return."},
        )

    _ = (
        emergency_stop,
        abort_heating,
        abort_macro,
        resume_after_power_loss,
        restart_firmware,
        restart_klipper_host,
        run_gcode,
        run_macro,
        get_klipper_log,
        restart_service,
        reboot_printer,
        shutdown_printer,
    )


async def _allowed_services(rt: Runtime) -> set[str]:
    """Service names Moonraker reports as managed; empty set if unknown."""
    try:
        info = await rt.client.system_info() or {}
        services = ((info.get("system_info") or {}).get("available_services")) or []
        return {str(s) for s in services}
    except Exception:
        return set()
