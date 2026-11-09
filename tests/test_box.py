"""QIDI Box support, tested against a mock that can grow a Box.

Read this before trusting any of it: **no Box has been connected to this server.**
The mock's ``attach_box()`` builds the machine QIDI's published macros imply (a
``box_extras`` object, ``E_LOAD``/``E_UNLOAD``, ``box_count`` in saved variables), so
these tests prove the server's logic -- what it reads, what it refuses, what it
sends -- not that a real Box answers that way. ``docs/guides/box-first-contact.md``
is the procedure that closes that gap.

What they do pin down, because it is where the old code was wrong or a Box could
hurt someone:

* the phantom-slot bug: a one-Box printer used to report all sixteen slots;
* "Box present" is two conditions (``box_count`` AND ``box_extras``), because that is
  what QIDI's macros test, and each failing half has its own diagnosis;
* nothing is sent to the Box until the firmware would actually act on it;
* the one writable path into ``save_variables`` is an allow-list.
"""

from __future__ import annotations

import pytest

from qidi_mcp.box import (
    MAX_BOXES,
    SLOTS_PER_BOX,
    BoxState,
    all_default_ids,
    parse_filament_table,
    parse_slot_name,
)
from qidi_mcp.errors import ConnectionFailed

from .mock_moonraker import FILAMENT_TABLE

# --------------------------------------------------------------------------
# Pure parsing
# --------------------------------------------------------------------------


def test_the_real_filament_table_parses_completely():
    """QIDI's own file, not the mock's slice: 50 filaments, the colour dict, the vendors."""
    from pathlib import Path

    path = Path(__file__).parent.parent / "research/upstream/qidi-max4/config/officiall_filas_list.cfg"
    if not path.exists():
        pytest.skip("Optional QIDI GPLv3 table absent; run bash scripts/fetch_upstream_sources.sh first")
    table = parse_filament_table(path.read_text())

    assert len(table.filaments) == 50
    assert table.filament(1)["name"] == "PLA Rapido"
    assert table.filament(1)["type"] == "PLA"
    assert table.filament(1)["max_temp"] == 240
    assert table.filament(50)["name"] == "TPU 95A-HF"
    # '#' starts a comment in most INI parsers; here it starts a value.
    assert table.color(1) == "#FAFAFA"
    assert table.color(24) == "#B87F2B"
    assert table.vendor(1) == "QIDI"


def test_unknown_and_garbage_ids_decode_to_none_not_to_a_guess():
    table = parse_filament_table(FILAMENT_TABLE)
    assert table.filament(999) is None
    assert table.filament("not a number") is None
    assert table.color(None) is None
    assert table.vendor(-1) is None


def test_an_empty_or_hostile_table_parses_without_raising():
    for text in ("", "garbage", "[fila1]\nfilament", "[fila1]\nmin_temp = hot", "= = ="):
        parse_filament_table(text)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("slot2", 2),
        ("'slot15'", 15),
        ("SLOT0", 0),
        ("slot-1", None),  # QIDI's "nothing synced"
        ("slot16", 16),
        ("slotx", None),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_slot_names(raw, expected):
    assert parse_slot_name(raw) == expected


def test_all_default_ids_needs_every_slot_to_be_the_factory_triple():
    base = {f"{k}_slot{i}": 1 for i in range(4) for k in ("filament", "color", "vendor")}
    assert all_default_ids(BoxState(variables={"box_count": 1, **base}))
    assert not all_default_ids(
        BoxState(variables={"box_count": 1, **base, "filament_slot2": 7})
    )
    # No slots described -> nothing to say the defaults are in use.
    assert not all_default_ids(BoxState(variables={"box_count": 0, **base}))


# --------------------------------------------------------------------------
# get_box_status
# --------------------------------------------------------------------------


async def test_a_printer_with_no_box_reports_no_slots_at_all(call_tool):
    """The phantom-slot bug: this used to list sixteen slots for a machine with none."""
    r = await call_tool("get_box_status")
    assert r["ok"] is True
    assert r["detected"] is False
    assert r["box_count"] == 0
    assert r["slots"] == []
    assert r["units"] == []
    assert r["extruder"] is None
    assert r["box_extras"] is None
    assert r["problems"] == []
    assert "No Box" in r["note"]


