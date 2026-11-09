"""Calibration: bed mesh, Z tilt, probe checks, endstops, bed screws, input shaper.

Safety notes specific to this module:

*   Calibration routines move the toolhead over the whole bed, take minutes, and
    several of them *overwrite saved values*. Anything that persists state is a
    two-step token; anything that merely measures is a single confirm.
*   ``BED_MESH_CALIBRATE`` and friends need a long HTTP timeout — a 9x9 mesh on a
    400 mm bed takes a while. Moonraker holds the request open until the G-code
    queue drains, so the timeout here is generous.
*   ``SAVE_CONFIG`` is *never* issued implicitly. It restarts Klipper and
    rewrites printer.cfg; if a routine's result needs saving, the tool says so
    and leaves the decision to a separate, explicit call.
*   Input-shaper calibration is the most invasive: it shakes the gantry hard at
    resonance. Two-step, and the ticket says what will happen.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import InvalidRequest, NotSupported
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
CAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
PERSIST = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)

#: Calibration can legitimately take many minutes.
_CAL_TIMEOUT = 900.0


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="get_bed_mesh",
        description=(
            "The active bed mesh and saved profiles: the probed Z deviation grid, its range, "
            "and the mesh area. Reveals whether the bed is warped and by how much."
        ),
        annotations=READ,
    )
    @guarded
    async def get_bed_mesh(include_matrix: bool = False) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_bed_mesh:
            raise NotSupported("This printer has no [bed_mesh] configured.")
        status = await rt.client.query({"bed_mesh": None})
        mesh = status.get("bed_mesh", {}) or {}
        matrix = mesh.get("probed_matrix") or []
        flat = [z for row in matrix for z in row if isinstance(z, (int, float))]
        payload: dict[str, Any] = {
            "ok": True,
            "active_profile": mesh.get("profile_name") or None,
            "saved_profiles": list((mesh.get("profiles") or {}).keys()),
            "mesh_min": mesh.get("mesh_min"),
            "mesh_max": mesh.get("mesh_max"),
            "grid_size": [len(matrix[0]) if matrix else 0, len(matrix)],
        }
        if flat:
            payload["deviation_mm"] = {
                "min": round(min(flat), 4),
                "max": round(max(flat), 4),
                "range": round(max(flat) - min(flat), 4),
                "mean": round(sum(flat) / len(flat), 4),
            }
        if include_matrix:
            payload["probed_matrix"] = matrix
            payload["mesh_matrix"] = mesh.get("mesh_matrix")
        else:
            payload["hint"] = "Pass include_matrix=true for the raw grid."
        return payload

    @server.tool(
        name="bed_mesh_calibrate",
        description=(
            "Probe the bed and build a new mesh. Takes several minutes on the Max4's 9x9 grid "
            "and moves the toolhead across the whole bed; the bed should be at printing "
            "temperature for a meaningful result. The mesh is active immediately but NOT "
            "saved to disk — use bed_mesh_profile to save it. Two-step confirmation."
        ),
        annotations=PERSIST,
    )
    @guarded
    async def bed_mesh_calibrate(
        profile: str | None = None,
        adaptive: bool = False,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_bed_mesh:
            raise NotSupported("This printer has no [bed_mesh] configured.")
        args = {"profile": profile, "adaptive": adaptive}
        ticket = await rt.authorize(
            "bed_mesh_calibrate",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["what_will_happen"] = (
                "The printer homes, then probes a grid across the bed (9x9 on a stock Max4), "
                "moving the toolhead the full travel. Expect several minutes. The new mesh "
                "replaces the active one in memory."
            )
            return ticket

        script = "BED_MESH_CALIBRATE"
        if profile:
            script += f" PROFILE={profile}"
        if adaptive:
            script += " ADAPTIVE=1"
        await rt.client.gcode(script, timeout=_CAL_TIMEOUT)
        status = await rt.client.query({"bed_mesh": None})
        mesh = status.get("bed_mesh", {}) or {}
        matrix = mesh.get("probed_matrix") or []
        flat = [z for row in matrix for z in row if isinstance(z, (int, float))]
        return rt.ok(
            "bed_mesh_calibrate",
            Risk.DESTRUCTIVE,
            args,
            {
                "command_sent": script,
                "active_profile": mesh.get("profile_name"),
                "deviation_mm": (
                    {
                        "min": round(min(flat), 4),
                        "max": round(max(flat), 4),
                        "range": round(max(flat) - min(flat), 4),
                    }
                    if flat
                    else None
                ),
                "note": (
                    "Mesh is active in memory only. Save it with bed_mesh_profile "
                    "action='save', which writes config and restarts Klipper."
                ),
            },
        )

    @server.tool(
        name="bed_mesh_profile",
        description=(
            "Manage bed mesh profiles: action='load' to apply a saved profile, 'save' to "
            "persist the current mesh (this writes printer.cfg and RESTARTS Klipper), "
            "'remove' to delete one, or 'clear' to drop the active mesh. Save/remove are "
            "two-step because they rewrite the config."
        ),
        annotations=PERSIST,
    )
    @guarded
    async def bed_mesh_profile(
        action: str,
        name: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        act = action.strip().lower()
        if act not in {"load", "save", "remove", "clear"}:
            raise InvalidRequest("action must be 'load', 'save', 'remove', or 'clear'.")
        if act != "clear" and not name:
            raise InvalidRequest(f"action='{act}' needs a profile name.")
        caps = await rt.capabilities()
        if not caps.has_bed_mesh:
            raise NotSupported("This printer has no [bed_mesh] configured.")

        args = {"action": act, "name": name}
        # load/clear are reversible; save/remove touch persistent config.
        risk = Risk.MOTION if act in {"load", "clear"} else Risk.DESTRUCTIVE
        ticket = await rt.authorize(
            "bed_mesh_profile",
            risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=(act != "load")),
        )
        if ticket is not None:
            if act == "save":
                ticket["what_will_happen"] = (
                    "BED_MESH_PROFILE SAVE writes the mesh into printer.cfg's autosave block "
                    "and Klipper restarts to reload it. Any unsaved config changes are also "
                    "committed."
                )
            elif act == "remove":
                ticket["what_will_happen"] = "The saved profile is deleted from printer.cfg."
            return ticket

        script = {
            "load": f"BED_MESH_PROFILE LOAD={name}",
            "save": f"BED_MESH_PROFILE SAVE={name}",
            "remove": f"BED_MESH_PROFILE REMOVE={name}",
            "clear": "BED_MESH_CLEAR",
        }[act]
        await rt.client.gcode(script, timeout=_CAL_TIMEOUT if act == "save" else None)
        return rt.ok(
            "bed_mesh_profile",
            risk,
            args,
            {
                "command_sent": script,
                "note": (
                    "Klipper restarts after a profile save; give it a few seconds."
                    if act == "save"
                    else None
                ),
            },
        )

    @server.tool(
        name="z_tilt_adjust",
        description=(
            "Level the Max4's two independent Z steppers against each other (Z_TILT_ADJUST). "
            "Probes the configured points and adjusts each Z motor until the gantry is "
            "square. Run this if first layers are thicker on one side. Requires confirm=true."
        ),
        annotations=CAL,
    )
    @guarded
    async def z_tilt_adjust(
        retries: int | None = None,
        retry_tolerance: float | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not (caps.has_z_tilt or caps.has_gcode("Z_TILT_ADJUST")):
            raise NotSupported("This printer has no [z_tilt] configured.")
        args = {"retries": retries, "retry_tolerance": retry_tolerance}
        ticket = await rt.authorize(
            "z_tilt_adjust",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket
        script = "Z_TILT_ADJUST"
        if retries is not None:
            script += f" RETRIES={int(retries)}"
        if retry_tolerance is not None:
            script += f" RETRY_TOLERANCE={float(retry_tolerance)}"
        await rt.client.gcode(script, timeout=_CAL_TIMEOUT)
        status = await rt.client.query({"z_tilt": None})
        return rt.ok(
            "z_tilt_adjust",
            Risk.MOTION,
            args,
            {"command_sent": script, "z_tilt": status.get("z_tilt")},
        )

    @server.tool(
        name="query_probe",
        description=(
            "Read the probe's current state (triggered or open) without moving anything. On "
            "the Max4 this is QIDI's inductive 'probe_air' sensor."
        ),
        annotations=READ,
    )
    @guarded
    async def query_probe() -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_probe:
            raise NotSupported("No probe is configured on this printer.")
        await rt.client.gcode("QUERY_PROBE")
        status = await rt.client.query({"probe": None})
        log = await rt.client.gcode_store(count=5)
        replies = [
            e.get("message")
            for e in (log or {}).get("gcode_store", [])
            if "probe" in str(e.get("message", "")).lower()
        ]
        return {
            "ok": True,
            "probe": status.get("probe"),
            "recent_probe_messages": replies[-3:],
        }

    @server.tool(
        name="probe_accuracy",
        description=(
            "Probe the same point repeatedly and report min/max/average/median/stddev. This "
            "is the diagnostic for 'is my probe repeatable?' — a stddev above ~0.01 mm means "
            "the probe or gantry has a mechanical problem. Moves Z only. Requires confirm=true."
        ),
        annotations=CAL,
    )
    @guarded
    async def probe_accuracy(
        samples: int = 10,
        probe_speed: float | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_probe:
            raise NotSupported("No probe is configured on this printer.")
        n = max(2, min(int(samples), 50))
        args = {"samples": n, "probe_speed": probe_speed}
        ticket = await rt.authorize(
            "probe_accuracy",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True, require_homed=("x", "y", "z")),
        )
        if ticket is not None:
            return ticket
        script = f"PROBE_ACCURACY SAMPLES={n}"
        if probe_speed is not None:
            script += f" PROBE_SPEED={float(probe_speed)}"
        await rt.client.gcode(script, timeout=_CAL_TIMEOUT)
        log = await rt.client.gcode_store(count=30)
        messages = [
            str(e.get("message", ""))
            for e in (log or {}).get("gcode_store", [])
            if "probe" in str(e.get("message", "")).lower()
        ]
        return rt.ok(
            "probe_accuracy",
            Risk.MOTION,
            args,
            {
                "command_sent": script,
                "results": messages[-6:],
                "hint": "Look for the 'probe accuracy results' line; stddev < 0.01 mm is good.",
            },
        )

    @server.tool(
        name="query_endstops",
        description=(
            "Read every endstop's triggered/open state without moving. Use this to diagnose "
            "a homing failure — a switch already reading 'TRIGGERED' at rest is the usual "
            "culprit."
        ),
        annotations=READ,
    )
    @guarded
    async def query_endstops() -> dict[str, Any]:
        result = await rt.client.get("/printer/query_endstops/status")
        return {"ok": True, "endstops": result}

    @server.tool(
        name="screws_tilt_calculate",
        description=(
            "Probe the bed's screw positions and report how far, and which way, to turn each "
            "levelling knob. The Max4 has four screws. Manual bed levelling aid — the "
            "printer only measures; you turn the knobs. Requires confirm=true."
        ),
        annotations=CAL,
    )
    @guarded
    async def screws_tilt_calculate(
        direction: str | None = None, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_gcode("SCREWS_TILT_CALCULATE"):
            raise NotSupported("This printer has no [screws_tilt_adjust] configured.")
        if direction and direction.upper() not in {"CW", "CCW"}:
            raise InvalidRequest("direction must be 'CW' or 'CCW'.")
        args = {"direction": direction}
        ticket = await rt.authorize(
            "screws_tilt_calculate",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket
        script = "SCREWS_TILT_CALCULATE"
        if direction:
            script += f" DIRECTION={direction.upper()}"
        await rt.client.gcode(script, timeout=_CAL_TIMEOUT)
        status = await rt.client.query({"screws_tilt_adjust": None})
        return rt.ok(
            "screws_tilt_calculate",
            Risk.MOTION,
            args,
            {"command_sent": script, "results": status.get("screws_tilt_adjust")},
        )

    @server.tool(
        name="input_shaper_calibrate",
        description=(
            "Run resonance testing with the toolhead accelerometer and compute optimal input "
            "shaper settings (SHAPER_CALIBRATE). The gantry is shaken hard through a "
            "frequency sweep — loud, and it must not be interrupted. Results are applied in "
            "memory; saving them rewrites printer.cfg. Two-step confirmation. Note: the Max4 "
            "also ships QIDI's own M901 macro for this."
        ),
        annotations=PERSIST,
    )
    @guarded
    async def input_shaper_calibrate(
        axis: str | None = None, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_resonance_tester:
            raise NotSupported(
                "This printer has no [resonance_tester]/accelerometer configured.",
                hint="The Max4 normally has a lis2dw in the toolhead.",
            )
        ax = (axis or "").strip().lower()
        if ax and ax not in {"x", "y"}:
            raise InvalidRequest("axis must be 'x' or 'y' (or omit for both).")
        args = {"axis": ax or "both"}
        ticket = await rt.authorize(
            "input_shaper_calibrate",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["what_will_happen"] = (
                "The toolhead is driven through a resonance sweep on each axis — this is loud "
                "and shakes the machine noticeably. Takes a few minutes per axis. New shaper "
                "values are applied in memory; they are lost on restart unless saved to "
                "config, which this tool does not do for you."
            )
            return ticket
        script = "SHAPER_CALIBRATE" + (f" AXIS={ax.upper()}" if ax else "")
        await rt.client.gcode(script, timeout=max(_CAL_TIMEOUT, 1800.0))
        status = await rt.client.query({"configfile": ["settings"]})
        shaper = ((status.get("configfile") or {}).get("settings") or {}).get("input_shaper")
        return rt.ok(
            "input_shaper_calibrate",
            Risk.DESTRUCTIVE,
            args,
            {
                "command_sent": script,
                "input_shaper_now": shaper,
                "note": (
                    "Values are in memory only. They are lost on restart unless written to "
                    "printer.cfg (a SAVE_CONFIG, which this server does not issue implicitly)."
                ),
            },
        )

    @server.tool(
        name="set_input_shaper",
        description=(
            "Set input shaper frequency/type directly, without running a calibration sweep. "
            "Useful to apply values you already know. Applies in memory only."
        ),
        annotations=CAL,
    )
    @guarded
    async def set_input_shaper(
        freq_x: float | None = None,
        freq_y: float | None = None,
        shaper_type_x: str | None = None,
        shaper_type_y: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        valid = {"zv", "mzv", "zvd", "ei", "2hump_ei", "3hump_ei", "smooth"}
        for label, val in (("shaper_type_x", shaper_type_x), ("shaper_type_y", shaper_type_y)):
            if val and val.strip().lower() not in valid:
                raise InvalidRequest(f"{label} must be one of {sorted(valid)}.")
        for label, val in (("freq_x", freq_x), ("freq_y", freq_y)):
            if val is not None and not 5.0 <= val <= 200.0:
                raise InvalidRequest(f"{label} must be between 5 and 200 Hz.")
        parts = []
        if freq_x is not None:
            parts.append(f"SHAPER_FREQ_X={freq_x:g}")
        if freq_y is not None:
            parts.append(f"SHAPER_FREQ_Y={freq_y:g}")
        if shaper_type_x:
            parts.append(f"SHAPER_TYPE_X={shaper_type_x.strip().lower()}")
        if shaper_type_y:
            parts.append(f"SHAPER_TYPE_Y={shaper_type_y.strip().lower()}")
        if not parts:
            raise InvalidRequest("Give at least one shaper parameter.")

        args = {
            "freq_x": freq_x,
            "freq_y": freq_y,
            "shaper_type_x": shaper_type_x,
            "shaper_type_y": shaper_type_y,
        }
        ticket = await rt.authorize(
            "set_input_shaper",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket
        script = "SET_INPUT_SHAPER " + " ".join(parts)
        await rt.client.gcode(script)
        return rt.ok("set_input_shaper", Risk.MOTION, args, {"command_sent": script})

    _ = (
        get_bed_mesh,
        bed_mesh_calibrate,
        bed_mesh_profile,
        z_tilt_adjust,
        query_probe,
        probe_accuracy,
        query_endstops,
        screws_tilt_calculate,
        input_shaper_calibrate,
        set_input_shaper,
    )
