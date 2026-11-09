"""Read-only tools: status, capabilities, temperatures, position, config, logs.

Nothing here mutates the printer, so nothing here is gated. These are the tools
an agent should reach for first, and the ones that remain available in
read-only mode.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .. import __version__
from ..client import DEFAULT_STATUS_OBJECTS
from ..errors import InvalidRequest
from ..policy import Risk
from ..runtime import Runtime, guarded

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="get_printer_status",
        description=(
            "Overall printer state: Klipper readiness, print job state and progress, "
            "temperatures, position, homed axes, and any active error message. "
            "Start here — this is the cheapest complete picture of the machine."
        ),
        annotations=READ,
    )
    @guarded
    async def get_printer_status() -> dict[str, Any]:
        status = await rt.client.query(DEFAULT_STATUS_OBJECTS)
        webhooks = status.get("webhooks", {}) or {}
        stats = status.get("print_stats", {}) or {}
        display = status.get("display_status", {}) or {}
        toolhead = status.get("toolhead", {}) or {}
        gcode_move = status.get("gcode_move", {}) or {}
        virtual_sd = status.get("virtual_sdcard", {}) or {}
        idle = status.get("idle_timeout", {}) or {}

        heaters: dict[str, Any] = {}
        for key, label in (
            ("extruder", "extruder"),
            ("heater_bed", "bed"),
            ("heater_generic chamber", "chamber"),
        ):
            obj = status.get(key)
            if obj:
                heaters[label] = {
                    "temperature": obj.get("temperature"),
                    "target": obj.get("target"),
                    "power": obj.get("power"),
                }

        sensors = {
            key.split(" ", 1)[1]: {"temperature": val.get("temperature")}
            for key, val in status.items()
            if key.startswith("temperature_sensor ") and isinstance(val, dict)
        }

        klippy_state = str(webhooks.get("state", "unknown"))
        payload: dict[str, Any] = {
            "ok": True,
            "klipper": {
                "state": klippy_state,
                "ready": klippy_state == "ready",
                "message": webhooks.get("state_message", ""),
            },
            "job": {
                "state": stats.get("state", "standby"),
                "filename": stats.get("filename") or None,
                "progress_percent": round(float(display.get("progress", 0.0) or 0.0) * 100, 1),
                "print_duration_s": stats.get("print_duration"),
                "total_duration_s": stats.get("total_duration"),
                "filament_used_mm": stats.get("filament_used"),
                "current_layer": (stats.get("info") or {}).get("current_layer"),
                "total_layers": (stats.get("info") or {}).get("total_layer"),
                "message": stats.get("message") or None,
                "file_position": virtual_sd.get("file_position"),
                "sd_progress_percent": (
                    round(float(virtual_sd.get("progress", 0.0) or 0.0) * 100, 1)
                    if virtual_sd
                    else None
                ),
            },
            "temperatures": heaters,
            "other_sensors": sensors,
            "motion": {
                "homed_axes": toolhead.get("homed_axes", ""),
                "position": toolhead.get("position"),
                "gcode_position": gcode_move.get("gcode_position"),
                "speed_factor_percent": round(
                    float(gcode_move.get("speed_factor", 1.0) or 1.0) * 100, 1
                ),
                "extrude_factor_percent": round(
                    float(gcode_move.get("extrude_factor", 1.0) or 1.0) * 100, 1
                ),
                "z_offset": gcode_move.get("homing_origin", [0, 0, 0, 0])[2]
                if gcode_move.get("homing_origin")
                else None,
                "max_velocity": toolhead.get("max_velocity"),
                "max_accel": toolhead.get("max_accel"),
            },
            "idle_timeout": {
                "state": idle.get("state"),
                "printing_time_s": idle.get("printing_time"),
            },
            "display_message": display.get("message") or None,
        }

        filament = next(
            (v for k, v in status.items() if k.startswith("filament_switch_sensor ")), None
        )
        if isinstance(filament, dict):
            payload["filament"] = {
                "detected": filament.get("filament_detected"),
                "sensor_enabled": filament.get("enabled"),
            }
        return payload

    @server.tool(
        name="get_capabilities",
        description=(
            "What this specific printer supports, discovered live: heaters and their real "
            "temperature limits, controllable fans, sensors, LEDs, output pins, filament "
            "sensors, axis travel, calibration features, QIDI-specific Klipper modules, "
            "Moonraker components present, and macro count. Call this before assuming a "
            "feature exists — QIDI ship a trimmed Moonraker and an extended Klipper."
        ),
        annotations=READ,
    )
    @guarded
    async def get_capabilities(refresh: bool = False) -> dict[str, Any]:
        caps = await rt.capabilities(refresh=refresh)
        await rt.sync_limits()
        # Whether cross-agent coordination works at all is a capability of the
        # machine, not of this server: it depends on the printer's Moonraker
        # keeping its 'database' component. Somewhere between "what this printer
        # supports" and nowhere, this belongs in the former.
        return {"ok": True, **caps.summary(), "sharing": await rt.lease.status()}

    @server.tool(
        name="get_safety_policy",
        description=(
            "The safety envelope this MCP server is enforcing: temperature ceilings, "
            "confirmation thresholds, jog/extrude caps, read-only and during-print flags, "
            "and which risk tiers require confirmation. Read this to understand why a call "
            "was refused."
        ),
        annotations=READ,
    )
    @guarded
    async def get_safety_policy() -> dict[str, Any]:
        s = rt.settings
        env = s.safety
        return {
            "ok": True,
            # Which build is answering. An older server does not enforce the
            # printer lease, so an agent has to be able to tell.
            "server_version": __version__,
            "mode": {
                "read_only": s.read_only,
                "strict_confirm": s.strict_confirm,
                "allow_during_print": s.allow_during_print,
                "confirm_ttl_seconds": s.confirm_ttl,
            },
            "temperature_limits_c": {
                "extruder": {
                    "max": env.extruder.max,
                    "confirm_above": env.extruder.confirm_above,
                },
                "bed": {"max": env.bed.max, "confirm_above": env.bed.confirm_above},
                "chamber": {"max": env.chamber.max, "confirm_above": env.chamber.confirm_above},
            },
            "motion_limits": {
                "max_jog_distance_mm": env.max_jog_distance,
                "max_extrude_distance_mm": env.max_extrude_distance,
                "max_feedrate_mm_per_min": env.max_move_feedrate,
                "min_jog_z_mm": env.min_jog_z,
                "x_travel_mm": [s.hardware.x.min, s.hardware.x.max],
                "y_travel_mm": [s.hardware.y.min, s.hardware.y.max],
                "z_travel_mm": [s.hardware.z.min, s.hardware.z.max],
            },
            "risk_tiers": {
                "read": "no gate",
                "low": "no gate (lights, beeper, fans, messages)",
                "motion": "requires confirm=true",
                "thermal": "requires confirm=true and passes range check",
                "print": "requires confirm=true",
                "destructive": "requires two-step confirm_token",
                "emergency": "never gated (emergency_stop always works)",
            },
            "hardware_ceilings_c": {
                "extruder": s.hardware.extruder_max_temp,
                "bed": s.hardware.bed_max_temp,
                "chamber": s.hardware.chamber_max_temp,
                "note": "Configured limits can never exceed these.",
            },
            "sharing": {
                **await rt.lease.status(),
                "what_it_is": (
                    "A cooperative lease stored on the printer, so a second agent is "
                    "refused mutating calls while you work. It does not bind the "
                    "printer's own touchscreen or Fluidd — look at the machine too."
                ),
            },
        }

    @server.tool(
        name="get_temperatures",
        description=(
            "Current temperature, target, and heater power for every heater, plus all "
            "read-only temperature sensors (including the Max4's chamber thermal-protection "
            "sensor) and temperature-controlled fans."
        ),
        annotations=READ,
    )
    @guarded
    async def get_temperatures() -> dict[str, Any]:
        caps = await rt.capabilities()
        objects: dict[str, list[str] | None] = {}
        for info in caps.heaters.values():
            objects[str(info["object_key"])] = None
        for key in caps.temperature_sensors.values():
            objects[key] = None
        if not objects:
            objects = {"extruder": None, "heater_bed": None}

        status = await rt.client.query(objects)
        env = rt.settings.safety
        heaters: dict[str, Any] = {}
        for name, info in caps.heaters.items():
            obj = status.get(str(info["object_key"])) or {}
            limits = env.heater("bed" if name == "heater_bed" else name)
            heaters[name] = {
                "temperature_c": obj.get("temperature"),
                "target_c": obj.get("target"),
                "power": obj.get("power"),
                "firmware_max_c": info.get("max_temp"),
                "mcp_allowed_max_c": limits.max if limits else None,
            }
        sensors = {}
        for name, key in caps.temperature_sensors.items():
            obj = status.get(key) or {}
            sensors[name] = {
                "temperature_c": obj.get("temperature"),
                "measured_min_c": obj.get("measured_min_temp"),
                "measured_max_c": obj.get("measured_max_temp"),
                "target_c": obj.get("target"),
                "speed": obj.get("speed"),
            }
        return {"ok": True, "heaters": heaters, "sensors": sensors}

    @server.tool(
        name="get_position",
        description=(
            "Toolhead position: raw kinematic position, the G-code position (offsets "
            "applied), which axes are homed, the active Z offset, and speed/flow factors."
        ),
        annotations=READ,
    )
    @guarded
    async def get_position() -> dict[str, Any]:
        status = await rt.client.query(
            {
                "toolhead": ["position", "homed_axes", "axis_minimum", "axis_maximum", "extruder"],
                "gcode_move": [
                    "gcode_position",
                    "position",
                    "homing_origin",
                    "speed_factor",
                    "extrude_factor",
                    "speed",
                    "absolute_coordinates",
                    "absolute_extrude",
                ],
                "motion_report": ["live_position", "live_velocity", "live_extruder_velocity"],
            }
        )
        toolhead = status.get("toolhead", {}) or {}
        gm = status.get("gcode_move", {}) or {}
        motion = status.get("motion_report", {}) or {}
        origin = gm.get("homing_origin") or [0, 0, 0, 0]
        return {
            "ok": True,
            "homed_axes": toolhead.get("homed_axes", ""),
            "kinematic_position": toolhead.get("position"),
            "gcode_position": gm.get("gcode_position"),
            "live_position": motion.get("live_position"),
            "live_velocity_mm_s": motion.get("live_velocity"),
            "offsets": {"x": origin[0], "y": origin[1], "z": origin[2]},
            "axis_minimum": toolhead.get("axis_minimum"),
            "axis_maximum": toolhead.get("axis_maximum"),
            "active_extruder": toolhead.get("extruder"),
            "absolute_coordinates": gm.get("absolute_coordinates"),
            "absolute_extrude": gm.get("absolute_extrude"),
            "speed_factor_percent": round(float(gm.get("speed_factor", 1.0) or 1.0) * 100, 1),
            "extrude_factor_percent": round(float(gm.get("extrude_factor", 1.0) or 1.0) * 100, 1),
        }

    @server.tool(
        name="query_objects",
        description=(
            "Escape hatch for arbitrary Klipper status objects. Pass a list of object names "
            "(e.g. ['bed_mesh','probe','mcu']); omit to get the full object list instead. "
            "Use get_capabilities first to see what exists."
        ),
        annotations=READ,
    )
    @guarded
    async def query_objects(objects: list[str] | None = None) -> dict[str, Any]:
        if not objects:
            available = await rt.client.object_list()
            return {
                "ok": True,
                "available_objects": available,
                "hint": "Pass a subset of these as `objects` to read their values.",
            }
        if len(objects) > 40:
            raise InvalidRequest("Query at most 40 objects at a time.")
        status = await rt.client.query(dict.fromkeys(objects))
        missing = [o for o in objects if o not in status]
        payload: dict[str, Any] = {"ok": True, "status": status}
        if missing:
            payload["not_present"] = missing
        return payload

    @server.tool(
        name="get_job_status",
        description=(
            "Detail on the current print job: state, filename, progress by both layer and "
            "file position, durations, filament used, estimated remaining time, and the "
            "slicer metadata for the running file."
        ),
        annotations=READ,
    )
    @guarded
    async def get_job_status() -> dict[str, Any]:
        status = await rt.client.query(
            {
                "print_stats": None,
                "display_status": None,
                "virtual_sdcard": None,
                "idle_timeout": None,
            }
        )
        stats = status.get("print_stats", {}) or {}
        display = status.get("display_status", {}) or {}
        vsd = status.get("virtual_sdcard", {}) or {}
        info = stats.get("info") or {}

        progress = float(display.get("progress", 0.0) or 0.0)
        print_duration = float(stats.get("print_duration", 0.0) or 0.0)
        remaining = None
        remaining_source = None
        if progress > 0.01 and print_duration > 0:
            remaining = round(print_duration / progress - print_duration)
            remaining_source = "progress"

        payload: dict[str, Any] = {
            "ok": True,
            "state": stats.get("state", "standby"),
            "filename": stats.get("filename") or None,
            "progress_percent": round(progress * 100, 1),
            "current_layer": info.get("current_layer"),
            "total_layers": info.get("total_layer"),
            "print_duration_s": stats.get("print_duration"),
            "total_duration_s": stats.get("total_duration"),
            "estimated_remaining_s": remaining,
            # "progress": extrapolated from how far the print has got; "slicer":
            # the file's own estimate minus time printed, used before progress is
            # meaningful (the first percent of a long print, and heat-up).
            "estimated_remaining_source": remaining_source,
            "filament_used_mm": stats.get("filament_used"),
            "file_position_bytes": vsd.get("file_position"),
            "file_size_bytes": vsd.get("file_size"),
            "is_active": bool(vsd.get("is_active")),
            "message": stats.get("message") or None,
            "idle_timeout_state": (status.get("idle_timeout") or {}).get("state"),
        }
        filename = stats.get("filename")
        if filename:
            try:
                meta = await rt.client.file_metadata(filename)
                payload["file_metadata"] = {
                    "estimated_time_s": meta.get("estimated_time"),
                    "filament_total_mm": meta.get("filament_total"),
                    "filament_weight_total_g": meta.get("filament_weight_total"),
                    "layer_height_mm": meta.get("layer_height"),
                    "first_layer_height_mm": meta.get("first_layer_height"),
                    "object_height_mm": meta.get("object_height"),
                    "nozzle_diameter_mm": meta.get("nozzle_diameter"),
                    "filament_type": meta.get("filament_type"),
                    "filament_name": meta.get("filament_name"),
                    "slicer": meta.get("slicer"),
                    "slicer_version": meta.get("slicer_version"),
                    "first_layer_bed_temp_c": meta.get("first_layer_bed_temp"),
                    "first_layer_extr_temp_c": meta.get("first_layer_extr_temp"),
                    "chamber_temp_c": meta.get("chamber_temp"),
                }
            except Exception:  # metadata is best-effort
                payload["file_metadata"] = None
            est = (payload.get("file_metadata") or {}).get("estimated_time_s")
            active = payload["state"] in ("printing", "paused")
            if remaining is None and active and isinstance(est, (int, float)) and est > 0:
                payload["estimated_remaining_s"] = max(0, round(float(est) - print_duration))
                payload["estimated_remaining_source"] = "slicer"
        return payload

    @server.tool(
        name="get_printer_config",
        description=(
            "The printer's live Klipper configuration as Moonraker reports it: either the "
            "resolved settings tree (section -> key -> value) or the list of section names. "
            "Pass a section name to get just that section — the full tree is large."
        ),
        annotations=READ,
    )
    @guarded
    async def get_printer_config(section: str | None = None, list_sections: bool = False) -> dict[str, Any]:
        result = await rt.client.query({"configfile": ["settings", "save_config_pending"]})
        cfg = result.get("configfile", {}) or {}
        settings = cfg.get("settings", {}) or {}
        if section is None:
            return {
                "ok": True,
                "sections": sorted(settings.keys()),
                "save_config_pending": cfg.get("save_config_pending"),
                "hint": "Pass section='extruder' (for example) to read one section.",
            }
        key = (section or "").strip()
        chosen = settings.get(key) or settings.get(key.lower())
        if chosen is None:
            matches = [s for s in settings if key.lower() in s.lower()]
            raise InvalidRequest(
                f"No config section named {key!r}.",
                hint=f"Close matches: {matches[:10]}" if matches else "Call with list_sections=true.",
            )
        return {
            "ok": True,
            "section": key,
            "settings": chosen,
            "save_config_pending": cfg.get("save_config_pending"),
        }

    @server.tool(
        name="list_gcode_commands",
        description=(
            "Every G-code command and macro this printer accepts, with descriptions where "
            "available. Includes QIDI's custom macros (PRINT_START, M4029 levelling, "
            "M603/M604 filament handling, CLEAR_NOZZLE, and so on). Filter with `contains`."
        ),
        annotations=READ,
    )
    @guarded
    async def list_gcode_commands(contains: str | None = None, limit: int = 200) -> dict[str, Any]:
        help_map = await rt.client.gcode_help()
        items = sorted(help_map.items())
        if contains:
            needle = contains.upper()
            items = [(k, v) for k, v in items if needle in k.upper() or needle in str(v).upper()]
        total = len(items)
        items = items[: max(1, min(limit, 500))]
        return {
            "ok": True,
            "total_matching": total,
            "returned": len(items),
            "commands": dict(items),
        }

    @server.tool(
        name="get_gcode_log",
        description=(
            "Recent G-code console history — the commands sent and Klipper's replies, "
            "exactly what the Fluidd console shows. The fastest way to see why something "
            "failed or what a macro actually did."
        ),
        annotations=READ,
    )
    @guarded
    async def get_gcode_log(count: int = 50) -> dict[str, Any]:
        result = await rt.client.gcode_store(count=max(1, min(count, 1000)))
        entries = (result or {}).get("gcode_store", [])
        return {"ok": True, "count": len(entries), "entries": entries}

    @server.tool(
        name="get_temperature_history",
        description=(
            "Moonraker's cached temperature history (roughly the last 20 minutes, 1 Hz) for "
            "every heater and sensor: temperatures, targets, and power. Use this to judge "
            "whether a heater is tracking its target or a thermal runaway is developing."
        ),
        annotations=READ,
    )
    @guarded
    async def get_temperature_history(
        sensor: str | None = None, last_n: int = 60, include_monitors: bool = False
    ) -> dict[str, Any]:
        store = await rt.client.temperature_store(include_monitors=include_monitors)
        store = store or {}
        if sensor:
            match = store.get(sensor) or next(
                (v for k, v in store.items() if k.lower() == sensor.lower()), None
            )
            if match is None:
                raise InvalidRequest(
                    f"No temperature history for {sensor!r}.",
                    hint=f"Available: {sorted(store.keys())}",
                )
            store = {sensor: match}
        n = max(1, min(last_n, 1200))
        trimmed = {
            name: {k: (v[-n:] if isinstance(v, list) else v) for k, v in series.items()}
            for name, series in store.items()
            if isinstance(series, dict)
        }
        return {"ok": True, "samples_per_series": n, "history": trimmed}

    @server.tool(
        name="get_system_info",
        description=(
            "Host and service health: Moonraker/Klipper versions, the printer's OS and "
            "CPU, load, memory, uptime, throttle flags (undervoltage), MCU load and "
            "communication error counters. Use this when prints fail oddly — an "
            "undervoltage flag or MCU bandwidth saturation explains a lot."
        ),
        annotations=READ,
    )
    @guarded
    async def get_system_info() -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True}
        try:
            server_info = await rt.client.server_info()
            payload["moonraker"] = {
                "version": server_info.get("moonraker_version"),
                "klippy_connected": server_info.get("klippy_connected"),
                "klippy_state": server_info.get("klippy_state"),
                "components": server_info.get("components"),
                "warnings": server_info.get("warnings"),
                "failed_components": server_info.get("failed_components"),
            }
        except Exception as exc:
            payload["moonraker_error"] = str(exc)
        try:
            pinfo = await rt.client.printer_info()
            payload["klipper"] = {
                "version": pinfo.get("software_version"),
                "state": pinfo.get("state"),
                "state_message": pinfo.get("state_message"),
                "hostname": pinfo.get("hostname"),
                "cpu_info": pinfo.get("cpu_info"),
                "python_version": pinfo.get("python_version"),
            }
        except Exception as exc:
            payload["klipper_error"] = str(exc)
        try:
            sysinfo = await rt.client.system_info()
            info = (sysinfo or {}).get("system_info", {})
            payload["host"] = {
                "distribution": (info.get("distribution") or {}).get("name"),
                "cpu": {
                    k: (info.get("cpu_info") or {}).get(k)
                    for k in ("cpu_count", "processor", "model", "total_memory", "memory_units")
                },
                "python": (info.get("python") or {}).get("version_string"),
                "virtualization": info.get("virtualization"),
                "instance_ids": info.get("instance_ids"),
            }
        except Exception as exc:
            payload["host_error"] = str(exc)
        try:
            stats = await rt.client.proc_stats()
            stats = stats or {}
            recent = (stats.get("moonraker_stats") or [])[-1:]
            payload["process"] = {
                "moonraker_latest": recent[0] if recent else None,
                "throttled_state": stats.get("throttled_state"),
                "cpu_temp": stats.get("cpu_temp"),
                "system_cpu_usage": stats.get("system_cpu_usage"),
                "system_uptime": stats.get("system_uptime"),
                "websocket_connections": stats.get("websocket_connections"),
            }
        except Exception as exc:
            payload["process_error"] = str(exc)
        try:
            mcu_status = await rt.client.query({"mcu": None, "system_stats": None})
            mcu = mcu_status.get("mcu", {}) or {}
            payload["mcu"] = {
                "mcu_version": mcu.get("mcu_version"),
                "mcu_build_versions": mcu.get("mcu_build_versions"),
                "last_stats": mcu.get("last_stats"),
            }
            payload["klipper_process"] = mcu_status.get("system_stats")
        except Exception as exc:
            payload["mcu_error"] = str(exc)
        return payload

    @server.tool(
        name="get_audit_log",
        description=(
            "Every non-read action this MCP server has taken on the printer this session, "
            "with risk tier, arguments, and outcome (including refusals). Use it to answer "
            "'what did the agent actually do to my printer?'"
        ),
        annotations=READ,
    )
    @guarded
    async def get_audit_log(count: int = 30) -> dict[str, Any]:
        entries = rt.policy.audit.tail(max(1, min(count, 200)))
        return {
            "ok": True,
            "count": len(entries),
            "log_file": rt.settings.audit_log,
            "entries": entries,
        }

    _ = (
        get_printer_status,
        get_capabilities,
        get_safety_policy,
        get_temperatures,
        get_position,
        query_objects,
        get_job_status,
        get_printer_config,
        list_gcode_commands,
        get_gcode_log,
        get_temperature_history,
        get_system_info,
        get_audit_log,
    )


_UNUSED = Risk  # re-exported for symmetry with mutating modules