async def test_one_box_reports_exactly_four_slots(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("get_box_status")

    assert r["detected"] is True and r["ready"] is True
    assert r["problems"] == []
    assert [s["slot"] for s in r["slots"]] == [0, 1, 2, 3]
    assert [s["position"] for s in r["slots"]] == [1, 2, 3, 4]
    assert {s["unit"] for s in r["slots"]} == {1}
    assert len(r["units"]) == 1 and len(r["units"][0]["slots"]) == SLOTS_PER_BOX


async def test_two_boxes_report_eight_slots_grouped_by_unit(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(2)
    r = await call_tool("get_box_status")

    assert len(r["slots"]) == 8
    assert [u["unit"] for u in r["units"]] == [1, 2]
    # Unit 2 starts at firmware slot 4 -- the human position restarts at 1.
    unit2 = r["units"][1]["slots"]
    assert [(s["slot"], s["position"]) for s in unit2][0] == (4, 1)


async def test_a_nonsense_box_count_is_clamped_and_reported(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["box_count"] = 9
    r = await call_tool("get_box_status")

    assert len(r["slots"]) == MAX_BOXES * SLOTS_PER_BOX
    assert any("box_count is 9" in p for p in r["problems"])


async def test_occupied_slot_is_decoded_to_names(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)  # slot 0 holds filament id 2, colour 2
    r = await call_tool("get_box_status")

    slot0 = r["slots"][0]
    assert slot0["loaded"] is True
    assert slot0["filament_id"] == 2  # the raw id is always kept next to the name
    assert slot0["filament"]["name"] == "PLA Matte"
    assert slot0["filament"]["type"] == "PLA"
    assert slot0["filament"]["max_temp"] == 234  # synthetic mock profile, not vendor operating guidance
    assert slot0["color_hex"] == "#060606"
    assert slot0["vendor"] == "QIDI"
    assert r["names_decoded"] is True


async def test_names_degrade_to_raw_ids_when_the_table_is_unreadable(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.filament_table = None
    r = await call_tool("get_box_status")

    assert r["ok"] is True
    assert r["names_decoded"] is False
    assert r["slots"][0]["filament_id"] == 2
    assert "filament" not in r["slots"][0]


async def test_the_factory_default_triple_is_called_out(call_tool, moonraker):
    """1/1/1 is a real spool and also the unassigned default; say so, don't pick one."""
    _, printer = moonraker
    printer.attach_box(1)
    for i in range(4):
        printer.saved_variables[f"filament_slot{i}"] = 1
        printer.saved_variables[f"color_slot{i}"] = 1
    r = await call_tool("get_box_status")

    assert r["ids_look_unassigned"] is True
    assert "factory default" in r["caveat"]


async def test_the_active_slot_is_the_one_the_firmware_says_is_synced(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["slot_sync"] = "slot2"
    r = await call_tool("get_box_status")

    assert r["active_slot_index"] == 2
    assert [s["in_extruder"] for s in r["slots"]] == [False, False, True, False]


async def test_no_slot_is_in_the_extruder_when_the_firmware_says_none(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)  # the mock ships slot_sync == 'slot-1'
    r = await call_tool("get_box_status")
    assert r["active_slot_index"] is None
    assert not any(s["in_extruder"] for s in r["slots"])


async def test_live_box_objects_are_passed_through_verbatim(call_tool, moonraker):
    """QIDI do not publish box_extras' fields, so we must not interpret them."""
    _, printer = moonraker
    printer.attach_box(1)
    printer.box_extras = {"is_tool_change": 0, "some_field_we_never_heard_of": [1, 2, 3]}
    r = await call_tool("get_box_status")

    assert r["box_extras"]["some_field_we_never_heard_of"] == [1, 2, 3]
    assert r["motion_sensors"] == {
        "filament_motion_sensor box_motion_sensor": {"enabled": True, "filament_detected": False}
    }


async def test_box_commands_are_reported_as_the_printer_exposes_them(call_tool, moonraker):
    _, printer = moonraker
    stock = (await call_tool("get_box_status"))["commands_available"]
    assert stock["E_LOAD"] is False

    printer.attach_box(1)
    # Capabilities are cached; a fresh discovery is what a new session would see.
    await call_tool("get_capabilities", refresh=True)
    r = await call_tool("get_box_status")
    assert r["commands_available"]["E_LOAD"] is True
    assert r["commands_available"]["INIT_BOX_STATE"] is True


# -- the two halves of "a Box is present" ----------------------------------


async def test_box_count_without_a_live_box_object_is_diagnosed(call_tool, moonraker):
    """box.cfg ships with its includes commented out: count says 1, Klipper has no Box."""
    _, printer = moonraker
    printer.saved_variables["box_count"] = 1
    r = await call_tool("get_box_status")

    assert r["detected"] is False and r["ready"] is False
    assert any("box_extras" in p and "commented out" in p for p in r["problems"])
    assert "not usable" in r["note"]
    # The slots the saved variables describe are still shown -- hiding them would
    # hide the very data needed to diagnose it.
    assert len(r["slots"]) == 4


async def test_a_live_box_object_without_a_registered_count_is_diagnosed(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1, register_count=False)
    r = await call_tool("get_box_status")

    assert r["detected"] is False
    assert any("box_count is 0" in p and "never writes it" in p for p in r["problems"])
    assert r["slots"] == []


async def test_a_disabled_box_is_diagnosed(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["enable_box"] = 0
    r = await call_tool("get_box_status")

    assert r["detected"] is True and r["ready"] is False
    assert any("disabled" in p for p in r["problems"])


async def test_a_tool_change_in_progress_is_a_state_not_a_fault(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.box_extras = {"is_tool_change": 1}
    r = await call_tool("get_box_status")

    assert r["tool_change_in_progress"] is True
    assert r["problems"] == []  # nothing is misconfigured
    assert "tool change is in progress" in r["note"]


async def test_unreadable_live_objects_do_not_prove_the_box_is_absent(
    call_tool, server_rt, moonraker, monkeypatch
):
    """If the second read fails we know nothing about box_extras -- say that, not 'no Box'."""
    _, printer = moonraker
    printer.attach_box(1)
    _, rt = server_rt
    real_query = rt.client.query

    async def flaky(objects, *a, **kw):
        if "box_extras" in objects:
            raise ConnectionFailed("moonraker went away")
        return await real_query(objects, *a, **kw)

    monkeypatch.setattr(rt.client, "query", flaky)
    r = await call_tool("get_box_status")

    assert r["ok"] is True
    assert r["ready"] is False
    assert any("not proven" in p for p in r["problems"])
    assert not any("commented out" in p for p in r["problems"])


# --------------------------------------------------------------------------
# Capabilities: the multi_color_controller lie
# --------------------------------------------------------------------------


async def test_a_stock_max4_does_not_claim_to_have_a_box(call_tool):
    """Every Max4 declares [multi_color_controller], Box or not. It used to mean 'Box'."""
    r = await call_tool("get_capabilities", refresh=True)
    features = r["features"]
    assert features["multi_color_controller"] is True
    assert features["multi_color_box"] is False
    assert features["box_count"] == 0


async def test_capabilities_report_a_box_only_when_both_halves_exist(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("get_capabilities", refresh=True)
    assert r["features"]["multi_color_box"] is True
    assert r["features"]["box_count"] == 1


async def test_capabilities_warn_when_the_count_has_no_live_object(call_tool, moonraker):
    _, printer = moonraker
    printer.saved_variables["box_count"] = 1
    r = await call_tool("get_capabilities", refresh=True)
    assert r["features"]["multi_color_box"] is False
    assert any("box_extras" in w for w in r["warnings"])


# --------------------------------------------------------------------------
# load_box_slot
# --------------------------------------------------------------------------


async def _token(call_tool, **kw):
    first = await call_tool("load_box_slot", confirm=True, **kw)
    assert first["ok"] is False and first.get("confirm_token"), first
    return first


async def test_load_box_slot_on_a_printer_without_a_box_is_refused(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("load_box_slot", slot=0, confirm=True)
    assert r["ok"] is False
    assert r["error"] == "not_supported"
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_load_box_slot_is_two_step_and_sends_exactly_e_load(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)

    first = await _token(call_tool, slot=0, hotend_temp=230)
    assert first["will_send"] == "E_LOAD SLOT=0 S=230"
    assert first["verification"]  # the not-run-on-hardware caveat travels with the ticket
    assert not any("E_LOAD" in c for c in printer.commands)  # a ticket sends nothing

    second = await call_tool(
        "load_box_slot", slot=0, hotend_temp=230, confirm=True,
        confirm_token=first["confirm_token"],
    )
    assert second["ok"] is True
    assert second["next_stage"] == "push"
    assert [c for c in printer.commands if "E_LOAD" in c] == ["E_LOAD SLOT=0 S=230"]


async def test_load_box_slot_ticket_is_bound_to_the_slot(call_tool, moonraker):
    """A token issued for slot 0 must not be spendable on slot 3."""
    _, printer = moonraker
    printer.attach_box(1)
    first = await _token(call_tool, slot=0)

    r = await call_tool(
        "load_box_slot", slot=3, confirm=True, confirm_token=first["confirm_token"]
    )
    assert r["ok"] is False
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_load_box_slot_without_confirm_sends_nothing(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("load_box_slot", slot=0)
    assert r["ok"] is False
    assert not any("E_LOAD" in c for c in printer.commands)


@pytest.mark.parametrize("slot", [-1, 4, 16, 99])
async def test_load_box_slot_rejects_slots_the_installed_boxes_do_not_have(
    call_tool, moonraker, slot
):
    _, printer = moonraker
    printer.attach_box(1)  # slots 0-3 only
    r = await call_tool("load_box_slot", slot=slot, confirm=True)
    assert r["ok"] is False
    assert r["error"] == "invalid_request"
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_a_second_box_makes_its_slots_valid(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(2)
    first = await _token(call_tool, slot=7)
    assert first["will_send"].startswith("E_LOAD SLOT=7 ")


async def test_load_box_slot_rejects_a_boolean_slot(call_tool, moonraker):
    """True == 1 in Python; a bool must not quietly select slot 1."""
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("load_box_slot", slot=True, confirm=True)
    assert r["ok"] is False


@pytest.mark.parametrize("temp", [20.0, 100.0, 149.0, 500.0])
async def test_load_box_slot_rejects_temperatures_that_cannot_feed(call_tool, moonraker, temp):
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("load_box_slot", slot=0, hotend_temp=temp, confirm=True)
    assert r["ok"] is False
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_load_box_slot_is_refused_while_printing(call_tool, moonraker, claim_printer):
    _, printer = moonraker
    printer.attach_box(1)
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    first = await call_tool("load_box_slot", slot=0, confirm=True)
    assert first["ok"] is False
    if first.get("confirm_token"):
        # Even a valid ticket must not get through the print interlock.
        first = await call_tool(
            "load_box_slot", slot=0, confirm=True, confirm_token=first["confirm_token"]
        )
        assert first["ok"] is False
    assert first["error"] == "interlock_blocked"
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_load_box_slot_refuses_a_box_the_firmware_would_ignore(call_tool, moonraker):
    """box_count set but no box_extras: QIDI's macros treat this as no Box, so we do too."""
    _, printer = moonraker
    printer.saved_variables["box_count"] = 1
    r = await call_tool("load_box_slot", slot=0, confirm=True)
    assert r["ok"] is False
    assert r["error"] == "interlock_blocked"
    assert r["blockers"]
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_load_box_slot_refuses_a_disabled_box(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["enable_box"] = 0
    r = await call_tool("load_box_slot", slot=0, confirm=True)
    assert r["ok"] is False and r["error"] == "interlock_blocked"


async def test_load_box_slot_waits_out_a_tool_change(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.box_extras = {"is_tool_change": 1}
    r = await call_tool("load_box_slot", slot=0, confirm=True)
    assert r["ok"] is False and r["error"] == "interlock_blocked"
    assert any("tool change" in b for b in r["blockers"])


async def test_the_ticket_warns_about_an_empty_slot_and_a_slot_already_in_the_extruder(
    call_tool, moonraker
):
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["slot_sync"] = "slot0"
    printer.filament_detected = True

    first = await _token(call_tool, slot=1)  # slot 1 is empty; slot 0 is in the extruder
    warnings = " ".join(first["warnings"])
    assert "slot 1 as empty" in warnings
    assert "slot 0 is already synced" in warnings
    assert "toolhead filament sensor" in warnings


async def test_load_box_slot_refuses_when_the_printer_does_not_list_e_load(call_tool, moonraker):
    """The first real Max4 Box had MULTI_COLOR_LOAD and no E_LOAD: say so, send nothing.

    A published command name that the printer itself does not list is not a guess worth
    sending on a DESTRUCTIVE tool, with or without a token.
    """
    _, printer = moonraker
    printer.attach_box(1)
    printer.extra_objects = [o for o in printer.extra_objects if "E_LOAD" not in o]
    printer.extra_help["MULTI_COLOR_LOAD"] = "Load material into the extruder"
    await call_tool("get_capabilities", refresh=True)

    r = await call_tool("load_box_slot", slot=0, hotend_temp=230)
    assert r["error"] == "not_supported"
    assert "E_LOAD" in r["message"]
    assert r["commands_available"]["E_LOAD"] is False
    assert r["commands_available"]["MULTI_COLOR_LOAD"] is True

    r = await call_tool("load_box_slot", slot=0, hotend_temp=230, confirm=True, confirm_token="x")
    assert r["error"] == "not_supported"
    assert not any("E_LOAD" in c for c in printer.commands)


async def test_get_box_status_reports_the_multi_color_commands_a_real_box_exposes(
    call_tool, moonraker
):
    _, printer = moonraker
    printer.attach_box(1)
    printer.extra_help.update(
        {"MULTI_COLOR_LOAD": "x", "MULTI_COLOR_DISABLE_HEATER": "x", "QUERY_MULTI_COLOR": "x"}
    )
    await call_tool("get_capabilities", refresh=True)
    avail = (await call_tool("get_box_status"))["commands_available"]
    assert avail["MULTI_COLOR_LOAD"] is True
    assert avail["MULTI_COLOR_DISABLE_HEATER"] is True
    assert avail["MULTI_COLOR_UNLOAD"] is False


# --------------------------------------------------------------------------
# set_box_option
# --------------------------------------------------------------------------


async def test_set_box_option_needs_a_detected_box(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_box_option", option="auto_read_rfid", enabled=False, confirm=True)
    assert r["ok"] is False and r["error"] == "not_supported"
    assert not any("SAVE_VARIABLE" in c for c in printer.commands)


async def test_set_box_option_needs_confirm_in_both_directions(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    for enabled in (True, False):
        r = await call_tool("set_box_option", option="auto_reload_detect", enabled=enabled)
        assert r["ok"] is False
    assert not any("SAVE_VARIABLE" in c for c in printer.commands)


async def test_set_box_option_writes_reads_back_and_reports(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["auto_reload_detect"] = 1

    r = await call_tool("set_box_option", option="auto_reload_detect", enabled=False, confirm=True)
    assert r["ok"] is True
    assert r["applied"] is True and r["stored_value"] == 0
    assert printer.saved_variables["auto_reload_detect"] == 0
    assert [c for c in printer.commands if "SAVE_VARIABLE" in c] == [
        "SAVE_VARIABLE VARIABLE=auto_reload_detect VALUE=0"
    ]

    r = await call_tool("set_box_option", option="auto_reload_detect", enabled=True, confirm=True)
    assert r["applied"] is True and printer.saved_variables["auto_reload_detect"] == 1


async def test_set_box_option_reports_when_the_value_did_not_stick(
    call_tool, moonraker, server_rt, monkeypatch
):
    """A command the firmware accepts but ignores must not be reported as a success."""
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["enable_box"] = 1
    _, rt = server_rt
    real_gcode = rt.client.gcode

    async def swallow(script, *a, **kw):
        if "SAVE_VARIABLE" in script:
            return None  # accepted, never applied
        return await real_gcode(script, *a, **kw)

    monkeypatch.setattr(rt.client, "gcode", swallow)
    r = await call_tool("set_box_option", option="enable_box", enabled=False, confirm=True)
    assert r["ok"] is True
    assert r["applied"] is False
    assert "does not match" in r["note"]


@pytest.mark.parametrize(
    "option",
    [
        "box_count",  # registered by the Box's own detection, never by us
        "auto_init_detect",  # effect unpublished: read-only on purpose
        "z_offset",
        "slot0",
        "last_load_slot",
        "enable_box; M112",
        "enable_box VALUE=1\nM112",
        "",
    ],
)
async def test_set_box_option_is_an_allow_list(call_tool, moonraker, option):
    """SAVE_VARIABLE is denied to run_gcode; this tool is the only way in, so it is narrow."""
    _, printer = moonraker
    printer.attach_box(1)
    before = dict(printer.saved_variables)
    r = await call_tool("set_box_option", option=option, enabled=True, confirm=True)
    assert r["ok"] is False and r["error"] == "invalid_request"
    assert printer.saved_variables == before
    assert not any("SAVE_VARIABLE" in c for c in printer.commands)


async def test_set_box_option_is_refused_while_printing(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    r = await call_tool("set_box_option", option="enable_box", enabled=False, confirm=True)
    assert r["ok"] is False and r["error"] == "interlock_blocked"
    assert printer.saved_variables["enable_box"] == 1


async def test_run_gcode_still_cannot_write_saved_variables(call_tool, moonraker):
    """Adding a write path must not widen the raw one."""
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool(
        "run_gcode", script="SAVE_VARIABLE VARIABLE=box_count VALUE=0", confirm=True
    )
    assert r["ok"] is False
    assert printer.saved_variables["box_count"] == 1


# --------------------------------------------------------------------------
# cooldown and the Box heater
# --------------------------------------------------------------------------


async def test_cooldown_leaves_the_box_heater_alone_by_default(call_tool, moonraker):
    """A filament-drying run is hours long; finishing a print must not end it."""
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("cooldown")
    assert r["ok"] is True
    assert r["box_heater"] is None
    assert printer.box_heater_disables == 0


async def test_cooldown_can_also_stop_the_box_heater_when_asked(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    r = await call_tool("cooldown", also_box_heater=True)
    assert r["ok"] is True
    assert r["box_heater"] == {"requested": True, "off": True, "command": "DISABLE_BOX_HEATER"}
    assert printer.box_heater_disables == 1
    assert printer.extruder_target == 0.0


async def test_a_refused_box_heater_command_does_not_undo_the_cooldown(
    call_tool, moonraker, server_rt, monkeypatch
):
    """On a printer with no Box heater macro the cooldown must still succeed."""
    from qidi_mcp.errors import QidiError

    _, printer = moonraker
    printer.extruder_target = 200.0
    _, rt = server_rt
    real_gcode = rt.client.gcode

    async def refuse_box_heater(script, *a, **kw):
        if script == "DISABLE_BOX_HEATER":
            raise QidiError("Unknown command: DISABLE_BOX_HEATER")
        return await real_gcode(script, *a, **kw)

    monkeypatch.setattr(rt.client, "gcode", refuse_box_heater)
    r = await call_tool("cooldown", also_box_heater=True)

    assert r["ok"] is True
    assert printer.extruder_target == 0.0
    assert r["box_heater"]["off"] is False
    assert "DISABLE_BOX_HEATER" in r["box_heater"]["error"]


async def test_cooldown_uses_the_box_heater_command_the_printer_actually_has(
    call_tool, moonraker
):
    """A real Max4 Box listed MULTI_COLOR_DISABLE_HEATER and no DISABLE_BOX_HEATER."""
    _, printer = moonraker
    printer.attach_box(1)
    printer.extra_help.pop("DISABLE_BOX_HEATER")
    printer.extra_help["MULTI_COLOR_DISABLE_HEATER"] = "Disable the Box heater"
    r = await call_tool("cooldown", also_box_heater=True)
    assert r["box_heater"] == {
        "requested": True,
        "off": True,
        "command": "MULTI_COLOR_DISABLE_HEATER",
    }
    assert "MULTI_COLOR_DISABLE_HEATER" in r["commands_sent"]
    assert not any(c == "DISABLE_BOX_HEATER" for c in printer.commands)


async def test_cooldown_sends_no_box_heater_command_the_printer_does_not_list(
    call_tool, moonraker
):
    _, printer = moonraker
    printer.attach_box(1)
    printer.extra_help.pop("DISABLE_BOX_HEATER")
    r = await call_tool("cooldown", also_box_heater=True)
    assert r["ok"] is True
    assert r["box_heater"]["off"] is False
    assert "neither" in r["box_heater"]["error"]
    assert printer.box_heater_disables == 0
    assert printer.extruder_target == 0.0


# --------------------------------------------------------------------------
# start_print ticket and file metadata
# --------------------------------------------------------------------------


async def test_start_print_ticket_has_no_box_section_on_a_stock_printer(
    call_tool, claim_printer
):
    await claim_printer("benchy")
    first = await call_tool("start_print", filename="benchy.gcode", confirm=True)
    assert first.get("confirm_token")
    assert "box" not in first


async def test_start_print_ticket_shows_what_is_in_the_box(call_tool, moonraker, claim_printer):
    _, printer = moonraker
    printer.attach_box(1)
    await claim_printer("benchy")
    first = await call_tool("start_print", filename="benchy.gcode", confirm=True)

    box = first["box"]
    assert box["detected"] is True and box["ready"] is True
    assert [s["slot"] for s in box["loaded_slots"]] == [0]
    assert box["loaded_slots"][0]["filament"] == "PLA Matte"
    assert box["loaded_slots"][0]["color_hex"] == "#060606"


async def test_a_broken_box_read_never_blocks_the_print_ticket(
    call_tool, moonraker, server_rt, claim_printer, monkeypatch
):
    """The Box section is a convenience. It must not be able to stop a print."""
    _, printer = moonraker
    printer.attach_box(1)
    _, rt = server_rt
    real_query = rt.client.query

    async def boom(objects, *a, **kw):
        if "box_extras" in objects:
            raise ConnectionFailed("nope")
        return await real_query(objects, *a, **kw)

    monkeypatch.setattr(rt.client, "query", boom)
    await claim_printer("benchy")
    first = await call_tool("start_print", filename="benchy.gcode", confirm=True)
    assert first.get("confirm_token")


async def test_file_metadata_exposes_the_per_filament_lists(call_tool, moonraker):
    _, printer = moonraker
    printer.files["benchy.gcode"].update(
        filament_colors=["#FF0000", "#00FF00"],
        filament_temps=[210, 230],
        referenced_tools=[0, 1],
    )
    r = await call_tool("get_file_metadata", filename="benchy.gcode")
    assert r["filament_colors"] == ["#FF0000", "#00FF00"]
    assert r["filament_temps_c"] == [210, 230]
    assert r["referenced_tools"] == [0, 1]


async def test_blockers_also_say_when_e_load_is_missing(call_tool, moonraker):
    """Don't make someone enable the Box and only then learn loading isn't offered."""
    _, printer = moonraker
    printer.attach_box(1)
    printer.saved_variables["enable_box"] = 0
    printer.extra_objects = [o for o in printer.extra_objects if "E_LOAD" not in o]
    await call_tool("get_capabilities", refresh=True)
    r = await call_tool("load_box_slot", slot=0)
    assert r["error"] == "interlock_blocked"
    assert "E_LOAD" in r["also"]
