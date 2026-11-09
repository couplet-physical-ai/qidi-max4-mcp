"""Functional tests: every tool works against the mock and reports honestly.

The point of this file is coverage of the *happy path* plus the "printer doesn't
have that" path, so an agent never gets a confident-sounding lie. Safety
behaviour is tested in test_safety.py.
"""

from __future__ import annotations

import pytest

# --------------------------------------------------------------------------
# Registration & contract
# --------------------------------------------------------------------------


async def test_every_tool_is_registered(server_rt):
    server, _ = server_rt
    tools = await server.list_tools()
    names = {t.name for t in tools}
    # A representative slice across all ten modules.
    for expected in [
        "get_printer_status", "get_capabilities", "get_temperatures", "set_temperature",
        "home_axes", "move_absolute", "start_print", "list_gcode_files", "upload_gcode",
        "set_fan_speed", "bed_mesh_calibrate", "get_webcam_snapshot", "emergency_stop",
        "run_gcode", "chamber_heater", "load_filament", "get_box_status",
    ]:
        assert expected in names, f"{expected} is missing"
    assert len(tools) >= 70


async def test_every_tool_has_a_description_and_annotations(server_rt):
    server, _ = server_rt
    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 40, f"{tool.name}'s description is too thin"
        assert tool.annotations is not None, f"{tool.name} has no annotations"


async def test_read_tools_are_marked_read_only(server_rt):
    """An agent must be able to tell a safe call from an unsafe one by annotation."""
    server, _ = server_rt
    tools = {t.name: t for t in await server.list_tools()}
    for name in [
        "get_printer_status", "get_temperatures", "get_position", "list_gcode_files",
        "get_capabilities", "get_bed_mesh", "query_endstops", "get_klipper_log",
    ]:
        assert tools[name].annotations.read_only_hint is True, f"{name} should be read-only"
    for name in ["delete_file", "emergency_stop", "reboot_printer", "run_gcode"]:
        assert tools[name].annotations.destructive_hint is True, f"{name} should be destructive"


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


async def test_get_printer_status_reports_the_whole_picture(call_tool):
    r = await call_tool("get_printer_status")
    assert r["ok"] is True
    assert r["klipper"]["state"] == "ready"
    assert r["job"]["state"] == "standby"
    assert r["temperatures"]["extruder"]["temperature"] == pytest.approx(24.5)
    assert r["temperatures"]["chamber"]["temperature"] == pytest.approx(25.1)
    assert r["motion"]["homed_axes"] == ""


async def test_get_capabilities_discovers_max4_hardware(call_tool):
    r = await call_tool("get_capabilities")
    assert r["ok"] is True
    assert r["printer"]["kinematics"] == "corexy"
    assert r["printer"]["axis_limits_mm"]["x"]["max"] == pytest.approx(392.0)
    assert r["printer"]["axis_limits_mm"]["y"]["max"] == pytest.approx(410.0)
    assert r["printer"]["axis_limits_mm"]["z"]["max"] == pytest.approx(342.0)
    feats = r["features"]
    assert feats["bed_mesh"] is True
    assert feats["z_tilt"] is True
    assert feats["probe"] is True
    assert feats["resonance_tester"] is True
    assert feats["exclude_object"] is True
    # The heated chamber is the Max4's defining feature.
    assert r["thermal"]["heated_chamber"] is True
    assert r["thermal"]["heaters"]["chamber"]["max_temp"] == pytest.approx(68.0)
    # QIDI's private Klipper modules must be surfaced, not hidden.
    assert any("closed_loop" in m for m in r["qidi_specific_klipper_modules"])


async def test_capabilities_reports_missing_moonraker_components(call_tool):
    """QIDI's fork has no update_manager/spoolman; the agent must be told."""
    r = await call_tool("get_capabilities")
    components = r["moonraker_components"]
    assert "update_manager" not in components
    assert "history" in components
    assert "timelapse" in components


async def test_get_temperatures(call_tool):
    r = await call_tool("get_temperatures")
    assert r["ok"] is True
    assert r["heaters"]["extruder"]["temperature_c"] == pytest.approx(24.5)
    assert r["heaters"]["heater_bed"]["temperature_c"] == pytest.approx(23.8)
    assert "Chamber_Thermal_Protection_Sensor" in str(r)


