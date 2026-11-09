"""QIDI-specific features that have no upstream Klipper equivalent.

Everything here is derived from QIDI's own published macros for this machine
(``QIDI's published config/klipper-macros-qd/*.cfg``), not invented:

*   ``M603 S<temp>`` — unload filament.
*   ``M604 S<temp> T<mode>`` — load filament as a staged sequence:
    ``T0`` heat and park over the purge chute, ``T1`` push filament in,
    ``T2`` purge until the old colour is flushed.
*   ``CLEAR_NOZZLE HOTEND=<temp>`` — wipe the nozzle on the chute.
*   ``M4029 S<bed_temp> R<restart>`` — QIDI's own bed-levelling calibration.
*   ``M1002 R/A/S`` — Z-offset management, including persisting it.
*   ``M1004`` — enable the chamber "Polar cooler" (air conditioner) per settings.
*   ``M141``/``M191`` — chamber heater target / wait, plus ``DISABLE_BOX_HEATER``.
*   Box (multi-material) state lives in ``save_variables``: ``box_count``,
    ``enable_box``, ``slot0..15``, ``filament_slot*``, ``color_slot*``, and in the
    ``box_extras`` Klipper object. ``E_LOAD SLOT=<n> S=<temp>`` feeds one Box slot.
    The shared Box model and the list of what is and is not established about it
    is in ``qidi_mcp/box.py``.

The filament tools are exposed as *explicit stages* rather than one magic
"change filament" call. QIDI's own sequence is staged because a human is
expected to be standing there feeding filament; hiding that behind a single
call would make an agent believe a step succeeded when it silently did not.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..box import (
    BOX_OPTIONS,
    FILAMENT_TABLE_FILE,
    MAX_BOXES,
    SLOTS_PER_BOX,
    all_default_ids,
    as_int,
    box_reader,
    extruder_view,
    slot_views,
    unit_views,
)
from ..errors import (
    ConfirmationRequired,
    InterlockBlocked,
    InvalidRequest,
    NotSupported,
    QidiError,
)
from ..filament_names import (
    palette_listing,
    resolve_color,
    resolve_filament,
    resolve_vendor,
    search_filaments,
)
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="chamber_heater",
        description=(
            "Control the Max4's actively heated chamber: set a target in °C (max 65 by "
            "policy), or 0 to turn it off. The chamber is what makes ABS/ASA/PA printing "
            "work on this machine. Also reports the chamber circulation fan and the "
            "independent thermal-protection sensor. Requires confirm=true to heat."
        ),
        annotations=ACT,
    )
    @guarded
    async def chamber_heater(
        target: float | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_heated_chamber:
            raise NotSupported(
                "This printer has no actively heated chamber.",
                hint="get_capabilities shows the heaters that exist.",
            )

        # No target given: report state only.
        if target is None:
            status = await rt.client.query(
                {
                    "heater_generic chamber": None,
                    "fan_generic chamber_circulation_fan": None,
                    "temperature_sensor Chamber_Thermal_Protection_Sensor": None,
                    "output_pin Polar_cooler": None,
                }
            )
            chamber = status.get("heater_generic chamber", {}) or {}
            return {
                "ok": True,
                "chamber_temperature_c": chamber.get("temperature"),
                "chamber_target_c": chamber.get("target"),
                "heater_power": chamber.get("power"),
                "circulation_fan_speed": (
                    status.get("fan_generic chamber_circulation_fan") or {}
                ).get("speed"),
                "thermal_protection_sensor_c": (
                    status.get("temperature_sensor Chamber_Thermal_Protection_Sensor") or {}
                ).get("temperature"),
                "polar_cooler_on": (status.get("output_pin Polar_cooler") or {}).get("value"),
                "policy_max_c": rt.settings.safety.chamber.max,
            }

        await rt.sync_limits()
        rt.policy.check_temperature("chamber", float(target))
        args = {"target": target}
        risk = Risk.LOW if target <= 0 else Risk.THERMAL
        needs_ticket = target > 0 and rt.policy.temperature_needs_confirm("chamber", float(target))

        ticket = await rt.authorize(
            "chamber_heater",
            Risk.DESTRUCTIVE if needs_ticket else risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket

        await rt.client.gcode(f"M141 S{float(target):.0f}")
        return rt.ok(
            "chamber_heater",
            risk,
            args,
            {
                "chamber_target_c": target,
                "command_sent": f"M141 S{float(target):.0f}",
                "note": (
                    "Chamber off."
                    if target <= 0
                    else "A cold chamber takes a long time to heat; poll get_temperatures."
                ),
            },
        )

    @server.tool(
        name="unload_filament",
        description=(
            "Run QIDI's filament unload routine (M603). Heats the hotend to the given "
            "temperature, then retracts and ejects the filament. Give a temperature "
            "appropriate to the filament currently loaded (e.g. 250 for PLA, 280 for PETG). "
            "Two-step confirmation; refused during a print."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def unload_filament(
        hotend_temp: float = 250.0, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_gcode("M603"):
            raise NotSupported(
                "This printer does not define QIDI's M603 unload macro.",
                hint="Use extrude_filament with a negative distance instead.",
            )
        rt.policy.check_temperature("extruder", float(hotend_temp))
        if hotend_temp < 150:
            raise InvalidRequest(
                f"{hotend_temp} °C is too cold to unload filament — it will jam.",
                hint="Use the filament's normal printing temperature.",
            )
        args = {"hotend_temp": hotend_temp}
        ticket = await rt.authorize(
            "unload_filament",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["what_will_happen"] = (
                f"The hotend heats to {hotend_temp:.0f} °C, the toolhead moves to the purge "
                "area, and the filament is retracted out. Takes a few minutes. QIDI's macro "
                "disables the filament sensor for the duration."
            )
            return ticket
        await rt.client.gcode(f"M603 S{float(hotend_temp):.0f}", timeout=900.0)
        return rt.ok(
            "unload_filament",
            Risk.DESTRUCTIVE,
            args,
            {
                "command_sent": f"M603 S{hotend_temp:.0f}",
                "note": "Check the console with get_gcode_log; the sensor is re-enabled at the end.",
            },
        )

    @server.tool(
        name="load_filament",
        description=(
            "Run one stage of QIDI's filament load routine (M604). stage='heat' warms up and "
            "parks over the purge chute, stage='push' feeds filament in, stage='purge' "
            "extrudes until the old colour is flushed. Run them in that order, checking "
            "between stages — this mirrors what the printer's own screen does. Two-step "
            "confirmation; refused during a print."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def load_filament(
        stage: str = "heat",
        hotend_temp: float = 250.0,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_gcode("M604"):
            raise NotSupported(
                "This printer does not define QIDI's M604 load macro.",
                hint="Use extrude_filament to push filament manually instead.",
            )
        stages = {"heat": 0, "push": 1, "purge": 2}
        st = stage.strip().lower()
        if st not in stages:
            raise InvalidRequest(f"stage must be one of {sorted(stages)}.")
        rt.policy.check_temperature("extruder", float(hotend_temp))
        if hotend_temp < 150:
            raise InvalidRequest(f"{hotend_temp} °C is too cold to load filament.")

        args = {"stage": st, "hotend_temp": hotend_temp}
        ticket = await rt.authorize(
            "load_filament",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["what_will_happen"] = {
                "heat": (
                    f"Homes if needed, moves to the purge chute, heats to {hotend_temp:.0f} °C."
                ),
                "push": "Feeds ~80 mm of filament into the hotend.",
                "purge": "Extrudes ~250 mm to flush the previous filament out.",
            }[st]
            return ticket

        mode = stages[st]
        await rt.client.gcode(f"M604 S{float(hotend_temp):.0f} T{mode}", timeout=900.0)
        next_stage = {"heat": "push", "push": "purge", "purge": None}[st]
        return rt.ok(
            "load_filament",
            Risk.DESTRUCTIVE,
            args,
            {
                "stage_completed": st,
                "command_sent": f"M604 S{hotend_temp:.0f} T{mode}",
                "next_stage": next_stage,
                "note": (
                    f"Now call load_filament stage='{next_stage}'."
                    if next_stage
                    else "Load sequence complete. Check the purge chute is clear."
                ),
            },
        )

    @server.tool(
        name="clean_nozzle",
        description=(
            "Run QIDI's nozzle-cleaning routine (CLEAR_NOZZLE): heats up, moves over the "
            "purge chute and wipes the nozzle. Worth doing before a critical first layer. "
            "Requires confirm=true; refused during a print."
        ),
        annotations=ACT,
    )
    @guarded
    async def clean_nozzle(
        hotend_temp: float = 250.0, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_gcode("CLEAR_NOZZLE"):
            raise NotSupported("This printer does not define QIDI's CLEAR_NOZZLE macro.")
        rt.policy.check_temperature("extruder", float(hotend_temp))
        if hotend_temp < 150:
            raise InvalidRequest(f"{hotend_temp} °C is too cold to clean the nozzle.")
        args = {"hotend_temp": hotend_temp}
        ticket = await rt.authorize(
            "clean_nozzle",
            Risk.MOTION,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            return ticket
        await rt.client.gcode(f"CLEAR_NOZZLE HOTEND={float(hotend_temp):.0f}", timeout=600.0)
        return rt.ok(
            "clean_nozzle",
            Risk.MOTION,
            args,
            {"command_sent": f"CLEAR_NOZZLE HOTEND={hotend_temp:.0f}"},
        )

    @server.tool(
        name="qidi_bed_level_calibrate",
        description=(
            "Run QIDI's own bed-levelling calibration (M4029) — the routine the printer's "
            "touchscreen calls, which heats the bed, probes, and stores the result. This is "
            "QIDI's equivalent of bed_mesh_calibrate plus Z-tilt, tuned for this machine. "
            "Takes 10+ minutes and normally restarts Klipper at the end. Two-step confirmation."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def qidi_bed_level_calibrate(
        bed_temp: float = 60.0,
        restart_after: bool = True,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_gcode("M4029"):
            raise NotSupported(
                "This printer does not define QIDI's M4029 levelling macro.",
                hint="Use bed_mesh_calibrate and z_tilt_adjust instead.",
            )
        rt.policy.check_temperature("bed", float(bed_temp))
        args = {"bed_temp": bed_temp, "restart_after": restart_after}
        ticket = await rt.authorize(
            "qidi_bed_level_calibrate",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["what_will_happen"] = (
                f"The bed heats to {bed_temp:.0f} °C, the printer homes and probes the bed, "
                "levels the dual Z steppers, and saves the result. Expect 10+ minutes, and "
                "with restart_after=true Klipper restarts at the end (which is how the values "
                "persist)."
            )
            return ticket
        script = f"M4029 S{float(bed_temp):.0f} R{1 if restart_after else 0}"
        await rt.client.gcode(script, timeout=1800.0)
        return rt.ok(
            "qidi_bed_level_calibrate",
            Risk.DESTRUCTIVE,
            args,
            {
                "command_sent": script,
                "note": (
                    "Klipper restarts at the end; expect a brief connection gap."
                    if restart_after
                    else "Results are in memory only."
                ),
            },
        )

    @server.tool(
        name="save_z_offset",
        description=(
            "Persist the current live Z offset using QIDI's M1002 routine, so it survives a "
            "restart. Use set_z_offset to dial it in first, then this to keep it. Two-step "
            "confirmation, because it writes persistent state."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def save_z_offset(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_gcode("M1002"):
            raise NotSupported("This printer does not define QIDI's M1002 Z-offset macro.")
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "save_z_offset",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            status = await rt.client.query({"gcode_move": ["homing_origin"]})
            origin = (status.get("gcode_move") or {}).get("homing_origin") or [0, 0, 0, 0]
            ticket["current_z_offset_mm"] = origin[2]
            ticket["why"] = "This writes the offset to the printer's saved variables."
            return ticket
        await rt.client.gcode("M1002 S1", timeout=120.0)
        return rt.ok("save_z_offset", Risk.DESTRUCTIVE, args, {"command_sent": "M1002 S1"})

    @server.tool(
        name="get_box_status",
        description=(
            "State of QIDI's Box (the multi-filament switcher): whether the firmware will "
            "actually use it, each unit's four slots with what the printer believes is in "
            "them (filament, colour and vendor decoded to names where QIDI's tables are "
            "readable), which slot is synced to the extruder, and the live box_extras "
            "object verbatim. If the Box is not working, `problems` says which of the "
            "conditions QIDI's macros test is false. Compare `slots` with the file's "
            "filament_type / filament_colors / referenced_tools from get_file_metadata "
            "before printing a multi-colour job. Returns box_count=0 on a machine with "
            "no Box."
        ),
        annotations=READ,
    )
    @guarded
    async def get_box_status() -> dict[str, Any]:
        reader = box_reader(rt)
        state = await reader.state()
        table = await reader.table() if state.slot_total else None
        slots = slot_views(state, table)
        problems = state.problems()
        v = state.variables

        if state.detected and not problems:
            note = (
                f"{state.units} Box unit(s) attached and enabled."
                if state.tool_change_active is not True
                else f"{state.units} Box unit(s) attached; a tool change is in progress."
            )
        elif problems:
            note = "The Box is not usable as configured: " + problems[0]
        else:
            note = "No Box unit is attached to this printer."

        caveat = "Slot contents are what the printer's saved variables claim, not a measurement."
        if all_default_ids(state):
            caveat += (
                " 1/1/1 (filament/colour/vendor) is QIDI's factory default and also a real "
                "spool, so identical IDs across every slot usually mean nothing has been "
                "assigned yet -- name what is in each slot with set_box_slot_filaments."
            )

        return {
            "ok": True,
            "detected": state.detected,
            "ready": state.ready,
            "has_multi_color_controller": state.controller_present,
            "box_count": state.box_count,
            "box_enabled": state.enabled,
            "slots_per_unit": SLOTS_PER_BOX,
            # Raw strings as the printer stores them ('slot2', or 'slot-1' for none).
            "active_slot": v.get("slot_sync"),
            "last_loaded_slot": v.get("last_load_slot"),
            "active_slot_index": state.active_slot,
            "tool_change_in_progress": state.tool_change_active,
            "auto_reload_on_runout": bool(int(v.get("auto_reload_detect", 0) or 0)),
            "auto_read_rfid": bool(int(v.get("auto_read_rfid", 0) or 0)),
            "summary": [x["label"] for x in slots if "label" in x],
            "units": unit_views(slots),
            "slots": slots,
            "extruder": extruder_view(state, table) if state.detected else None,
            "toolhead_filament_detected": state.toolhead_filament,
            "box_extras": dict(state.extras) if state.extras is not None else None,
            "motion_sensors": {k: dict(v) for k, v in state.motion_sensors.items()},
            "load_retry": {
                "attempts": v.get("load_retry_num"),
                "step": v.get("retry_step"),
            },
            "commands_available": dict(state.commands),
            "names_decoded": table is not None,
            "ids_look_unassigned": all_default_ids(state),
            "problems": problems,
            "note": note,
            "caveat": caveat,
        }

    @server.tool(
        name="load_box_slot",
        description=(
            "Feed one Box slot toward the extruder with QIDI's E_LOAD SLOT=<n> S=<temp>. "
            "Refused unless the printer lists E_LOAD: a real Max4 Box seen so far did not "
            "(it has MULTI_COLOR_LOAD, parameters unpublished), so on it this tool says so "
            "and sends nothing. "
            "slot is 0-based across the whole Box (unit 1 = slots 0-3, unit 2 = 4-7, ...). "
            "Refused unless the firmware will actually act on the Box (see get_box_status "
            "`problems`), the slot exists, and no tool change is running; empty-slot and "
            "already-loaded conditions are shown on the ticket for you to weigh. Two-step "
            "confirmation; refused during a print. Not yet verified against a physical Box: "
            "run docs/guides/box-first-contact.md first. To unload, use unload_filament -- "
            "QIDI's M603 already calls E_UNLOAD for the extruder."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def load_box_slot(
        slot: int,
        hotend_temp: float = 250.0,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise InvalidRequest("slot must be an integer.")

        reader = box_reader(rt)
        state = await reader.state()

        if not state.detected and not state.problems():
            raise NotSupported(
                "This printer has no Box attached (save_variables.box_count is 0 and "
                "Klipper has no 'box_extras' object).",
                hint="Use load_filament to feed filament from a spool holder instead.",
            )
        if blockers := state.load_blockers():
            raise InterlockBlocked(
                "The firmware would not act on the Box as it is now: " + blockers[0],
                hint="get_box_status lists every condition; fix those first.",
                blockers=blockers,
                # Say it now, so nobody fixes the blockers and only then learns that this
                # tool has nothing to send on their firmware.
                also=(
                    None
                    if state.commands.get("E_LOAD")
                    else "Separately, this printer does not list E_LOAD, so load_box_slot "
                    "would refuse even once the Box is ready."
                ),
            )
        # This tool sends E_LOAD, which QIDI's published macros reference but which the
        # first real Max4 Box did NOT register (its commands are MULTI_COLOR_*, whose
        # parameters are unpublished). A command that is not in the printer's own list is
        # not "probably there": refuse rather than send a feed command nobody has seen
        # work. If discovery itself failed the list is empty and this refuses too, which
        # is the safe direction; get_capabilities refresh=True re-reads it.
        if not state.commands.get("E_LOAD"):
            raise NotSupported(
                "This printer does not list E_LOAD, the command this tool sends.",
                hint=(
                    "get_box_status commands_available shows which Box commands exist. A Max4 "
                    "Box seen on 2026-10-22 had MULTI_COLOR_LOAD / MULTI_COLOR_UNLOAD instead, "
                    "with no published parameters, so loading is not offered here. Load from "
                    "the printer's screen. If you think discovery was stale, call "
                    "get_capabilities with refresh=true and retry."
                ),
                commands_available=dict(state.commands),
            )
        # Reaching here means detected, enabled and fully readable: every other case
        # is either "no Box" above or a named blocker.
        if not 0 <= slot < state.slot_total:
            raise InvalidRequest(
                f"slot {slot} does not exist: {state.units} Box unit(s) give slots "
                f"0-{state.slot_total - 1}.",
                hint="Slot 16 is the extruder itself; use load_filament for that.",
            )

        rt.policy.check_temperature("extruder", float(hotend_temp))
        if hotend_temp < 150:
            raise InvalidRequest(f"{hotend_temp} °C is too cold to feed filament.")

        script = f"E_LOAD SLOT={slot} S={float(hotend_temp):.0f}"
        args = {"slot": slot, "hotend_temp": hotend_temp}
        ticket = await rt.authorize(
            "load_box_slot",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            flags = state.variables.get(f"slot{slot}")
            warnings = []
            if flags is not None and not int(flags or 0):
                warnings.append(
                    f"The printer reports slot {slot} as empty (save_variables.slot{slot} = 0)."
                )
            if state.active_slot is not None and state.active_slot != slot:
                warnings.append(
                    f"The printer believes slot {state.active_slot} is already synced to the "
                    "extruder; unload it first or two filaments will meet in the hotend."
                )
            if state.toolhead_filament:
                warnings.append(
                    "The toolhead filament sensor reports filament in the extruder path."
                )
            ticket["will_send"] = script
            ticket["what_will_happen"] = (
                f"The Box feeds slot {slot} toward the extruder with the hotend at "
                f"{hotend_temp:.0f} °C. Follow with load_filament stage='push' and then "
                "'purge' to seat and flush it, as the printer's screen does."
            )
            ticket["warnings"] = warnings
            ticket["verification"] = (
                "Per-slot E_LOAD is inferred from QIDI's published macros (they call it "
                "with SLOT=16) and has not been run against a physical Box."
            )
            return ticket

        await rt.client.gcode(script, timeout=900.0)
        return rt.ok(
            "load_box_slot",
            Risk.DESTRUCTIVE,
            args,
            {
                "slot": slot,
                "command_sent": script,
                "next_stage": "push",
                "note": (
                    "Check get_box_status: active_slot should now name this slot. Then call "
                    "load_filament stage='push', and stage='purge' once filament shows at "
                    "the nozzle."
                ),
            },
            detail=script,
        )

    @server.tool(
        name="set_box_option",
        description=(
            "Change one Box setting QIDI stores in saved variables: 'enable_box' (the "
            "firmware's master switch for Box handling), 'auto_read_rfid' (read the spool "
            "tag when a spool is inserted) or 'auto_reload_detect' (on a filament runout, "
            "run QIDI's AUTO_RELOAD_FILAMENT -- the Box feeds filament by itself). "
            "Needs a detected Box, refused during a print, and always needs confirm=true "
            "because there is no free direction: switching auto-reload on makes the machine "
            "feed filament unattended, and switching enable_box off makes the firmware "
            "ignore the Box. Reads the value back and reports whether it took. Slot "
            "contents and box_count cannot be set here."
        ),
        annotations=ACT,
    )
    @guarded
    async def set_box_option(
        option: str,
        enabled: bool,
        confirm: bool = False,
    ) -> dict[str, Any]:
        name = (option or "").strip().lower()
        if name not in BOX_OPTIONS:
            raise InvalidRequest(
                f"{option!r} is not a Box option this tool can change.",
                hint=f"Choose one of: {', '.join(sorted(BOX_OPTIONS))}.",
            )

        state = await box_reader(rt).state()
        if not state.detected:
            raise NotSupported(
                "No Box is detected, so there is nothing to configure.",
                hint="get_box_status explains which condition is missing.",
                problems=state.problems(),
            )

        args: dict[str, Any] = {"option": name, "enabled": bool(enabled)}
        ticket = await rt.authorize(
            "set_box_option",
            Risk.PRINT,
            args,
            confirm=confirm,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["current"] = bool(int(state.variables.get(name, 0) or 0))
            ticket["effect"] = BOX_OPTIONS[name]
            return ticket

        # A plain integer, matching how QIDI's own macros store and compare these
        # (`auto_reload_detect == 1`). The name is allow-listed above, so nothing
        # caller-controlled reaches the G-code.
        script = f"SAVE_VARIABLE VARIABLE={name} VALUE={1 if enabled else 0}"
        await rt.client.gcode(script)

        after = await box_reader(rt).state()
        stored = int(after.variables.get(name, -1) or 0) if name in after.variables else None
        took = stored == (1 if enabled else 0)
        return rt.ok(
            "set_box_option",
            Risk.PRINT,
            args,
            {
                "option": name,
                "requested": bool(enabled),
                "stored_value": stored,
                "applied": took,
                "command_sent": script,
                "effect": BOX_OPTIONS[name],
                "note": (
                    "Saved."
                    if took
                    else "The command was accepted but the value read back does not match; "
                    "check get_box_status."
                ),
            },
            detail=script,
        )

    @server.tool(
        name="list_box_filament_options",
        description=(
            "Search the filament profiles, colour swatches and vendors the printer itself "
            "knows (its officiall_filas_list.cfg). Use it to find the exact name to give "
            "set_box_slot_filaments. `query` filters profiles by words in their name or "
            "type ('petg', 'matte', 'cf'); leave it empty for all of them. Colours are the "
            "printer's fixed swatches, each with a plain-English name."
        ),
        annotations=READ,
    )
    @guarded
    async def list_box_filament_options(query: str | None = None) -> dict[str, Any]:
        reader = box_reader(rt)
        table = await reader.table()
        if table is None:
            raise NotSupported(
                f"The printer does not serve {FILAMENT_TABLE_FILE}, so its filament list "
                "cannot be read.",
                hint="Check list_directory on the 'config' root.",
            )
        found = search_filaments(table.filaments, query)
        return {
            "ok": True,
            "query": query,
            "matches": len(found),
            "of_total": sum(1 for f in table.filaments.values() if f.get("name")),
            "filaments": found,
            "colors": palette_listing(table.colors),
            "vendors": [{"id": k, "name": v} for k, v in sorted(table.vendors.items())],
            "source": FILAMENT_TABLE_FILE,
            "note": (
                "Pass any of these names to set_box_slot_filaments. A bare material "
                "('PETG') resolves to its generic 'Basic' profile and says so."
            ),
        }

    _slot_keys = {"slot", "filament", "color", "vendor"}

    @server.tool(
        name="set_box_slot_filaments",
        description=(
            "Tell the printer what filament is in each Box slot, in plain words. "
            "`assignments` is a list like [{\"slot\": 0, \"filament\": \"PLA Matte\", "
            "\"color\": \"black\"}, {\"slot\": 1, \"filament\": \"PETG\", \"color\": "
            "\"#1353AB\", \"vendor\": \"Generic\"}]. slot is 0-based across the Box (unit 1 = "
            "slots 0-3). Filament and vendor are names or ids from list_box_filament_options; "
            "color is a colour word, #RRGGBB or swatch id, and lands on the nearest of the "
            "printer's own swatches. Only the fields you name change; everything else is "
            "kept. The first call (no confirm) shows exactly what each name resolved to "
            "and what would be written; re-call with confirm=true to write it, and the "
            "values are read back. This labels spools for the printer; it moves no filament "
            "and does not change whether a slot holds filament. Refused during a print. "
            "Not yet verified on a physical Box."
        ),
        annotations=ACT,
    )
    @guarded
    async def set_box_slot_filaments(
        assignments: list[dict[str, Any]],
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(assignments, list) or not assignments:
            raise InvalidRequest(
                "assignments must be a non-empty list of {slot, filament?, color?, vendor?}.",
                hint='Example: [{"slot": 0, "filament": "PLA Matte", "color": "black"}]',
            )
        if len(assignments) > MAX_BOXES * SLOTS_PER_BOX:
            raise InvalidRequest(f"At most {MAX_BOXES * SLOTS_PER_BOX} slots exist.")

        reader = box_reader(rt)
        state = await reader.state()
        if not state.detected:
            raise NotSupported(
                "No Box is detected, so there are no slots to describe.",
                hint="get_box_status explains which condition is missing.",
                problems=state.problems(),
            )
        table = await reader.table()
        if table is None or not table.filaments:
            raise NotSupported(
                f"The printer's {FILAMENT_TABLE_FILE} is unreadable, so names cannot be "
                "turned into the ids the firmware stores.",
                hint="Nothing was written. Check list_directory on the 'config' root.",
            )

        # -- resolve every name before anything is written ----------------------------
        plan: list[dict[str, Any]] = []
        seen: set[int] = set()
        for index, raw in enumerate(assignments):
            if not isinstance(raw, dict):
                raise InvalidRequest(f"assignments[{index}] must be an object, got {raw!r}.")
            item = {("color" if k == "colour" else k): v for k, v in raw.items()}
            unknown = sorted(set(item) - _slot_keys)
            if unknown:
                raise InvalidRequest(
                    f"assignments[{index}] has unknown field(s): {', '.join(unknown)}.",
                    hint="Fields are slot, filament, color and vendor.",
                )
            slot = item.get("slot")
            if isinstance(slot, bool) or not isinstance(slot, int):
                raise InvalidRequest(f"assignments[{index}].slot must be an integer.")
            if not 0 <= slot < state.slot_total:
                raise InvalidRequest(
                    f"slot {slot} does not exist: {state.units} Box unit(s) give slots "
                    f"0-{state.slot_total - 1}.",
                    hint="Slot 16 is the extruder itself and cannot be set here.",
                )
            if slot in seen:
                raise InvalidRequest(f"slot {slot} appears more than once.")
            seen.add(slot)
            if not any(item.get(k) is not None for k in ("filament", "color", "vendor")):
                raise InvalidRequest(
                    f"assignments[{index}] (slot {slot}) names nothing to change.",
                    hint="Give at least one of filament, color or vendor.",
                )

            entry: dict[str, Any] = {
                "slot": slot,
                "unit": slot // SLOTS_PER_BOX + 1,
                "position": slot % SLOTS_PER_BOX + 1,
            }
            writes: dict[str, int] = {}
            if item.get("filament") is not None:
                f = resolve_filament(table.filaments, item["filament"])
                writes[f"filament_slot{slot}"] = f.id
                entry["filament"] = {
                    "id": f.id,
                    "name": f.name,
                    "type": f.type,
                    "nozzle_temp_c": [f.entry.get("min_temp"), f.entry.get("max_temp")],
                    "matched_by": f.matched_by,
                    "assumed": f.assumed,
                }
            if item.get("color") is not None:
                c = resolve_color(table.colors, item["color"])
                writes[f"color_slot{slot}"] = c.id
                entry["color"] = {
                    "id": c.id,
                    "hex": c.hex,
                    "name": c.name,
                    "matched_by": c.matched_by,
                    "approximate": c.approximate,
                }
            if item.get("vendor") is not None:
                vid, vname = resolve_vendor(table.vendors, item["vendor"])
                writes[f"vendor_slot{slot}"] = vid
                entry["vendor"] = {"id": vid, "name": vname}
            entry["_writes"] = writes
            plan.append(entry)

        # -- show before/after in words, using the same labels get_box_status uses ----
        merged = dict(state.variables)
        for e in plan:
            merged.update(e["_writes"])
        before_views = slot_views(state, table)
        after_views = slot_views(replace(state, variables=merged), table)
        changes: list[tuple[str, int]] = []
        for e in plan:
            e["before"] = before_views[e["slot"]]["label"]
            e["after"] = after_views[e["slot"]]["label"]
            todo = {
                name: val
                for name, val in e["_writes"].items()
                if as_int(state.variables.get(name), -1) != val
            }
            e["unchanged"] = not todo
            changes.extend(todo.items())
            del e["_writes"]

        args: dict[str, Any] = {
            "assignments": [
                {
                    "slot": e["slot"],
                    **{k: e[k]["id"] for k in ("filament", "color", "vendor") if k in e},
                }
                for e in plan
            ]
        }
        warnings: list[str] = []
        for e in plan:
            if e.get("filament", {}).get("assumed"):
                warnings.append(
                    f"slot {e['slot']}: {e['filament']['matched_by']} -> {e['filament']['name']}. "
                    "Name the exact profile if that is not the one."
                )
            if e.get("color", {}).get("approximate"):
                warnings.append(
                    f"slot {e['slot']}: the printer has no swatch exactly like that; "
                    f"nearest is {e['color']['name']} {e['color']['hex']}."
                )
        for e in plan:
            if not as_int(state.variables.get(f"slot{e['slot']}"), 0):
                warnings.append(
                    f"slot {e['slot']}: the Box reports no filament in that slot. Recorded "
                    "anyway; it shows as empty until the Box sees a spool there."
                )
        if state.variables.get("auto_read_rfid") not in (None, 0, "0"):
            warnings.append(
                "auto_read_rfid is on: inserting a QIDI-tagged spool re-reads its tag and "
                "replaces what you set for that slot."
            )
        preview = {
            "will_set": plan,
            "will_send": [f"SAVE_VARIABLE VARIABLE={name} VALUE={val}" for name, val in changes],
            "warnings": warnings,
            "nothing_to_change": not changes,
            "verification": (
                "Writing these saved variables is how QIDI's published saved_variables.cfg "
                "records slot contents; the firmware's reaction to them has not been "
                "observed on a physical Box."
            ),
        }
        try:
            ticket = await rt.authorize(
                "set_box_slot_filaments",
                Risk.PRINT,
                args,
                confirm=confirm,
                interlocks=Interlocks(forbid_during_print=True),
            )
        except ConfirmationRequired as exc:
            # The framework refuses an unconfirmed write. Hand the refusal the whole
            # resolved plan, so "what would this do?" is answered by the same call.
            exc.extra.update(preview)
            raise
        if ticket is not None:  # strict-confirm mode answers with a ticket instead
            ticket.update(preview)
            return ticket

        # -- write, then read back what the printer actually holds --------------------
        sent: list[str] = []
        error: str | None = None
        for name, val in changes:
            script = f"SAVE_VARIABLE VARIABLE={name} VALUE={val}"
            try:
                await rt.client.gcode(script)
            except QidiError as exc:
                error = f"{script!r} failed: {exc.message}"
                break
            sent.append(script)

        after = await reader.state()
        verified = []
        for e in plan:
            wanted = [
                (f"{k}_slot{e['slot']}", e[k]["id"])
                for k in ("filament", "color", "vendor")
                if k in e
            ]
            took = all(as_int(after.variables.get(n), -1) == v for n, v in wanted)
            verified.append(
                {
                    "slot": e["slot"],
                    "applied": took,
                    "now": slot_views(after, table)[e["slot"]]["label"],
                }
            )
        everything = error is None and all(v["applied"] for v in verified)
        return rt.ok(
            "set_box_slot_filaments",
            Risk.PRINT,
            args,
            {
                "all_applied": everything,
                "slots": verified,
                "commands_sent": sent,
                "error": error,
                "warnings": warnings,
                "note": (
                    "Saved. This describes the spools to the printer; it did not move "
                    "filament. get_box_status shows the result."
                    if everything
                    else "Not everything took. get_box_status shows what the printer holds."
                ),
            },
            detail="; ".join(sent) or "no change needed",
        )

    @server.tool(
        name="get_saved_variables",
        description=(
            "The printer's persistent saved variables (save_variables.cfg): Z offset, "
            "power-loss-recovery state, Box slot assignments, feature toggles, and the last "
            "printed file. Read-only view of what the firmware remembers across restarts."
        ),
        annotations=READ,
    )
    @guarded
    async def get_saved_variables() -> dict[str, Any]:
        status = await rt.client.query({"save_variables": None})
        variables = (status.get("save_variables", {}) or {}).get("variables", {}) or {}
        # Group the noisy per-slot keys so the response stays readable.
        slot_keys = {k: v for k, v in variables.items() if any(
            k.startswith(p) for p in ("slot", "filament_slot", "color_slot", "vendor_slot")
        )}
        general = {k: v for k, v in variables.items() if k not in slot_keys}
        return {
            "ok": True,
            "variables": general,
            "slot_variables_count": len(slot_keys),
            "slot_variables": slot_keys,
        }

    @server.tool(
        name="set_polar_cooler",
        description=(
            "Turn the Max4's chamber cooler ('Polar_cooler' — the active chamber "
            "air-conditioning) on or off. Used for PLA in a warm chamber, where you want the "
            "chamber cooler rather than hotter."
        ),
        annotations=ACT,
    )
    @guarded
    async def set_polar_cooler(on: bool) -> dict[str, Any]:
        caps = await rt.capabilities()
        pin = next((p for p in caps.output_pins if p.lower() == "polar_cooler"), None)
        if pin is None:
            raise NotSupported(
                "This printer has no Polar_cooler output.",
                hint=f"Output pins present: {sorted(caps.output_pins)}",
            )
        args = {"on": on}
        await rt.authorize("set_polar_cooler", Risk.LOW, args, interlocks=Interlocks())
        script = f"SET_PIN PIN={pin} VALUE={1 if on else 0}"
        await rt.client.gcode(script)
        return rt.ok("set_polar_cooler", Risk.LOW, args, {"command_sent": script, "cooler_on": on})

    @server.tool(
        name="get_filament_presets",
        description=(
            "QIDI's built-in filament profiles as configured on this printer, including the "
            "recommended drying temperature and time per material from the machine's own "
            "drying table. Useful for answering 'how should I dry this spool?'."
        ),
        annotations=READ,
    )
    @guarded
    async def get_filament_presets() -> dict[str, Any]:
        # The drying table ships as a config file on the printer, not a status object.
        try:
            raw = await rt.client.file_download("config", "drying.conf", max_bytes=20_000)
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return {
                "ok": True,
                "drying_profiles": None,
                "note": (
                    "Could not read drying.conf from the printer's config root; this firmware "
                    "may store it elsewhere."
                ),
            }
        profiles = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(";") or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 4:
                try:
                    profiles.append(
                        {
                            "material": parts[0],
                            "bed_temp_c": float(parts[1]),
                            "chamber_temp_c": float(parts[2]),
                            "duration_min": int(float(parts[3])),
                        }
                    )
                except ValueError:
                    continue
        return {
            "ok": True,
            "drying_profiles": profiles,
            "source": "drying.conf on the printer",
            "note": (
                "These are the bed/chamber temperatures and durations QIDI recommend for "
                "drying each material in the chamber."
            ),
        }

    @server.tool(
        name="get_ai_detection_status",
        description=(
            "Report the Max4's AI failure detection: whether spaghetti ('noodle') and "
            "foreign-object detection are enabled, their sensitivity, and the last verdict "
            "the camera service reported. Read-only. Note a zero count from a DISABLED "
            "detector means 'nothing ever reported', not 'the bed is clear' -- check the "
            "enabled flags before trusting it as evidence of anything."
        ),
        annotations=READ,
    )
    @guarded
    async def get_ai_detection_status() -> dict[str, Any]:
        status = await rt.client.query({"ai_func_manager": None})
        mgr = status.get("ai_func_manager") or {}
        if not mgr:
            raise NotSupported(
                "This printer has no ai_func_manager object.",
                hint="AI detection is a QIDI firmware feature; get_capabilities lists what exists.",
            )
        noodle = mgr.get("enable_noodle") or {}
        foreign = mgr.get("enable_foreign") or {}
        state = mgr.get("ai_state") or {}
        detectors = {
            "noodle": {
                "enabled": bool(noodle.get("enable")),
                "sensitivity": noodle.get("sensitivity"),
                "detects": "spaghetti / detached print",
            },
            "foreign": {
                "enabled": bool(foreign.get("enable")),
                "sensitivity": foreign.get("sensitivity"),
                "detects": "foreign object or debris on the build plate",
            },
        }
        any_on = any(d["enabled"] for d in detectors.values())
        return {
            "ok": True,
            "detectors": detectors,
            "last_result": {
                "error_type": state.get("error_type") or None,
                "counts": state.get("counts"),
            },
            "any_enabled": any_on,
            "note": (
                "Klipper does not run the vision model; QIDI's camera service does and pushes"
                " verdicts in. So these fields only ever update while that service is running"
                " and the detector is enabled."
                if any_on
                else "All detection is OFF. last_result is therefore meaningless -- a count of 0"
                " reflects a silent detector, not a healthy print."
            ),
        }

    @server.tool(
        name="set_ai_detection",
        description=(
            "Enable or disable one of the Max4's AI failure detectors: type 'noodle' "
            "(spaghetti) or 'foreign' (debris on the plate), optionally setting sensitivity "
            "to LOW/MEDIUM/HIGH. Worth enabling before a long unattended print. Turning a "
            "detector ON is free; turning one OFF removes a safety net and needs "
            "confirm=true. This only flips the firmware flag -- it cannot start QIDI's "
            "camera service, so verify with get_ai_detection_status afterwards."
        ),
        annotations=ACT,
    )
    @guarded
    async def set_ai_detection(
        detector: str,
        enabled: bool,
        sensitivity: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        kind = (detector or "").strip().lower()
        if kind not in ("noodle", "foreign"):
            raise InvalidRequest(
                f"Unknown detector {detector!r}.",
                hint="Use 'noodle' (spaghetti) or 'foreign' (debris on the plate).",
            )
        level: str | None = None
        if sensitivity is not None:
            level = str(sensitivity).strip().upper()
            if level not in ("LOW", "MEDIUM", "HIGH"):
                raise InvalidRequest(
                    f"Unknown sensitivity {sensitivity!r}.", hint="Use LOW, MEDIUM or HIGH."
                )

        args: dict[str, Any] = {"detector": kind, "enabled": bool(enabled)}
        if level:
            args["sensitivity"] = level
        # Asymmetric on purpose. Enabling a detector is free: putting friction on the
        # safe action would discourage agents from arming the failure detection at
        # all. Disabling one silently removes the only thing that would catch a print
        # coming apart, so that direction has to be asked for explicitly.
        risk = Risk.LOW if enabled else Risk.PRINT
        ticket = await rt.authorize("set_ai_detection", risk, args, confirm=confirm)
        if ticket is not None:
            return ticket

        sent = [f"ENABLE_AI_DETECT TYPE={kind} STATE={1 if enabled else 0}"]
        if level:
            sent.append(f"SET_AI_SENSITIVITY TYPE={kind} VALUE={level}")
        for line in sent:
            await rt.client.gcode(line)
        return rt.ok(
            "set_ai_detection",
            risk,
            args,
            {
                "detector": kind,
                "enabled": bool(enabled),
                "sensitivity": level,
                "commands_sent": sent,
                "note": (
                    "Confirm with get_ai_detection_status. Detection also needs QIDI's camera"
                    " service running to ever report anything."
                ),
            },
        )

    _ = (
        chamber_heater,
        unload_filament,
        load_filament,
        clean_nozzle,
        qidi_bed_level_calibrate,
        save_z_offset,
        get_box_status,
        load_box_slot,
        set_box_option,
        get_saved_variables,
        set_polar_cooler,
        get_filament_presets,
        get_ai_detection_status,
        set_ai_detection,
    )
