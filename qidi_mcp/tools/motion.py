"""Motion: homing, absolute/relative moves, Z offset, manual extrusion, motors off.

Safety notes specific to this module:

*   Every coordinate is bounds-checked against the machine's travel *before*
    transmission, and Z has an additional configurable floor so an agent cannot
    drive the nozzle into the bed.
*   Moves require homed axes. Klipper would refuse anyway, but refusing here
    produces a useful message instead of a G-code error buried in the console.
*   Relative moves are converted to absolute by reading the current position and
    bounds-checking the *destination*. A relative move that Klipper would clip
    is refused with the computed target shown.
*   Every move restores the prior absolute/relative mode afterwards, so calling
    these tools mid-file cannot corrupt a running print's coordinate state.
*   Manual extrusion requires the hotend to be at or above ``min_extrude_temp``
    (135 °C on the Max4) — cold extrusion strips filament and can break a drive
    gear.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import InvalidRequest
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

MOTION_ANN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

_AXES = ("x", "y", "z")


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="home_axes",
        description=(
            "Home the printer. Omit axes to home all of them (G28), or pass e.g. 'xy' to "
            "home a subset. On the Max4, homing Z runs the inductive probe against the bed. "
            "Requires confirm=true. Blocked during a print."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def home_axes(
        axes: str | None = None, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        requested = (axes or "").strip().lower().replace(",", "").replace(" ", "")
        if requested and any(a not in _AXES for a in requested):
            raise InvalidRequest(
                f"axes must be a subset of 'xyz', got {axes!r}.",
                hint="Examples: 'xy', 'z', or omit for all axes.",
            )
        args = {"axes": requested or "xyz"}
        ticket = await rt.authorize(
            "home_axes",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket

        script = "G28" if not requested else "G28 " + " ".join(a.upper() for a in requested)
        await rt.client.gcode(script, timeout=max(rt.settings.long_timeout, 180.0))
        state = await rt.state(fresh=True)
        return rt.ok(
            "home_axes",
            Risk.MOTION,
            args,
            {
                "command_sent": script,
                "homed_axes": state.homed_axes,
                "position": list(state.position[:3]),
            },
        )

    @server.tool(
        name="move_absolute",
        description=(
            "Move the toolhead to absolute coordinates in mm. Supply any of x/y/z; omitted "
            "axes hold position. feedrate is mm/min (capped by policy). Axes must be homed "
            "and each coordinate must be inside the machine's travel. Requires confirm=true. "
            "Blocked during a print."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def move_absolute(
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        feedrate: float = 3000.0,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        targets = {"x": x, "y": y, "z": z}
        given = {k: v for k, v in targets.items() if v is not None}
        if not given:
            raise InvalidRequest("Give at least one of x, y, z.")
        for axis, value in given.items():
            rt.policy.check_position(axis, float(value))
        speed = rt.policy.check_feedrate(feedrate)

        args = {**given, "feedrate": speed}
        ticket = await rt.authorize(
            "move_absolute",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(
                forbid_during_print=True, require_homed=tuple(given.keys())
            ),
        )
        if ticket is not None:
            return ticket

        coords = " ".join(f"{a.upper()}{float(v):.3f}" for a, v in given.items())
        # G90 then restore whatever mode was active, so a paused print is unharmed.
        script = f"SAVE_GCODE_STATE NAME=_qidi_mcp_move\nG90\nG1 {coords} F{speed:.0f}\nM400\nRESTORE_GCODE_STATE NAME=_qidi_mcp_move"
        await rt.client.gcode(script)
        state = await rt.state(fresh=True)
        return rt.ok(
            "move_absolute",
            Risk.MOTION,
            args,
            {"moved_to": given, "feedrate_mm_min": speed, "position": list(state.position[:3])},
        )

    @server.tool(
        name="move_relative",
        description=(
            "Jog the toolhead by a relative offset in mm (any of dx/dy/dz). The destination "
            "is computed and bounds-checked before moving, and a single jog is capped by "
            "policy. Requires confirm=true. Blocked during a print."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def move_relative(
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        feedrate: float = 3000.0,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        deltas = {"x": float(dx), "y": float(dy), "z": float(dz)}
        moving = {k: v for k, v in deltas.items() if v != 0.0}
        if not moving:
            raise InvalidRequest("Give a non-zero dx, dy, or dz.")
        for axis, delta in moving.items():
            rt.policy.check_jog(axis, delta)
        speed = rt.policy.check_feedrate(feedrate)

        state = await rt.state(fresh=True)
        current = {"x": state.position[0], "y": state.position[1], "z": state.position[2]}
        destination = {a: current[a] + d for a, d in moving.items()}
        for axis, value in destination.items():
            try:
                rt.policy.check_position(axis, value)
            except Exception as exc:  # re-raise with the jog context attached
                if hasattr(exc, "extra"):
                    exc.extra["current_position"] = current  # type: ignore[attr-defined]
                    exc.extra["computed_destination"] = destination  # type: ignore[attr-defined]
                raise

        args = {**{f"d{k}": v for k, v in moving.items()}, "feedrate": speed}
        ticket = await rt.authorize(
            "move_relative",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(
                forbid_during_print=True, require_homed=tuple(moving.keys())
            ),
        )
        if ticket is not None:
            ticket["computed_destination"] = destination
            return ticket

        coords = " ".join(f"{a.upper()}{v:.3f}" for a, v in moving.items())
        script = (
            "SAVE_GCODE_STATE NAME=_qidi_mcp_jog\nG91\n"
            f"G1 {coords} F{speed:.0f}\nM400\nRESTORE_GCODE_STATE NAME=_qidi_mcp_jog"
        )
        await rt.client.gcode(script)
        new_state = await rt.state(fresh=True)
        return rt.ok(
            "move_relative",
            Risk.MOTION,
            args,
            {
                "jogged": moving,
                "from": current,
                "to": destination,
                "position": list(new_state.position[:3]),
            },
        )

    @server.tool(
        name="set_z_offset",
        description=(
            "Adjust the live Z offset (baby-stepping), in mm — positive raises the nozzle. "
            "Use mode='adjust' to nudge relative to the current offset, or mode='set' for an "
            "absolute value. Applies immediately, including mid-print, which is the usual "
            "way to fix first-layer squish. Not persistent across restarts."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def set_z_offset(
        offset: float,
        mode: str = "adjust",
        move: bool = True,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        mode_l = mode.strip().lower()
        if mode_l not in {"adjust", "set"}:
            raise InvalidRequest("mode must be 'adjust' or 'set'.")
        rt.policy.require_finite("z offset", offset)
        if abs(offset) > 5.0:
            raise InvalidRequest(
                f"A Z offset change of {offset} mm is implausible; the cap is ±5 mm.",
                hint="Baby-stepping is normally a few hundredths of a millimetre.",
            )
        args = {"offset": offset, "mode": mode_l, "move": move}
        # Deliberately allowed during a print: this is the standard live fix.
        ticket = await rt.authorize(
            "set_z_offset",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            return ticket

        param = "Z_ADJUST" if mode_l == "adjust" else "Z"
        script = f"SET_GCODE_OFFSET {param}={offset:.4f}" + (" MOVE=1" if move else "")
        await rt.client.gcode(script)
        status = await rt.client.query({"gcode_move": ["homing_origin"]})
        origin = (status.get("gcode_move", {}) or {}).get("homing_origin") or [0, 0, 0, 0]
        return rt.ok(
            "set_z_offset",
            Risk.MOTION,
            args,
            {"command_sent": script, "z_offset_now_mm": origin[2]},
        )

    @server.tool(
        name="extrude_filament",
        description=(
            "Manually extrude (positive) or retract (negative) filament, in mm. The hotend "
            "must already be at or above Klipper's min_extrude_temp (135 °C on the Max4) — "
            "heat it with set_temperature first. Capped per call by policy. Requires "
            "confirm=true. Blocked during a print."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def extrude_filament(
        distance: float,
        feedrate: float = 300.0,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        if distance == 0:
            raise InvalidRequest("distance must be non-zero.")
        rt.policy.check_extrude(distance)
        # require_finite first: NaN fails BOTH comparisons below (every comparison
        # against NaN is False), so a bare range check silently approves it and
        # 'Fnan' ends up in the G-code.
        rt.policy.require_finite("extrusion feedrate", feedrate)
        if feedrate <= 0 or feedrate > 3000:
            raise InvalidRequest("feedrate for extrusion must be between 1 and 3000 mm/min.")

        args = {"distance": distance, "feedrate": feedrate}
        ticket = await rt.authorize(
            "extrude_filament",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True, require_extrude_temp=True),
        )
        if ticket is not None:
            return ticket

        script = (
            "SAVE_GCODE_STATE NAME=_qidi_mcp_extrude\nM83\n"
            f"G1 E{distance:.3f} F{feedrate:.0f}\nM400\n"
            "RESTORE_GCODE_STATE NAME=_qidi_mcp_extrude"
        )
        await rt.client.gcode(script, timeout=max(rt.settings.long_timeout, 120.0))
        return rt.ok(
            "extrude_filament",
            Risk.MOTION,
            args,
            {
                "action": "extruded" if distance > 0 else "retracted",
                "distance_mm": abs(distance),
                "command_sent": script,
            },
        )

    @server.tool(
        name="set_speed_factor",
        description=(
            "Set the print speed override (M220) as a percentage, 10–200. Applies live to a "
            "running print. Slowing a print that is having quality trouble is a common fix."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def set_speed_factor(
        percent: float, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        if not 10 <= percent <= 200:
            raise InvalidRequest("percent must be between 10 and 200.")
        args = {"percent": percent}
        ticket = await rt.authorize(
            "set_speed_factor",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            return ticket
        await rt.client.gcode(f"M220 S{percent:.0f}")
        return rt.ok("set_speed_factor", Risk.MOTION, args, {"speed_factor_percent": percent})

    @server.tool(
        name="set_flow_factor",
        description=(
            "Set the extrusion flow override (M221) as a percentage, 50–150. Applies live. "
            "Use sparingly — flow changes mid-print show up as a visible band."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def set_flow_factor(
        percent: float, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        if not 50 <= percent <= 150:
            raise InvalidRequest("percent must be between 50 and 150.")
        args = {"percent": percent}
        ticket = await rt.authorize(
            "set_flow_factor",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            return ticket
        await rt.client.gcode(f"M221 S{percent:.0f}")
        return rt.ok("set_flow_factor", Risk.MOTION, args, {"extrude_factor_percent": percent})

    @server.tool(
        name="motors_off",
        description=(
            "Disable the stepper motors (M84). The toolhead can then be moved by hand, but "
            "the printer loses its homed position and must be re-homed. Refused while a "
            "print is running — it would abandon the job mid-air."
        ),
        annotations=MOTION_ANN,
    )
    @guarded
    async def motors_off(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "motors_off",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket
        await rt.client.gcode("M84")
        return rt.ok(
            "motors_off",
            Risk.MOTION,
            args,
            {"note": "Steppers disabled; the printer is no longer homed."},
        )

    _ = (
        home_axes,
        move_absolute,
        move_relative,
        set_z_offset,
        extrude_filament,
        set_speed_factor,
        set_flow_factor,
        motors_off,
    )