async def test_get_position(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    printer.position = [100.0, 200.0, 30.0, 0.0]
    r = await call_tool("get_position")
    assert r["ok"] is True
    assert r["gcode_position"][0] == pytest.approx(100.0)
    assert r["homed_axes"] == "xyz"


async def test_get_job_status_idle(call_tool):
    r = await call_tool("get_job_status")
    assert r["ok"] is True
    assert r["state"] == "standby"
    assert r["is_active"] is False


async def test_get_job_status_printing(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    printer.progress = 0.42
    r = await call_tool("get_job_status")
    assert r["is_active"] is True
    assert r["filename"] == "benchy.gcode"
    assert r["progress_percent"] == pytest.approx(42.0)


async def test_get_safety_policy_is_transparent(call_tool):
    r = await call_tool("get_safety_policy")
    assert r["ok"] is True
    assert r["temperature_limits_c"]["extruder"]["max"] <= 375.0
    assert r["mode"]["read_only"] is False
    assert r["hardware_ceilings_c"]["extruder"] == pytest.approx(375.0)
    assert r["risk_tiers"], "the agent must be able to read the risk model"


async def test_list_gcode_commands(call_tool):
    r = await call_tool("list_gcode_commands")
    assert r["ok"] is True
    assert r["total_matching"] > 20


async def test_list_gcode_commands_finds_qidi_macros(call_tool):
    r = await call_tool("list_gcode_commands", search="M60")
    text = str(r)
    assert "M603" in text and "M604" in text


async def test_query_objects_arbitrary_access(call_tool):
    r = await call_tool("query_objects", objects=["toolhead", "extruder"])
    assert r["ok"] is True
    assert "toolhead" in r["status"]


async def test_get_gcode_log(call_tool, moonraker):
    _, printer = moonraker
    printer.log("test message")
    r = await call_tool("get_gcode_log")
    assert r["ok"] is True
    assert any("test message" in str(e) for e in r["entries"])


async def test_get_system_info(call_tool):
    r = await call_tool("get_system_info")
    assert r["ok"] is True
    assert "debian" in str(r).lower() or "cpu" in str(r).lower()


async def test_get_temperature_history(call_tool):
    r = await call_tool("get_temperature_history")
    assert r["ok"] is True
    assert "extruder" in r["history"]


async def test_get_printer_config(call_tool):
    listing = await call_tool("get_printer_config")
    assert listing["ok"] is True
    assert "extruder" in listing["sections"]
    r = await call_tool("get_printer_config", section="extruder")
    assert r["ok"] is True
    assert r["settings"]["max_temp"] == pytest.approx(375.0)


async def test_get_audit_log_records_actions(call_tool):
    await call_tool("home_axes", confirm=True)
    r = await call_tool("get_audit_log")
    assert r["ok"] is True
    assert any(e.get("tool") == "home_axes" for e in r["entries"])


# --------------------------------------------------------------------------
# Thermal
# --------------------------------------------------------------------------


async def test_set_temperature_bed(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_temperature", heater="bed", target=60, confirm=True)
    assert r["ok"] is True
    assert printer.bed_target == pytest.approx(60.0)


async def test_set_temperature_uses_qidi_macros(call_tool, moonraker):
    """QIDI override M104/M140 with scaling macros; we must go through them."""
    _, printer = moonraker
    await call_tool("set_temperature", heater="extruder", target=220, confirm=True)
    assert any(c.startswith("M104") for c in printer.commands)


async def test_cooldown_turns_everything_off(call_tool, moonraker):
    _, printer = moonraker
    printer.extruder_target, printer.bed_target, printer.chamber_target = 250.0, 100.0, 50.0
    r = await call_tool("cooldown", confirm=True)
    assert r["ok"] is True
    assert printer.extruder_target == 0.0
    assert printer.bed_target == 0.0


async def test_unknown_heater_is_reported_clearly(call_tool):
    r = await call_tool("set_temperature", heater="nozzle2", target=200, confirm=True)
    assert r["ok"] is False
    assert "nozzle2" in str(r)


# --------------------------------------------------------------------------
# Motion
# --------------------------------------------------------------------------


async def test_home_axes_all(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("home_axes", confirm=True)
    assert r["ok"] is True
    assert printer.homed_axes == "xyz"


async def test_home_single_axis(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("home_axes", axes="z", confirm=True)
    assert r["ok"] is True
    assert "G28" in " ".join(printer.commands)


async def test_move_relative(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    printer.position = [100.0, 100.0, 10.0, 0.0]
    r = await call_tool("move_relative", dx=10, confirm=True)
    assert r["ok"] is True
    assert any("G91" in c or "G1" in c for c in printer.commands)


async def test_motion_wraps_state_in_save_restore(call_tool, moonraker):
    """Relative moves must not leak G91 into a later absolute command."""
    _, printer = moonraker
    printer.homed_axes = "xyz"
    await call_tool("move_relative", dx=5, confirm=True)
    sent = "\n".join(printer.commands)
    assert "SAVE_GCODE_STATE" in sent and "RESTORE_GCODE_STATE" in sent


async def test_extrude_filament_when_hot(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    printer.extruder_temp = 240.0
    r = await call_tool("extrude_filament", distance=20, confirm=True)
    assert r["ok"] is True


async def test_set_flow_factor(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_flow_factor", percent=95, confirm=True)
    assert r["ok"] is True
    assert printer.extrude_factor == pytest.approx(0.95)


async def test_motors_off(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    r = await call_tool("motors_off", confirm=True)
    assert r["ok"] is True
    assert printer.homed_axes == ""


async def test_z_offset_absolute(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    r = await call_tool("set_z_offset", offset=-0.1, mode="set", move=False, confirm=True)
    assert r["ok"] is True
    assert printer.z_offset == pytest.approx(-0.1)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


async def test_start_print_previews_before_starting(call_tool, moonraker, claim_printer):
    _, printer = moonraker
    await claim_printer("benchy")
    first = await call_tool("start_print", filename="benchy.gcode", confirm=True)
    assert first["ok"] is False
    assert first.get("confirm_token")
    # The preview must carry real metadata so the agent can sanity-check it.
    assert "PLA" in str(first) or "filament" in str(first).lower()
    assert printer.print_state == "standby"

    second = await call_tool(
        "start_print", filename="benchy.gcode", confirm=True,
        confirm_token=first["confirm_token"],
    )
    assert second["ok"] is True
    assert printer.print_state == "printing"


async def test_start_print_rejects_missing_file(call_tool):
    r = await call_tool("start_print", filename="nope.gcode", confirm=True)
    assert r["ok"] is False


async def test_pause_and_resume(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    p = await call_tool("pause_print", confirm=True)
    assert p["ok"] is True
    assert printer.print_state == "paused"
    r = await call_tool("resume_print", confirm=True)
    assert r["ok"] is True
    assert printer.print_state == "printing"


async def test_cancel_print_is_two_step(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    first = await call_tool("cancel_print", confirm=True)
    assert first["ok"] is False
    assert first.get("confirm_token")
    assert printer.print_state == "printing"
    second = await call_tool(
        "cancel_print", confirm=True, confirm_token=first["confirm_token"]
    )
    assert second["ok"] is True
    assert printer.print_state == "cancelled"


async def test_pause_when_not_printing_is_refused(call_tool):
    r = await call_tool("pause_print", confirm=True)
    assert r["ok"] is False


async def test_print_history(call_tool):
    r = await call_tool("get_print_history")
    assert r["ok"] is True
    assert len(r["jobs"]) == 2
    # Newest first, which is Moonraker's default order.
    assert r["jobs"][0]["filename"] == "projects/bracket_abs.gcode"


async def test_history_totals(call_tool):
    r = await call_tool("get_history_totals")
    assert r["ok"] is True
    assert r["job_totals"]["total_jobs"] == 42


async def test_job_queue(call_tool):
    r = await call_tool("get_job_queue")
    assert r["ok"] is True
    # 'paused', not 'ready': measured on the real Max 4, whose queue ships paused.
    # The mock used to answer 'ready', which quietly inverted the one distinction
    # that matters here -- a live queue starts the next job on its own, so adding
    # to it is a print start and is gated as one.
    assert r["queue_state"] == "paused"


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


async def test_list_gcode_files(call_tool):
    r = await call_tool("list_gcode_files")
    assert r["ok"] is True
    assert r["total_matching"] == 2


async def test_file_metadata_is_rich(call_tool):
    r = await call_tool("get_file_metadata", filename="benchy.gcode")
    assert r["ok"] is True
    assert r["filament_type"] == "PLA"
    assert r["estimated_time_s"] == pytest.approx(5820.0)
    assert r["layer_count"] == 240


async def test_upload_gcode(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("upload_gcode", filename="test.gcode", content="G28\nG1 X10\n")
    assert r["ok"] is True
    assert printer.uploads
    assert printer.uploads[0]["filename"] == "test.gcode"


async def test_upload_never_starts_a_print(call_tool, moonraker):
    """Upload and print must stay separate decisions."""
    _, printer = moonraker
    await call_tool("upload_gcode", filename="test.gcode", content="G28\n")
    assert printer.uploads[0]["fields"].get("print") in (None, "false")
    assert printer.print_state == "standby"


async def test_upload_rejects_path_traversal(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("upload_gcode", filename="../../etc/passwd", content="x")
    assert r["ok"] is False
    assert not printer.uploads


async def test_download_file(call_tool):
    r = await call_tool("download_file", path="benchy.gcode", root="gcodes", max_bytes=200)
    assert r["ok"] is True
    assert "G28" in r["content"]


async def test_list_file_roots(call_tool):
    r = await call_tool("list_file_roots")
    assert r["ok"] is True
    assert any(root["name"] == "gcodes" for root in r["roots"])


async def test_list_directory(call_tool):
    r = await call_tool("list_directory")
    assert r["ok"] is True
    assert "disk_usage" in r or "files" in r


async def test_move_and_copy_file(call_tool):
    m = await call_tool("move_file", source="benchy.gcode", dest="archive/benchy.gcode",
                        confirm=True)
    assert m["ok"] is True
    c = await call_tool("copy_file", source="benchy.gcode", dest="copy.gcode", confirm=True)
    assert c["ok"] is True


async def test_create_directory(call_tool):
    r = await call_tool("create_directory", path="newdir", confirm=True)
    assert r["ok"] is True


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


async def test_list_fans_includes_all_max4_fans(call_tool):
    r = await call_tool("list_fans")
    assert r["ok"] is True
    names = set(r["fans"])
    for expected in ["cooling_fan", "auxiliary_cooling_fan", "chamber_circulation_fan"]:
        assert expected in names, f"{expected} missing from {names}"


async def test_firmware_managed_fans_are_marked_unsettable(call_tool):
    r = await call_tool("list_fans")
    hotend = r["fans"].get("hotend_fan")
    assert hotend is not None
    assert hotend["settable"] is False


async def test_set_part_fan(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_fan_speed", speed=0.5)
    assert r["ok"] is True
    assert printer.fan_speed == pytest.approx(0.5, abs=0.01)


async def test_set_named_fan(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_fan_speed", fan="chamber_circulation_fan", speed=0.7)
    assert r["ok"] is True
    assert printer.circulation_fan_speed == pytest.approx(0.7)


async def test_setting_a_firmware_fan_is_refused(call_tool):
    r = await call_tool("set_fan_speed", fan="hotend_fan", speed=0.5)
    assert r["ok"] is False
    assert r["error"] == "not_supported"


async def test_fan_speed_out_of_range_is_refused(call_tool):
    for bad in (-0.5, 1.5, 255):
        r = await call_tool("set_fan_speed", speed=bad)
        assert r["ok"] is False, f"{bad} should be refused"


async def test_case_light(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_case_light", on=False)
    assert r["ok"] is True
    assert printer.caselight == 0.0


async def test_set_output_pin_rejects_unknown_pin(call_tool):
    r = await call_tool("set_output_pin", pin="not_a_pin", value=1)
    assert r["ok"] is False


async def test_polar_cooler(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_polar_cooler", on=True)
    assert r["ok"] is True
    assert printer.polar_cooler == 1.0


async def test_display_message_sanitises_input(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("display_message", message="hello\nM112 ; boom")
    assert r["ok"] is True
    sent = "\n".join(printer.commands)
    assert "M112" not in sent.replace("M117", "")  or "M117" in sent
    # The injected newline must not have created a second command line.
    assert not any(c.strip() == "M112" for c in printer.commands)


async def test_list_devices_inventory(call_tool):
    r = await call_tool("list_devices")
    assert r["ok"] is True
    assert "caselight" in r["output_pins"]
    assert "RGB" in r["leds"]


async def test_filament_sensor_query(call_tool):
    r = await call_tool("query_filament_sensor")
    assert r["ok"] is True
    assert list(r["sensors"].values())[0]["filament_detected"] is True


async def test_disabling_filament_sensor_is_gated(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_filament_sensor", enable=False)
    assert r["ok"] is False
    assert printer.filament_sensor_enabled is True


async def test_enabling_filament_sensor_is_free(call_tool, moonraker):
    _, printer = moonraker
    printer.filament_sensor_enabled = False
    r = await call_tool("set_filament_sensor", enable=True)
    assert r["ok"] is True
    assert printer.filament_sensor_enabled is True


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


async def test_get_bed_mesh_summarises_deviation(call_tool):
    r = await call_tool("get_bed_mesh")
    assert r["ok"] is True
    assert r["active_profile"] == "default"
    assert r["deviation_mm"]["range"] == pytest.approx(1.14, abs=0.01)


async def test_get_bed_mesh_matrix_on_request(call_tool):
    r = await call_tool("get_bed_mesh", include_matrix=True)
    assert r["probed_matrix"]


async def test_bed_mesh_calibrate_is_two_step(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("bed_mesh_calibrate", confirm=True)
    assert first.get("confirm_token")
    assert "what_will_happen" in first
    second = await call_tool(
        "bed_mesh_calibrate", confirm=True, confirm_token=first["confirm_token"]
    )
    assert second["ok"] is True
    assert any("BED_MESH_CALIBRATE" in c for c in printer.commands)


async def test_bed_mesh_profile_load(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("bed_mesh_profile", action="load", name="kamp", confirm=True)
    assert r["ok"] is True, r
    assert printer.active_mesh == "kamp"


async def test_bed_mesh_profile_save_is_two_step(call_tool):
    first = await call_tool("bed_mesh_profile", action="save", name="new", confirm=True)
    assert first.get("confirm_token")


async def test_z_tilt_adjust(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("z_tilt_adjust", confirm=True)
    assert r["ok"] is True
    assert any("Z_TILT_ADJUST" in c for c in printer.commands)


async def test_query_probe(call_tool):
    r = await call_tool("query_probe")
    assert r["ok"] is True
    assert r["probe"]["z_offset"] == pytest.approx(-0.05)


async def test_probe_accuracy_returns_statistics(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    r = await call_tool("probe_accuracy", samples=5, confirm=True)
    assert r["ok"] is True
    assert any("standard deviation" in str(m) for m in r["results"])


async def test_query_endstops(call_tool):
    r = await call_tool("query_endstops")
    assert r["ok"] is True
    assert r["endstops"]["z"] == "TRIGGERED"


async def test_input_shaper_calibrate_is_two_step_and_warns(call_tool):
    first = await call_tool("input_shaper_calibrate", confirm=True)
    assert first.get("confirm_token")
    assert "shak" in str(first).lower() or "loud" in str(first).lower()


async def test_set_input_shaper(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("set_input_shaper", freq_x=52.0, shaper_type_x="mzv", confirm=True)
    assert r["ok"] is True
    assert any("SET_INPUT_SHAPER" in c for c in printer.commands)


async def test_set_input_shaper_rejects_bad_type(call_tool):
    r = await call_tool("set_input_shaper", shaper_type_x="banana", confirm=True)
    assert r["ok"] is False


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------


async def test_list_webcams(call_tool):
    r = await call_tool("list_webcams")
    assert r["ok"] is True
    # The advertised URL must be the one measured to work on this firmware: the
    # nginx /webcam/ proxy on :80. crowsnest.conf claims :8080, but that port is
    # closed on the lab Max4 -- and trusting it once already produced a wrong
    # "the camera service is not running" diagnosis. Pin the verified URL so the
    # docs-derived guess cannot creep back in.
    urls = str(r["verified_urls"])
    assert "/webcam/?action=snapshot" in urls
    assert ":8080" not in urls


async def test_webcam_snapshot_returns_an_image(server_rt, moonraker):
    """A reachable camera must come back as a real image block, not JSON."""
    base_url, _ = moonraker
    server, _ = server_rt
    result = await server.call_tool(
        "get_webcam_snapshot", {"camera_url": f"{base_url}/?action=snapshot"}
    )
    blocks = getattr(result, "content", None) or []
    assert blocks, "no content returned"
    assert getattr(blocks[0], "type", None) == "image", getattr(blocks[0], "text", "")


async def test_webcam_snapshot_tries_every_candidate_then_explains(server_rt):
    """With no camera reachable, the agent must get a diagnosis, not a bare failure."""
    import json

    server, _ = server_rt
    result = await server.call_tool("get_webcam_snapshot", {})
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    if set(payload) == {"result"}:
        payload = payload["result"]
    assert payload["ok"] is False
    assert payload["error"] == "snapshot_unavailable"
    assert payload["attempted"], "must report which URLs it tried"
    assert "8080" in payload["hint"]


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------


async def test_run_macro_known(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("run_macro", macro="CLEAR_NOZZLE", confirm=True)
    token = first["confirm_token"]
    second = await call_tool(
        "run_macro", macro="CLEAR_NOZZLE", confirm=True, confirm_token=token
    )
    assert second["ok"] is True


async def test_run_macro_unknown_is_refused_with_suggestions(call_tool):
    r = await call_tool("run_macro", macro="NOT_A_REAL_MACRO", confirm=True)
    assert r["ok"] is False
    assert "NOT_A_REAL_MACRO" in str(r)


async def test_run_macro_rejects_injection_in_params(call_tool):
    r = await call_tool(
        "run_macro", macro="CLEAR_NOZZLE", params={"HOTEND": "250\nM112"}, confirm=True
    )
    assert r["ok"] is False


async def test_get_klipper_log(call_tool):
    r = await call_tool("get_klipper_log", max_bytes=2000)
    assert r["ok"] is True
    assert r["bytes_returned"] <= 2000
    assert "klippy" in r["content"]


async def test_get_moonraker_log(call_tool):
    r = await call_tool("get_klipper_log", which="moonraker", max_bytes=1000)
    assert r["ok"] is True


async def test_restart_service_validates_name(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("restart_service", service="not_a_service", confirm=True)
    assert r["ok"] is False
    assert printer.service_actions == []


async def test_restart_service_two_step(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("restart_service", service="crowsnest", confirm=True)
    token = first["confirm_token"]
    second = await call_tool(
        "restart_service", service="crowsnest", confirm=True, confirm_token=token
    )
    assert second["ok"] is True
    assert ("restart", "crowsnest") in printer.service_actions


async def test_reboot_and_shutdown_are_two_step(call_tool, moonraker):
    _, printer = moonraker
    r1 = await call_tool("reboot_printer", confirm=True)
    assert r1.get("confirm_token") and printer.reboots == 0
    r2 = await call_tool("shutdown_printer", confirm=True)
    assert r2.get("confirm_token") and printer.shutdowns == 0


# --------------------------------------------------------------------------
# QIDI-specific
# --------------------------------------------------------------------------


async def test_chamber_heater_reports_when_no_target_given(call_tool):
    r = await call_tool("chamber_heater")
    assert r["ok"] is True
    assert r["chamber_temperature_c"] == pytest.approx(25.1)
    assert r["policy_max_c"] <= 68.0


async def test_chamber_heater_sets_target(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("chamber_heater", target=50, confirm=True)
    assert r["ok"] is True
    assert printer.chamber_target == pytest.approx(50.0)


async def test_load_filament_stages(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("load_filament", stage="heat", hotend_temp=230, confirm=True)
    token = first["confirm_token"]
    second = await call_tool(
        "load_filament", stage="heat", hotend_temp=230, confirm=True, confirm_token=token
    )
    assert second["ok"] is True
    assert second["next_stage"] == "push"
    assert any("M604" in c and "T0" in c for c in printer.commands)


async def test_load_filament_rejects_cold_temp(call_tool):
    r = await call_tool("load_filament", stage="heat", hotend_temp=50, confirm=True)
    assert r["ok"] is False


async def test_unload_filament(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("unload_filament", hotend_temp=250, confirm=True)
    token = first["confirm_token"]
    second = await call_tool(
        "unload_filament", hotend_temp=250, confirm=True, confirm_token=token
    )
    assert second["ok"] is True
    assert any("M603" in c for c in printer.commands)


async def test_clean_nozzle(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("clean_nozzle", hotend_temp=240, confirm=True)
    assert r["ok"] is True
    assert any("CLEAR_NOZZLE" in c for c in printer.commands)


async def test_qidi_bed_level_is_two_step(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("qidi_bed_level_calibrate", bed_temp=60, confirm=True)
    assert first.get("confirm_token")
    second = await call_tool(
        "qidi_bed_level_calibrate", bed_temp=60, confirm=True,
        confirm_token=first["confirm_token"],
    )
    assert second["ok"] is True
    assert any("M4029" in c for c in printer.commands)


async def test_get_box_status_without_a_box(call_tool):
    r = await call_tool("get_box_status")
    assert r["ok"] is True
    assert r["box_count"] == 0
    assert r["slots"] == []
    assert "No Box" in r["note"]


async def test_get_box_status_with_a_box(call_tool, moonraker):
    _, printer = moonraker
    printer.saved_variables["box_count"] = 1
    printer.saved_variables["slot0"] = 1
    r = await call_tool("get_box_status")
    assert r["box_count"] == 1
    assert any(s["loaded"] for s in r["slots"])


async def test_get_saved_variables_groups_slots(call_tool):
    r = await call_tool("get_saved_variables")
    assert r["ok"] is True
    assert "z_offset" in r["variables"]
    assert r["slot_variables_count"] > 10


async def test_filament_drying_presets(call_tool):
    r = await call_tool("get_filament_presets")
    assert r["ok"] is True
    materials = {p["material"] for p in r["drying_profiles"]}
    assert "ABS" in materials and "PLA" in materials


# --------------------------------------------------------------------------
# Degraded printer: honesty checks
# --------------------------------------------------------------------------


async def test_klippy_shutdown_gives_a_clear_error(call_tool, moonraker):
    _, printer = moonraker
    printer.klippy_state = "shutdown"
    printer.klippy_message = "MCU 'mcu' shutdown: Timer too close"
    r = await call_tool("get_temperatures")
    assert r["ok"] is False
    assert r["error"] == "klippy_not_ready"
    assert r.get("hint")


async def test_status_still_works_when_klippy_is_down(call_tool, moonraker):
    """The agent must be able to diagnose a dead printer, not just fail."""
    _, printer = moonraker
    printer.klippy_state = "shutdown"
    printer.klippy_message = "MCU 'mcu' shutdown: Timer too close"
    r = await call_tool("get_printer_status")
    # Either a populated status showing the shutdown, or a structured error that
    # names the cause — both let the agent diagnose. A bare failure would not.
    assert "Timer too close" in str(r) or r.get("error") == "klippy_not_ready"
    assert r.get("hint") or r.get("klipper")


async def test_unreachable_printer_reports_connection_failure(moonraker):
    """A dead printer must produce a connection error, not a hang or a crash."""
    import json

    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server

    settings = load_settings(
        {
            "QIDI_PRINTER_URL": "http://127.0.0.1:9",  # discard port
            "QIDI_TEST_MODE": "1",
            "QIDI_TIMEOUT": "1",
        }
    )
    server, runtime = build_server(settings)
    try:
        result = await server.call_tool("get_printer_status", {})
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = json.loads(result.content[0].text)
        if set(payload) == {"result"}:
            payload = payload["result"]
        assert payload["ok"] is False
        assert payload["error"] in {"connection_failed", "klippy_not_ready"}
        assert payload.get("hint")
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------
# QIDI Moonraker fork extensions
#
# These routes exist only in QIDI's fork (verified against QIDI_MAX4's own
# moonraker/components/klippy_apis.py). They matter because they are SOFTER
# than an emergency stop: breakheater/breakmacro escape a stuck operation
# without the shutdown-plus-firmware-restart an e-stop forces.
# --------------------------------------------------------------------------


async def test_abort_heating_releases_a_stuck_wait(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("abort_heating")
    assert r["ok"] is True
    assert printer.break_heaters == 1
    # It must NOT be an emergency stop: the printer stays alive.
    assert printer.emergency_stops == 0
    assert printer.klippy_state == "ready"


async def test_abort_heating_leaves_targets_alone(call_tool, moonraker):
    """Releasing the wait is not the same as turning the heater off."""
    _, printer = moonraker
    printer.extruder_target = 250.0
    await call_tool("abort_heating")
    assert printer.extruder_target == 250.0
    assert printer.break_heaters == 1


async def test_abort_macro_needs_confirmation(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("abort_macro")
    assert first["ok"] is False
    assert printer.break_macros == 0

    second = await call_tool("abort_macro", confirm=True)
    assert second["ok"] is True
    assert printer.break_macros == 1
    assert printer.emergency_stops == 0
    assert "warning" in second


async def test_abort_macro_works_during_a_print(call_tool, moonraker):
    """A stuck macro mid-print is exactly when you need this."""
    _, printer = moonraker
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    r = await call_tool("abort_macro", confirm=True)
    assert r["ok"] is True
    assert printer.break_macros == 1


async def test_power_loss_resume_is_two_step_and_reports_interrupted_flag(call_tool, moonraker):
    _, printer = moonraker
    printer.saved_variables["was_interrupted"] = True
    first = await call_tool("resume_after_power_loss", confirm=True)
    assert first["ok"] is False
    assert first["was_interrupted"] is True
    assert printer.power_loss_resumes == 0

    second = await call_tool(
        "resume_after_power_loss", confirm=True, confirm_token=first["confirm_token"]
    )
    assert second["ok"] is True
    assert printer.power_loss_resumes == 1


async def test_power_loss_resume_warns_when_nothing_was_interrupted(call_tool, moonraker):
    """The agent must be able to tell there is nothing to recover before confirming."""
    _, printer = moonraker
    printer.saved_variables["was_interrupted"] = False
    first = await call_tool("resume_after_power_loss", confirm=True)
    assert first["was_interrupted"] is False
    assert "do not confirm" in first["what_will_happen"].lower()
    assert printer.power_loss_resumes == 0


async def test_qidi_fork_tools_degrade_on_stock_klipper(moonraker):
    """On a printer without QIDI's fork these routes 404; that must be reported
    as 'not supported', not as a crash or a silent success."""
    import json

    from aiohttp import web

    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server
    from tests.mock_moonraker import MockPrinter, build_app

    stock = MockPrinter()
    app = build_app(stock, qidi_fork=False)  # emulate upstream Moonraker
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    settings = load_settings(
        {"QIDI_PRINTER_URL": f"http://127.0.0.1:{port}", "QIDI_TEST_MODE": "1"}
    )
    server, runtime = build_server(settings)
    try:
        result = await server.call_tool("abort_heating", {})
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = json.loads(result.content[0].text)
        if set(payload) == {"result"}:
            payload = payload["result"]
        assert payload["ok"] is False
        assert payload["error"] in {"not_supported", "moonraker_http_error"}
        assert stock.break_heaters == 0
    finally:
        await runtime.aclose()
        await runner.cleanup()


async def test_capabilities_detects_the_qidi_fork_and_soft_abort(call_tool):
    """The agent must know a soft abort exists before reaching for the e-stop."""
    r = await call_tool("get_capabilities")
    assert r["features"]["soft_abort"] is True
    assert any("breakheater" in e for e in r["qidi_fork_endpoints"])
    assert "abort_heating" in r["abort_options"]["soft"]


async def test_capabilities_reports_real_max4_platform(call_tool):
    r = await call_tool("get_system_info")
    text = str(r)
    assert "Cortex-A35" in text or "aarch64" in text
    assert "bullseye" in text.lower()


async def test_run_gcode_refuses_line_numbered_dangerous_command(call_tool, moonraker):
    """End-to-end: the N-prefix bypass must be refused by the tool, not just the
    screen, and nothing may reach the printer."""
    _, printer = moonraker
    r = await call_tool("run_gcode", script="N1 SAVE_CONFIG", confirm=True)
    assert r["ok"] is False
    assert "SAVE_CONFIG" in str(r)
    assert not any("SAVE_CONFIG" in c.upper() for c in printer.commands)


async def test_run_gcode_screens_every_line_not_just_the_first(call_tool, moonraker):
    """A dangerous command hidden on line 3 (and line-numbered) must be caught."""
    _, printer = moonraker
    script = "G28\nM105\nN3 FORCE_MOVE STEPPER=stepper_x DISTANCE=50 VELOCITY=50\nM114"
    r = await call_tool("run_gcode", script=script, confirm=True)
    assert r["ok"] is False
    assert "FORCE_MOVE" in str(r)
    # The whole script is rejected atomically - no partial execution.
    assert printer.commands == []


async def test_run_gcode_reports_resolved_command_in_the_ticket(call_tool):
    """The confirmation ticket must show what Klipper will really run."""
    r = await call_tool("run_gcode", script="N10 G28", confirm=True)
    screened = r.get("screened_commands") or []
    assert any(s["command"] == "G28" for s in screened), r


async def test_run_macro_cannot_smuggle_a_second_command(call_tool, moonraker):
    """The macro name must be a single token, so no injection via name or params."""
    _, printer = moonraker
    for evil in [
        "G28\nSAVE_CONFIG",
        "N1 SAVE_CONFIG",
        "G28 ; SAVE_CONFIG",
        "SAVE_CONFIG",
    ]:
        r = await call_tool("run_macro", macro=evil, confirm=True)
        assert r["ok"] is False, f"{evil!r} was accepted"
    assert not any("SAVE_CONFIG" in c.upper() for c in printer.commands)


async def test_run_macro_rejects_newlines_in_parameters(call_tool, moonraker):
    """A param value must not be able to open a new G-code line."""
    _, printer = moonraker
    r = await call_tool(
        "run_macro", macro="CLEAR_NOZZLE", params={"HOTEND": "240\nSAVE_CONFIG"}, confirm=True
    )
    assert r["ok"] is False
    assert not any("SAVE_CONFIG" in c.upper() for c in printer.commands)


async def test_cooldown_turns_off_chamber_even_when_discovery_was_degraded(moonraker):
    """The poisoned-capability-cache bug.

    If the device inventory could not be read (Klipper restarting when discovery
    ran), the chamber heater looks absent. cooldown must still turn it off --
    it is the tool you reach for when you do not trust the machine's state.
    """
    import json

    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server

    base_url, printer = moonraker
    # Make discovery fail the way a restarting Klipper does.
    printer.klippy_state = "startup"
    settings = load_settings({"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1"})
    server, runtime = build_server(settings)
    try:
        caps = await runtime.capabilities()
        assert caps.degraded is True, "discovery should be flagged degraded"
        assert caps.has_heated_chamber is False  # the poisoning

        # Now Klipper is ready and the chamber is hot.
        printer.klippy_state = "ready"
        printer.chamber_target = 60.0
        printer.chamber_temp = 55.0

        result = await server.call_tool("cooldown", {})
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = json.loads(result.content[0].text)
        if set(payload) == {"result"}:
            payload = payload["result"]
        assert payload["ok"] is True
        assert printer.chamber_target == 0.0, (
            f"chamber left at {printer.chamber_target} C - cooldown trusted a degraded cache"
        )
        assert printer.extruder_target == 0.0
        assert printer.bed_target == 0.0
    finally:
        await runtime.aclose()


async def test_turning_a_heater_off_is_never_refused_for_capability_reasons(moonraker):
    """set_temperature(chamber, 0) must work even if the chamber looks absent."""
    import json

    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server

    base_url, printer = moonraker
    printer.klippy_state = "startup"
    settings = load_settings({"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1"})
    server, runtime = build_server(settings)
    try:
        caps = await runtime.capabilities()
        assert caps.has_heated_chamber is False
        printer.klippy_state = "ready"
        printer.chamber_target = 60.0

        result = await server.call_tool(
            "set_temperature", {"heater": "chamber", "target": 0}
        )
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = json.loads(result.content[0].text)
        if set(payload) == {"result"}:
            payload = payload["result"]
        assert payload["ok"] is True, f"refused to turn a heater OFF: {payload}"
        assert printer.chamber_target == 0.0

        # Heating it, by contrast, is still correctly refused.
        result2 = await server.call_tool(
            "set_temperature", {"heater": "chamber", "target": 50, "confirm": True}
        )
        payload2 = getattr(result2, "structuredContent", None)
        if payload2 is None:
            payload2 = json.loads(result2.content[0].text)
        if set(payload2) == {"result"}:
            payload2 = payload2["result"]
        assert payload2["ok"] is False
        assert payload2["error"] == "not_supported"
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------
# A black frame must never reach the agent as an image
#
# Regression guard for 2026-09-16: three unlit-chamber snapshots were returned as
# valid images, an agent described their contents in detail, and a print that
# extruded nothing was reported as a success. The camera is only useful if a frame
# that cannot show anything is refused instead of returned.
# --------------------------------------------------------------------------


def test_jpeg_dimensions_reads_the_frame_header():
    from qidi_mcp.tools.camera import _jpeg_dimensions
    from tests.mock_moonraker import dark_jpeg

    assert _jpeg_dimensions(dark_jpeg(1920, 1080)) == (1920, 1080)
    assert _jpeg_dimensions(dark_jpeg(640, 480)) == (640, 480)


def test_jpeg_dimensions_fails_open_on_anything_it_cannot_parse():
    """Unmeasurable must mean 'pass it through', never 'block it'."""
    from qidi_mcp.tools.camera import _jpeg_dimensions

    assert _jpeg_dimensions(b"") is None
    assert _jpeg_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64) is None
    assert _jpeg_dimensions(b"\xff\xd8\xff\xe0" + b"\x00" * 128) is None  # no SOF


async def test_webcam_snapshot_refuses_a_featureless_frame(server_rt, moonraker):
    import json

    base_url, _ = moonraker
    server, _ = server_rt
    result = await server.call_tool(
        "get_webcam_snapshot", {"camera_url": f"{base_url}/darkcam/?action=snapshot"}
    )
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    if set(payload) == {"result"}:
        payload = payload["result"]
    assert payload["ok"] is False
    assert payload["error"] == "frame_too_dark"
    assert payload["resolution"] == [1920, 1080]
    assert payload["bytes_per_pixel"] < 0.03
    # The hint has to name the fix, or the agent just retries and gets the same frame.
    assert "set_case_light" in payload["hint"]


async def test_webcam_snapshot_allow_dark_returns_the_frame_anyway(server_rt, moonraker):
    """The escape hatch must work, so the check can never wedge a real diagnosis."""
    base_url, _ = moonraker
    server, _ = server_rt
    result = await server.call_tool(
        "get_webcam_snapshot",
        {"camera_url": f"{base_url}/darkcam/?action=snapshot", "allow_dark": True},
    )
    blocks = getattr(result, "content", None) or []
    assert blocks, "no content returned"
    assert getattr(blocks[0], "type", None) == "image", getattr(blocks[0], "text", "")


async def test_remaining_time_uses_the_slicer_estimate_before_progress_means_anything(call_tool, printer):
    """At 1% or less, extrapolating from progress is noise or nothing; the file knows."""
    printer.print_filename, printer.print_state, printer.progress = "benchy.gcode", "printing", 0.005
    r = await call_tool("get_job_status")
    assert r["estimated_remaining_source"] == "slicer"
    # benchy's slicer estimate is 5820 s; nothing much printed yet in the mock.
    assert 5000 <= r["estimated_remaining_s"] <= 5820


async def test_remaining_time_follows_progress_once_it_is_meaningful(call_tool, printer):
    printer.print_filename, printer.print_state, printer.progress = "benchy.gcode", "printing", 0.5
    r = await call_tool("get_job_status")
    assert r["estimated_remaining_source"] in ("progress", None)


async def test_a_finished_print_never_gets_a_slicer_countdown(call_tool, printer):
    """The fallback is for running prints only; a done job must not count down."""
    printer.print_filename, printer.print_state, printer.progress = "benchy.gcode", "complete", 0.0
    r = await call_tool("get_job_status")
    assert r["estimated_remaining_s"] is None
    assert r["estimated_remaining_source"] is None
