"""The tests that matter: proof the safety envelope cannot be talked around.

Every test here is written from the standpoint of "an agent is trying to do
something harmful, either deliberately or by hallucinating parameters". A pass
means the server refused *and told the agent why*.
"""

from __future__ import annotations

import contextlib
import json
import re

import pytest

from qidi_mcp.config import load_settings
from qidi_mcp.errors import InvalidRequest, SafetyViolation
from qidi_mcp.limits import (
    DANGEROUS_GCODE_COMMANDS,
    MAX4,
    clamp_envelope,
    screen_gcode,
)
from qidi_mcp.policy import SafetyPolicy

# --------------------------------------------------------------------------
# Temperature limits
# --------------------------------------------------------------------------


async def test_extruder_temperature_above_policy_is_refused(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="extruder", target=400, confirm=True)
    assert result["ok"] is False
    assert result["error"] in {"safety_violation", "limit_exceeded"}
    assert printer.extruder_target == 0.0, "target must not have been sent to the printer"


async def test_extruder_temperature_above_hardware_max_is_refused(call_tool, moonraker):
    """375 is printer.cfg's max_temp; QIDI's own macro caps at 370. Both under 400."""
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="extruder", target=374, confirm=True)
    assert result["ok"] is False
    assert printer.extruder_target == 0.0


async def test_bed_temperature_above_policy_is_refused(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="bed", target=130, confirm=True)
    assert result["ok"] is False
    assert printer.bed_target == 0.0


async def test_chamber_temperature_above_policy_is_refused(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("chamber_heater", target=80, confirm=True)
    assert result["ok"] is False
    assert printer.chamber_target == 0.0


async def test_negative_temperature_is_refused(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="extruder", target=-50, confirm=True)
    assert result["ok"] is False
    assert printer.extruder_target == 0.0


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), float("-inf")])
async def test_non_finite_temperature_is_refused(call_tool, moonraker, bad):
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="extruder", target=bad, confirm=True)
    assert result["ok"] is False
    assert printer.extruder_target == 0.0


async def test_temperature_within_limits_is_applied(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="extruder", target=220, confirm=True)
    assert result["ok"] is True
    assert printer.extruder_target == 220.0


async def test_high_but_legal_temperature_needs_confirmation(call_tool, moonraker):
    """Above the confirm threshold (260) a plain call must not fire."""
    _, printer = moonraker
    result = await call_tool("set_temperature", heater="extruder", target=280)
    assert result["ok"] is False
    assert printer.extruder_target == 0.0


async def test_env_cannot_widen_limits_past_hardware(moonraker):
    """A reckless env var must still be clamped to what the hardware allows."""
    base_url, _ = moonraker
    settings = load_settings(
        {
            "QIDI_PRINTER_URL": base_url,
            "QIDI_TEST_MODE": "1",
            "QIDI_MAX_EXTRUDER_TEMP": "999",
            "QIDI_MAX_BED_TEMP": "500",
            "QIDI_MAX_CHAMBER_TEMP": "300",
        }
    )
    assert settings.safety.extruder.max <= 375.0
    assert settings.safety.bed.max <= 125.0
    assert settings.safety.chamber.max <= 68.0


def test_clamp_envelope_never_exceeds_hardware():
    """Whatever the envelope claims, the hardware profile is the ceiling."""
    from dataclasses import replace

    from qidi_mcp.limits import DEFAULT_SAFETY, HeaterLimits

    reckless = replace(
        DEFAULT_SAFETY,
        extruder=HeaterLimits(min=0.0, max=9999.0, confirm_above=9999.0),
        bed=HeaterLimits(min=0.0, max=9999.0, confirm_above=9999.0),
        chamber=HeaterLimits(min=0.0, max=9999.0, confirm_above=9999.0),
        max_move_feedrate=10_000_000.0,
    )
    env = clamp_envelope(reckless, MAX4)
    assert env.extruder.max <= MAX4.extruder_max_temp
    assert env.bed.max <= MAX4.bed_max_temp
    assert env.chamber.max <= MAX4.chamber_max_temp
    assert env.max_move_feedrate <= MAX4.max_velocity * 60.0
    # confirm_above must never sit above max, or nothing would ever be confirmed
    assert env.extruder.confirm_above <= env.extruder.max


# --------------------------------------------------------------------------
# Confirmation model
# --------------------------------------------------------------------------


async def test_destructive_tool_requires_two_steps(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("delete_file", path="benchy.gcode", confirm=True)
    assert first["ok"] is False
    assert first.get("confirm_token")
    assert printer.deleted == []

    second = await call_tool(
        "delete_file", path="benchy.gcode", confirm=True,
        confirm_token=first["confirm_token"],
    )
    assert second["ok"] is True
    assert "benchy.gcode" in printer.deleted


async def test_confirm_token_is_single_use(call_tool, moonraker):
    _, printer = moonraker
    first = await call_tool("delete_file", path="benchy.gcode", confirm=True)
    token = first["confirm_token"]
    await call_tool("delete_file", path="benchy.gcode", confirm=True, confirm_token=token)
    printer.deleted.clear()

    replay = await call_tool(
        "delete_file", path="benchy.gcode", confirm=True, confirm_token=token
    )
    assert replay["ok"] is False, "a spent token must not work twice"
    assert printer.deleted == []


async def test_confirm_token_cannot_be_reused_for_different_arguments(call_tool, moonraker):
    """The classic bait-and-switch: get a token for a harmless file, use it on another."""
    _, printer = moonraker
    first = await call_tool("delete_file", path="benchy.gcode", confirm=True)
    token = first["confirm_token"]

    swapped = await call_tool(
        "delete_file", path="projects/bracket_abs.gcode", confirm=True, confirm_token=token
    )
    assert swapped["ok"] is False
    assert "projects/bracket_abs.gcode" not in printer.deleted


async def test_confirm_token_cannot_be_used_on_another_tool(call_tool, moonraker):
    """A ticket for one tool must never authorise a different tool."""
    _, printer = moonraker
    first = await call_tool("delete_file", path="benchy.gcode", confirm=True)
    token = first["confirm_token"]
    other = await call_tool("reboot_printer", confirm=True, confirm_token=token)
    assert other["ok"] is False
    assert printer.reboots == 0


async def test_stale_token_is_not_silently_ignored(call_tool, moonraker):
    """A token passed to a confirm-only tool must be checked, not dropped."""
    _, printer = moonraker
    first = await call_tool("delete_file", path="benchy.gcode", confirm=True)
    token = first["confirm_token"]
    # home_axes only needs confirm=True, but a foreign token must still be refused.
    result = await call_tool("home_axes", confirm=True, confirm_token=token)
    assert result["ok"] is False
    assert printer.homed_axes == ""


def test_broker_rejects_cross_tool_tokens():
    from qidi_mcp.errors import ConfirmationRequired
    from qidi_mcp.policy import ConfirmationBroker

    broker = ConfirmationBroker(ttl=60.0)
    ticket = broker.issue("delete_file", {"path": "a.gcode"})
    with pytest.raises(ConfirmationRequired):
        broker.redeem(ticket.token, "reboot_printer", {})


def test_broker_rejects_mutated_arguments():
    from qidi_mcp.errors import ConfirmationRequired
    from qidi_mcp.policy import ConfirmationBroker

    broker = ConfirmationBroker(ttl=60.0)
    ticket = broker.issue("delete_file", {"path": "a.gcode"})
    with pytest.raises(ConfirmationRequired):
        broker.redeem(ticket.token, "delete_file", {"path": "b.gcode"})


def test_broker_ignores_confirm_fields_in_fingerprint():
    """confirm/confirm_token must not be part of identity, or redeeming would fail."""
    from qidi_mcp.policy import ConfirmationBroker

    broker = ConfirmationBroker(ttl=60.0)
    ticket = broker.issue("delete_file", {"path": "a.gcode", "confirm": True})
    broker.redeem(
        ticket.token,
        "delete_file",
        {"path": "a.gcode", "confirm": True, "confirm_token": ticket.token},
    )


async def test_fabricated_token_is_refused(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool(
        "delete_file", path="benchy.gcode", confirm=True,
        confirm_token="deadbeefdeadbeefdeadbeef",
    )
    assert result["ok"] is False
    assert printer.deleted == []


async def test_motion_tool_requires_confirm_flag(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("home_axes")
    assert result["ok"] is False
    assert printer.homed_axes == ""

    ok = await call_tool("home_axes", confirm=True)
    assert ok["ok"] is True
    assert printer.homed_axes == "xyz"


# --------------------------------------------------------------------------
# Interlocks
# --------------------------------------------------------------------------


async def test_motion_is_blocked_during_a_print(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    printer.homed_axes = "xyz"
    result = await call_tool("move_absolute", x=100, y=100, confirm=True)
    assert result["ok"] is False
    assert result["error"] in {"interlock_blocked", "safety_violation", "limit_exceeded"}


async def test_heating_is_blocked_during_a_print(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    result = await call_tool("set_temperature", heater="bed", target=60, confirm=True)
    assert result["ok"] is False
    assert printer.bed_target == 0.0


async def test_babystep_is_allowed_during_a_print(call_tool, moonraker):
    """Live Z tuning is the one motion-ish thing that must work mid-print."""
    _, printer = moonraker
    printer.print_state = "printing"
    printer.homed_axes = "xyz"
    result = await call_tool("set_z_offset", offset=-0.02, mode="adjust", move=False, confirm=True)
    assert result["ok"] is True
    assert printer.z_offset == pytest.approx(-0.02)


async def test_speed_factor_is_allowed_during_a_print(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    result = await call_tool("set_speed_factor", percent=80, confirm=True)
    assert result["ok"] is True
    assert printer.speed_factor == pytest.approx(0.8)


async def test_move_requires_homing(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = ""
    result = await call_tool("move_absolute", x=100, y=100, confirm=True)
    assert result["ok"] is False
    assert "home" in (result.get("hint", "") + result.get("message", "")).lower()


async def test_commands_refused_when_klippy_is_shutdown(call_tool, moonraker):
    _, printer = moonraker
    printer.klippy_state = "shutdown"
    printer.klippy_message = "MCU 'mcu' shutdown: Timer too close"
    result = await call_tool("home_axes", confirm=True)
    assert result["ok"] is False


async def test_firmware_restart_works_when_klippy_is_shutdown(call_tool, moonraker):
    """The recovery path must not be gated behind a ready printer."""
    _, printer = moonraker
    printer.klippy_state = "shutdown"
    printer.klippy_message = "MCU 'mcu' shutdown: Timer too close"
    result = await call_tool("restart_firmware", confirm=True)
    assert result["ok"] is True, f"recovery must not be blocked by the shutdown: {result}"
    assert printer.firmware_restarts == 1


async def test_extrude_below_min_extrude_temp_is_refused(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    printer.extruder_temp = 24.0
    result = await call_tool("extrude_filament", distance=10, confirm=True)
    assert result["ok"] is False
    assert "135" in str(result) or "temp" in str(result).lower()


# --------------------------------------------------------------------------
# Position and motion bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("axis", "value"),
    [("x", 500.0), ("y", 500.0), ("z", 400.0), ("x", -50.0), ("y", -50.0), ("z", -20.0)],
)
async def test_out_of_bounds_move_is_refused(call_tool, moonraker, axis, value):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    before = list(printer.position)
    result = await call_tool("move_absolute", confirm=True, **{axis: value})
    assert result["ok"] is False
    assert printer.position == before


async def test_in_bounds_move_is_applied(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    result = await call_tool("move_absolute", x=200, y=200, z=50, confirm=True)
    assert result["ok"] is True
    assert printer.position[:3] == [200.0, 200.0, 50.0]


async def test_excessive_jog_distance_is_refused(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    result = await call_tool("move_relative", dx=5000, confirm=True)
    assert result["ok"] is False


async def test_excessive_extrude_length_is_refused(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    printer.extruder_temp = 240.0
    result = await call_tool("extrude_filament", distance=10_000, confirm=True)
    assert result["ok"] is False


async def test_absurd_feedrate_is_clamped_or_refused(call_tool, moonraker):
    _, printer = moonraker
    printer.homed_axes = "xyz"
    result = await call_tool("move_absolute", x=100, feedrate=999_999, confirm=True)
    if result["ok"]:
        sent = " ".join(printer.commands)
        assert "999999" not in sent, "an absurd feedrate must not reach the printer verbatim"


# --------------------------------------------------------------------------
# G-code screening
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "SAVE_CONFIG",
        "save_config",
        "  SAVE_CONFIG  ",
        "FORCE_MOVE STEPPER=stepper_x DISTANCE=10 VELOCITY=50",
        "SET_KINEMATIC_POSITION X=0 Y=0 Z=0",
        "SET_TMC_CURRENT STEPPER=stepper_x CURRENT=3.0",
        "PID_CALIBRATE HEATER=extruder TARGET=250",
        "M112",
        "G28\nSAVE_CONFIG",
        "M117 hello\nFORCE_MOVE STEPPER=stepper_z DISTANCE=100 VELOCITY=100",
    ],
)
async def test_dangerous_gcode_is_refused_without_explicit_flag(call_tool, moonraker, script):
    _, printer = moonraker
    result = await call_tool("run_gcode", script=script, confirm=True)
    assert result["ok"] is False, f"{script!r} should have been refused"
    assert script.strip().splitlines()[-1].strip() not in printer.commands


async def test_dangerous_gcode_passes_with_explicit_flag_and_token(
    call_tool, moonraker, allow_dangerous_gcode
):
    """With the operator switch on, the agent's flag plus the token still suffice."""
    _, printer = moonraker
    first = await call_tool(
        "run_gcode", script="SAVE_CONFIG", allow_dangerous=True, confirm=True
    )
    assert first.get("confirm_token"), "must still require the two-step token"
    second = await call_tool(
        "run_gcode", script="SAVE_CONFIG", allow_dangerous=True, confirm=True,
        confirm_token=first["confirm_token"],
    )
    assert second["ok"] is True
    assert "SAVE_CONFIG" in printer.commands


async def test_readonly_gcode_needs_no_token(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("run_gcode", script="M105")
    assert result["ok"] is True
    assert "M105" in printer.commands


async def test_write_gcode_needs_the_token(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("run_gcode", script="M104 S250", confirm=True)
    assert result["ok"] is False
    assert result.get("confirm_token")
    assert "M104 S250" not in printer.commands


def test_screen_gcode_catches_case_and_whitespace_variants():
    for variant in ("SAVE_CONFIG", "save_config", "  Save_Config", "SAVE_CONFIG ; note"):
        assert screen_gcode(variant).is_dangerous, variant


def test_screen_gcode_does_not_flag_innocent_commands():
    for safe in ("M105", "G28", "QUERY_ENDSTOPS", "M114", "STATUS"):
        assert not screen_gcode(safe).is_dangerous, safe


def test_screen_gcode_ignores_comments():
    assert not screen_gcode("; SAVE_CONFIG would be bad").is_dangerous


# --------------------------------------------------------------------------
# Read-only mode
# --------------------------------------------------------------------------


@pytest.fixture
async def readonly_call(moonraker):
    import json

    from qidi_mcp.server import build_server

    base_url, _ = moonraker
    settings = load_settings(
        {"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1", "QIDI_READ_ONLY": "1"}
    )
    server, runtime = build_server(settings)

    async def _call(_tool_name: str, /, **kwargs):
        result = await server.call_tool(_tool_name, kwargs)
        payload = getattr(result, "structuredContent", None)
        if payload is None and getattr(result, "content", None):
            payload = getattr(result.content[0], "text", None)
        if isinstance(payload, str):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                payload = json.loads(payload)
        if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
            payload = payload["result"]
        return payload

    try:
        yield _call
    finally:
        await runtime.aclose()


async def test_read_only_mode_blocks_writes(readonly_call, moonraker):
    _, printer = moonraker
    result = await readonly_call("set_temperature", heater="extruder", target=200, confirm=True)
    assert result["ok"] is False
    assert result["error"] == "read_only_mode"
    assert printer.extruder_target == 0.0


async def test_read_only_mode_allows_reads(readonly_call):
    result = await readonly_call("get_printer_status")
    assert result["ok"] is True


async def test_read_only_mode_still_allows_emergency_stop(readonly_call, moonraker):
    """The one write that must never be blocked."""
    _, printer = moonraker
    result = await readonly_call("emergency_stop", reason="test: safety check")
    assert result["ok"] is True
    assert printer.emergency_stops == 1


# --------------------------------------------------------------------------
# Emergency stop
# --------------------------------------------------------------------------


async def test_emergency_stop_needs_no_confirmation(call_tool, moonraker):
    _, printer = moonraker
    result = await call_tool("emergency_stop", reason="spaghetti detected")
    assert result["ok"] is True
    assert printer.emergency_stops == 1
    assert printer.klippy_state == "shutdown"


async def test_emergency_stop_works_during_a_print(call_tool, moonraker):
    _, printer = moonraker
    printer.print_state = "printing"
    result = await call_tool("emergency_stop")
    assert result["ok"] is True
    assert printer.emergency_stops == 1


async def test_emergency_stop_is_idempotent(call_tool, moonraker):
    _, printer = moonraker
    await call_tool("emergency_stop")
    result = await call_tool("emergency_stop")
    assert result["ok"] is True
    assert printer.emergency_stops == 2


# --------------------------------------------------------------------------
# Policy unit tests
# --------------------------------------------------------------------------


def _policy() -> SafetyPolicy:
    return SafetyPolicy(load_settings({"QIDI_PRINTER_URL": "127.0.0.1", "QIDI_TEST_MODE": "1"}))


def test_policy_rejects_string_coercion_attacks():
    policy = _policy()
    with pytest.raises((SafetyViolation, TypeError, ValueError)):
        policy.check_temperature("extruder", "400")  # type: ignore[arg-type]


def test_policy_allows_zero_as_off():
    policy = _policy()
    policy.check_temperature("extruder", 0.0)  # must not raise: 0 means "off"


def test_unknown_heater_is_refused():
    policy = _policy()
    with pytest.raises(SafetyViolation):
        policy.check_temperature("nonexistent_heater", 100.0)


async def test_read_only_refuses_before_touching_the_network(moonraker):
    """A read-only server must not need the printer to say 'no'.

    Otherwise an unreachable printer produces "connection failed" when the real
    reason is "this server cannot do that at all", and the agent tries to fix
    the wrong problem.
    """
    import json

    from qidi_mcp.server import build_server

    settings = load_settings(
        {
            "QIDI_PRINTER_URL": "http://127.0.0.1:9",  # nothing listens here
            "QIDI_TEST_MODE": "1",
            "QIDI_READ_ONLY": "1",
            "QIDI_TIMEOUT": "2",
        }
    )
    server, runtime = build_server(settings)
    try:
        result = await server.call_tool(
            "set_temperature", {"heater": "extruder", "target": 200, "confirm": True}
        )
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = json.loads(result.content[0].text)
        if set(payload) == {"result"}:
            payload = payload["result"]
        assert payload["ok"] is False
        assert payload["error"] == "read_only_mode", (
            f"expected a read-only refusal without network I/O, got {payload}"
        )
    finally:
        await runtime.aclose()


async def test_every_mutating_tool_is_shielded_in_read_only_mode(moonraker):
    """Sweep: no mutating tool may act while read-only, except emergency_stop."""
    import json

    from qidi_mcp.server import build_server

    base_url, printer = moonraker
    settings = load_settings(
        {"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1", "QIDI_READ_ONLY": "1"}
    )
    server, runtime = build_server(settings)
    try:
        tools = await server.list_tools()
        checked = 0
        for tool in tools:
            ann = tool.annotations
            if ann is None or ann.read_only_hint or tool.name == "emergency_stop":
                continue
            # Call with only the args we can synthesise safely; a validation
            # error is fine — what matters is that nothing mutates.
            try:
                result = await server.call_tool(tool.name, {})
            except Exception:
                continue
            payload = getattr(result, "structuredContent", None)
            if payload is None and getattr(result, "content", None):
                try:
                    payload = json.loads(result.content[0].text)
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue
            if isinstance(payload, dict) and set(payload) == {"result"}:
                payload = payload["result"]
            if isinstance(payload, dict):
                assert payload.get("ok") is not True, (
                    f"{tool.name} succeeded in read-only mode: {payload}"
                )
                checked += 1
        assert checked > 5, "the sweep did not actually exercise any tools"
        # Nothing may have reached the printer.
        assert printer.emergency_stops == 0
        assert printer.reboots == 0
        assert printer.firmware_restarts == 0
        assert printer.deleted == []
        assert printer.uploads == []
        assert printer.extruder_target == 0.0
        assert printer.bed_target == 0.0
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------
# G-code screen vs. Klipper's REAL parser.
#
# The screen is only sound if it names the same command the firmware will run.
# Klipper does not split on whitespace: it splits on runs of [A-Z_] and skips a
# leading N<number> line number. Screening the first whitespace token let
# "N1 SAVE_CONFIG" through as the unknown command "N1" while Klipper happily ran
# SAVE_CONFIG -- a full deny-list bypass in syntax every G-code sender emits.
# These tests pin the screen to upstream's parser.
# --------------------------------------------------------------------------

_KLIPPER_ARGS_RE = re.compile("([A-Z_]+|[A-Z*])")


def _klipper_would_run(line: str) -> str:
    """Independent transcription of klippy/gcode.py's command extraction.

    Deliberately written from upstream rather than importing ours, so the test
    fails if our implementation drifts.
    """
    line = line.strip()
    cpos = line.find(";")
    if cpos >= 0:
        line = line[:cpos]
    parts = _KLIPPER_ARGS_RE.split(line.upper())
    if "".join(parts[:2]) == "N":
        return "".join(parts[3:5]).strip()
    return "".join(parts[:3]).strip()


@pytest.mark.parametrize(
    "line",
    [
        "N1 SAVE_CONFIG",
        "N100 FORCE_MOVE STEPPER=stepper_x DISTANCE=10 VELOCITY=5",
        "N5 M502",
        "N 7 SAVE_CONFIG",
        "N1SAVE_CONFIG",
        "N42 SET_KINEMATIC_POSITION X=0 Y=0 Z=0",
        "N9 PID_CALIBRATE HEATER=extruder TARGET=200",
        "n1 save_config",
        "N0001 SAVE_CONFIG ; looks like a comment",
    ],
)
def test_line_numbers_cannot_smuggle_a_dangerous_command(line):
    """A leading N<number> must not hide a deny-listed command."""
    assert _klipper_would_run(line) in DANGEROUS_GCODE_COMMANDS, "test case is wrong"
    screen = screen_gcode(line)
    assert screen.is_dangerous is True, f"{line!r} bypassed the screen as {screen.command!r}"
    assert screen.command == _klipper_would_run(line)


@pytest.mark.parametrize(
    "line",
    [
        "SAVE_CONFIG",
        "save_config",
        "  SAVE_CONFIG  ",
        "\tFIRMWARE_RESTART",
        "SAVE_CONFIG ; trailing comment",
        "SAVE_CONFIG\r",
        "M502",
        "FORCE_MOVE STEPPER=stepper_z DISTANCE=5 VELOCITY=5",
    ],
)
def test_dangerous_commands_are_caught_in_every_spelling(line):
    assert screen_gcode(line).is_dangerous is True


@pytest.mark.parametrize(
    "line,expected",
    [
        ("M109 S200", "M109"),
        ("M109S200", "M109"),  # no space: Klipper still reads M109
        ("G1 X10 Y10 F3000", "G1"),
        ("N1 G28", "G28"),
        ("SET_FAN_SPEED FAN=cooling_fan SPEED=0.5", "SET_FAN_SPEED"),
        ("", ""),
        ("; pure comment", ""),
    ],
)
def test_screen_agrees_with_klipper_on_the_command_name(line, expected):
    assert screen_gcode(line).command == expected
    assert _klipper_would_run(line) == expected


def test_screen_matches_klipper_across_generated_lines():
    """Differential fuzz: for every mutation of a dangerous command, our screen
    and Klipper's parser must name the same command."""
    bases = ["SAVE_CONFIG", "FORCE_MOVE", "M502", "PID_CALIBRATE", "G28", "M105", "M109"]
    prefixes = ["", "N1 ", "N42 ", "N0 ", "  ", "\t", "N1", "n7 "]
    suffixes = ["", " ; c", "\r", " S200", " X=1 Y=2", "   "]
    mismatches = []
    for base in bases:
        for pre in prefixes:
            for suf in suffixes:
                for body in (base, base.lower()):
                    line = f"{pre}{body}{suf}"
                    ours = screen_gcode(line).command
                    theirs = _klipper_would_run(line)
                    if ours != theirs:
                        mismatches.append((line, ours, theirs))
                    # And the classification must follow the real command.
                    if theirs in DANGEROUS_GCODE_COMMANDS:
                        assert screen_gcode(line).is_dangerous, f"bypass via {line!r}"
    assert not mismatches, f"screen disagrees with Klipper on: {mismatches[:10]}"


async def test_start_print_token_is_void_if_the_file_changes(call_tool, moonraker, claim_printer):
    """TOCTOU: approving one file must not start a different one.

    upload_gcode(overwrite=True) is unconfirmed LOW tier, so between issuing a
    start_print ticket and redeeming it the bytes behind the name can be
    swapped. Binding only the filename let the old token start the new content --
    which also bypasses run_gcode's screen, since file contents are never
    screened. The token must be bound to the file's identity.
    """
    _, printer = moonraker
    await claim_printer("toctou check")
    benign = "G28\nM117 hello\n"
    up = await call_tool(
        "upload_gcode", filename="toctou.gcode", content=benign, confirm=True
    )
    assert up["ok"] is True

    ticket = await call_tool("start_print", filename="toctou.gcode", confirm=True)
    assert ticket["ok"] is False
    token = ticket["confirm_token"]

    # Swap the contents for something hostile. Overwriting is itself now a
    # two-step operation, so an attacker has to work for it -- but the point of
    # this test is that even a *fully authorised* swap must void the print token.
    hostile = "M104 S350\nM140 S120\nG1 Z-5 F600\n" + ("G1 X1 Y1\n" * 30)
    swap_ticket = await call_tool(
        "upload_gcode",
        filename="toctou.gcode",
        content=hostile,
        overwrite=True,
        confirm=True,
    )
    assert swap_ticket["ok"] is False
    swap = await call_tool(
        "upload_gcode",
        filename="toctou.gcode",
        content=hostile,
        overwrite=True,
        confirm=True,
        confirm_token=swap_ticket["confirm_token"],
    )
    assert swap["ok"] is True

    # The pre-issued token must no longer redeem.
    redeem = await call_tool(
        "start_print", filename="toctou.gcode", confirm=True, confirm_token=token
    )
    assert redeem["ok"] is False, (
        "a token issued for the benign file started the swapped file"
    )
    assert printer.print_state != "printing"
    assert printer.print_filename != "toctou.gcode"


async def test_start_print_token_still_works_when_the_file_is_untouched(
    call_tool, moonraker, claim_printer
):
    """The binding must not break the ordinary two-step flow."""
    _, printer = moonraker
    await claim_printer("happy path")
    await call_tool(
        "upload_gcode", filename="fine.gcode", content="G28\nM117 ok\n", confirm=True
    )
    ticket = await call_tool("start_print", filename="fine.gcode", confirm=True)
    redeem = await call_tool(
        "start_print",
        filename="fine.gcode",
        confirm=True,
        confirm_token=ticket["confirm_token"],
    )
    assert redeem["ok"] is True, redeem
    assert printer.print_state == "printing"


async def test_raw_gcode_cannot_bypass_the_during_print_interlock(call_tool, moonraker):
    """run_gcode/run_macro must not be a way around forbid_during_print.

    If they were, every other interlock would be advisory: an agent refused a
    mid-print move_absolute could just send G1 directly.
    """
    _, printer = moonraker
    printer.print_state = "printing"
    printer.print_filename = "job.gcode"

    acting = await call_tool("run_gcode", script="G1 X50 Y50 F3000", confirm=True)
    assert acting["ok"] is False
    assert acting["error"] == "interlock_blocked", acting
    assert not any(c.startswith("G1") for c in printer.commands)

    macro = await call_tool("run_macro", macro="CLEAR_NOZZLE", confirm=True)
    assert macro["ok"] is False
    assert macro["error"] == "interlock_blocked"


async def test_read_only_gcode_still_works_during_a_print(call_tool, moonraker):
    """Queries must stay available mid-print -- that is when you need them most."""
    _, printer = moonraker
    printer.print_state = "printing"
    r = await call_tool("run_gcode", script="M105", confirm=True)
    assert r["ok"] is True, r


async def test_overwriting_a_file_is_not_an_unconfirmed_operation(call_tool, moonraker):
    """Replacing existing G-code destroys the old file and invalidates any
    start_print ticket reviewed against it, so it needs a confirmation."""
    _, printer = moonraker
    await call_tool("upload_gcode", filename="x.gcode", content="G28\n", confirm=True)

    # Overwriting with confirm=True alone must now be a ticket, not a silent write.
    attempt = await call_tool(
        "upload_gcode", filename="x.gcode", content="M104 S350\n", overwrite=True, confirm=True
    )
    assert attempt["ok"] is False
    assert "confirm_token" in attempt
    assert attempt["replacing"] == "x.gcode"

    redeem = await call_tool(
        "upload_gcode",
        filename="x.gcode",
        content="M104 S350\n",
        overwrite=True,
        confirm=True,
        confirm_token=attempt["confirm_token"],
    )
    assert redeem["ok"] is True


async def test_fresh_upload_stays_cheap(call_tool):
    """The confirmation is for destruction, not for uploading; a new file is one step."""
    r = await call_tool("upload_gcode", filename="brand_new.gcode", content="G28\n", confirm=True)
    assert r["ok"] is True, r


async def test_every_gate_decision_is_audited(call_tool, moonraker):
    """An action must never execute without a log line.

    Previously only rt.ok() logged success, so a tool that raised after
    authorisation -- e.g. a network error once the G-code was already sent --
    left a 'ticket_issued' entry with no matching outcome, and a cleared
    confirm-flag gate left nothing at all.
    """
    _, printer = moonraker
    await call_tool("home_axes", confirm=True)
    log = await call_tool("get_audit_log", limit=50)
    entries = log["entries"]
    outcomes = [(e["tool"], e["outcome"]) for e in entries]
    assert ("home_axes", "cleared") in outcomes, outcomes


async def test_rejected_token_is_audited(call_tool, moonraker):
    """A replay or wrong-args redemption is exactly what an auditor looks for."""
    _, printer = moonraker
    await call_tool("upload_gcode", filename="a.gcode", content="G28\n", confirm=True)
    ticket = await call_tool("delete_file", path="gcodes/a.gcode", confirm=True)
    token = ticket["confirm_token"]
    ok = await call_tool("delete_file", path="gcodes/a.gcode", confirm=True, confirm_token=token)
    assert ok["ok"] is True
    # Replay the burned token.
    replay = await call_tool(
        "delete_file", path="gcodes/a.gcode", confirm=True, confirm_token=token
    )
    assert replay["ok"] is False

    log = await call_tool("get_audit_log", limit=50)
    rejected = [
        e for e in log["entries"]
        if e["outcome"] == "refused" and "token_rejected" in str(e.get("detail", ""))
    ]
    assert rejected, f"replay was not audited: {log['entries'][-5:]}"


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "NaN", "Infinity"])
def test_non_finite_settings_are_refused(bad):
    """float() accepts 'inf' and 'nan'; both would disable the checks they feed.

    A NaN limit is the nastier one: every comparison against NaN is False, so a
    NaN ceiling silently permits any temperature.
    """
    with pytest.raises(Exception) as exc:
        load_settings({"QIDI_PRINTER_URL": "127.0.0.1", "QIDI_MAX_EXTRUDER_TEMP": bad})
    assert "finite" in str(exc.value).lower() or "number" in str(exc.value).lower()


@pytest.mark.parametrize("ttl,ok", [("5", True), ("120", True), ("3600", True),
                                    ("0", False), ("-5", False), ("inf", False),
                                    ("nan", False), ("1e308", False), ("4000", False)])
def test_confirm_ttl_is_bounded(ttl, ok):
    """An unbounded TTL yields tickets redeemable hours later against a printer in
    a different state; zero or negative makes destructive tools permanently dead."""
    env = {"QIDI_PRINTER_URL": "127.0.0.1", "QIDI_CONFIRM_TTL": ttl}
    if ok:
        assert load_settings(env).confirm_ttl == float(ttl)
    else:
        with pytest.raises(InvalidRequest):
            load_settings(env)


def test_broker_refuses_a_nonsensical_ttl():
    from qidi_mcp.policy import ConfirmationBroker

    for bad in (float("inf"), float("nan"), 0.0, -1.0):
        with pytest.raises(ValueError):
            ConfirmationBroker(ttl=bad)


def test_ticket_store_is_bounded():
    """A ticket flood must not grow memory without limit."""
    from qidi_mcp.policy import ConfirmationBroker

    broker = ConfirmationBroker(ttl=600.0)
    for i in range(broker.MAX_TICKETS + 50):
        broker.issue("delete_file", {"path": f"gcodes/f{i}.gcode"})
    assert len(broker._tickets) <= broker.MAX_TICKETS


async def test_interlocks_are_evaluated_on_fresh_state_not_a_cache(call_tool, moonraker):
    """The stale-state bypass.

    A refused call used to prime the 0.75 s state cache; the agent's immediate
    retry was then judged against the pre-refusal snapshot. If a print started in
    between -- someone pressing Print on the touchscreen -- G28/M84/M104 was
    injected into it. Interlocks must always query fresh.
    """
    _, printer = moonraker
    printer.print_state = "standby"

    # First call refused for a missing confirm; this is what primed the cache.
    first = await call_tool("home_axes")
    assert first["ok"] is False

    # A print starts out-of-band, well inside the old TTL.
    printer.print_state = "printing"
    printer.print_filename = "job.gcode"

    retry = await call_tool("home_axes", confirm=True)
    assert retry["ok"] is False, "stale cache authorised homing during a print"
    assert retry["error"] == "interlock_blocked"
    assert not any(c.strip().upper().startswith("G28") for c in printer.commands)


async def test_stale_cache_bypass_is_closed_for_every_gated_tool(call_tool, moonraker):
    """Sweep the tools the audit proved were reachable this way."""
    _, printer = moonraker
    attempts = [
        ("set_temperature", {"heater": "extruder", "target": 250, "confirm": True}, "M104"),
        ("home_axes", {"confirm": True}, "G28"),
        ("motors_off", {"confirm": True}, "M84"),
        ("move_absolute", {"x": 100, "confirm": True}, "G1"),
    ]
    for tool, kwargs, gcode in attempts:
        printer.print_state = "standby"
        printer.commands.clear()
        await call_tool(tool, **{k: v for k, v in kwargs.items() if k != "confirm"})
        printer.print_state = "printing"
        result = await call_tool(tool, **kwargs)
        assert result["ok"] is False, f"{tool} ran during a print via the stale cache"
        assert not any(c.strip().upper().startswith(gcode) for c in printer.commands), (
            f"{tool} emitted {gcode} during a print"
        )


async def test_503_does_not_forge_an_idle_printer(moonraker):
    """The degraded-state fallback must not claim nothing is printing.

    Moonraker answers 503 both when Klipper is in shutdown AND when Moonraker
    itself restarted while the print continued. Asserting print_state='standby'
    let reboot/shutdown/restart_firmware run on a live job.
    """
    import json

    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server

    base_url, printer = moonraker
    settings = load_settings({"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1"})
    server, runtime = build_server(settings)
    try:
        # Klipper is printing, but object queries fail the way a Moonraker
        # restart makes them fail. server_info keeps reporting 'ready'.
        printer.print_state = "printing"
        printer.fail_queries_with_503 = True

        result = await server.call_tool("reboot_printer", {"confirm": True})
        payload = getattr(result, "structuredContent", None)
        if payload is None:
            payload = json.loads(result.content[0].text)
        if set(payload) == {"result"}:
            payload = payload["result"]
        assert payload["ok"] is False, "rebooted the host on an unknown-state printer"
        assert printer.reboots == 0
    finally:
        await runtime.aclose()


async def test_recovery_still_works_when_klipper_is_genuinely_shut_down(moonraker):
    """The counterpart: a real shutdown must stay recoverable.

    Fixing the forged-idle bug must not strand the printer -- when Moonraker
    confirms Klipper is in shutdown, nothing is printing and restart_firmware is
    exactly what the operator needs.
    """
    import json

    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server

    base_url, printer = moonraker
    settings = load_settings({"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1"})
    server, runtime = build_server(settings)
    try:
        printer.klippy_state = "shutdown"
        printer.print_state = "standby"
        response = await server.call_tool("restart_firmware", {"confirm": True})
        payload = getattr(response, "structuredContent", None) or json.loads(
            response.content[0].text
        )
        if set(payload) == {"result"}:
            payload = payload["result"]
        # May clear in one step or issue a ticket depending on tier config; what
        # matters is that a genuine shutdown does not BLOCK the recovery path.
        if payload.get("confirm_token"):
            response = await server.call_tool(
                "restart_firmware",
                {"confirm": True, "confirm_token": payload["confirm_token"]},
            )
            payload = getattr(response, "structuredContent", None) or json.loads(
                response.content[0].text
            )
            if set(payload) == {"result"}:
                payload = payload["result"]
        assert payload["ok"] is True, f"recovery blocked after a real shutdown: {payload}"
        assert printer.firmware_restarts == 1
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------
# Argument screening on the raw G-code path.
#
# Screening command NAMES is not enough: "SET_HEATER_TEMPERATURE" is an ordinary
# name and "TARGET=374" is not. Before this, the same server that refused
# set_temperature(374) would send M104 S374 as raw text -- 1 C under the MCU
# shutdown trip -- because the numeric envelope was only enforced by the
# dedicated tools.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=374",
        "M104 S374",
        "M109 S374",
        "M140 S200",
        "M190 S200",
        "M141 S90",
        "M191 S90",
        "N1 M104 S374",          # line-numbered, to combine both screens
        "M104 S374 ; innocuous comment",
    ],
)
async def test_raw_gcode_cannot_exceed_the_temperature_envelope(call_tool, moonraker, script):
    _, printer = moonraker
    result = await call_tool("run_gcode", script=script, confirm=True)
    if result.get("confirm_token"):
        result = await call_tool(
            "run_gcode", script=script, confirm=True, confirm_token=result["confirm_token"]
        )
    assert result["ok"] is False, f"{script!r} was accepted"
    assert result["error"] == "limit_exceeded", result
    assert printer.commands == [], f"{script!r} reached the printer"
    assert printer.extruder_target == 0.0
    assert printer.bed_target == 0.0
    assert printer.chamber_target == 0.0


@pytest.mark.parametrize(
    "script",
    ["M104 S220", "M140 S60", "M141 S40", "G1 X100 Y100 F3000", "M105", "G28"],
)
async def test_legitimate_raw_gcode_still_works(call_tool, moonraker, script):
    """The screen must not become so strict that the escape hatch is useless."""
    _, printer = moonraker
    result = await call_tool("run_gcode", script=script, confirm=True)
    if result.get("confirm_token"):
        result = await call_tool(
            "run_gcode", script=script, confirm=True, confirm_token=result["confirm_token"]
        )
    assert result["ok"] is True, f"{script!r} was wrongly refused: {result}"


@pytest.mark.parametrize("script", ["G1 X99999 F3000", "G1 Z-500", "G1 Y-400"])
async def test_raw_gcode_cannot_exceed_the_axis_envelope(call_tool, moonraker, script):
    _, printer = moonraker
    result = await call_tool("run_gcode", script=script, confirm=True)
    if result.get("confirm_token"):
        result = await call_tool(
            "run_gcode", script=script, confirm=True, confirm_token=result["confirm_token"]
        )
    assert result["ok"] is False, f"{script!r} was accepted"
    assert printer.commands == []


async def test_relative_moves_are_not_bounds_checked_as_absolute(call_tool, moonraker):
    """In G91 the number is a delta, so the axis envelope does not apply to it."""
    result = await call_tool("run_gcode", script="G91\nG1 X10 F600", confirm=True)
    if result.get("confirm_token"):
        result = await call_tool(
            "run_gcode",
            script="G91\nG1 X10 F600",
            confirm=True,
            confirm_token=result["confirm_token"],
        )
    assert result["ok"] is True, result


@pytest.mark.parametrize(
    "command",
    [
        "SAVE_CONFIG_QD",          # QIDI's "save config without restarting"
        "RESET_HEATER_SCALING",    # re-widens the firmware's own temp caps
        "SAVE_VARIABLE VARIABLE=z_offset VALUE=99",
        "SET_VELOCITY_LIMIT ACCEL=100000",
        "STEPPER_BUZZ STEPPER=stepper_z",
        "M84",
        "M4029",
        "M4031",
        "M901",
        "G29",
        "PROBE_CALIBRATE",
        "M303",
        "Z_OFFSET_APPLY_PROBE",
    ],
)
async def test_vendor_and_upstream_dangerous_spellings_are_refused(call_tool, moonraker, command):
    """A deny-list that only knows upstream's names is not a deny-list.

    QIDI's macros reach the same primitives under their own names; SAVE_CONFIG_QD
    writes printer.cfg exactly as SAVE_CONFIG does, without even the restart that
    would make it obvious.
    """
    _, printer = moonraker
    result = await call_tool("run_gcode", script=command, confirm=True)
    assert result["ok"] is False, f"{command!r} passed the screen"
    assert result["error"] in {"safety_violation", "limit_exceeded"}
    assert printer.commands == []


async def test_dedicated_tools_are_not_blocked_by_the_denylist(call_tool, moonraker):
    """The deny-list governs the RAW path only.

    motors_off legitimately sends M84 and bed_mesh_calibrate legitimately runs a
    calibration; adding those spellings to the deny-list must not disable the
    dedicated tools that wrap them with proper interlocks.
    """
    _, printer = moonraker
    r = await call_tool("motors_off", confirm=True)
    assert r["ok"] is True, r
    assert any("M84" in c.upper() for c in printer.commands)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"distance": float("nan")},
        {"distance": 5, "feedrate": float("nan")},
        {"distance": float("inf")},
    ],
)
async def test_nan_never_reaches_the_wire_as_a_move(call_tool, moonraker, kwargs):
    """NaN defeats every comparison, so 'refuse if too big' silently approves it.

    A NaN that got through formatted as the literal text 'nan' in the G-code.
    """
    _, printer = moonraker
    printer.extruder_temp = 250.0
    printer.extruder_target = 250.0
    result = await call_tool("extrude_filament", confirm=True, **kwargs)
    assert result["ok"] is False, f"{kwargs} was accepted: {result}"
    assert not any("nan" in c.lower() or "inf" in c.lower() for c in printer.commands)


async def test_negative_temperature_ceiling_is_refused_at_load():
    """An inverted range contains no value at all -- including 0.

    A single mistyped env var would otherwise produce a server that can be
    pointed at a hot printer and cannot turn the heater off.
    """
    with pytest.raises(InvalidRequest):
        load_settings({"QIDI_PRINTER_URL": "127.0.0.1", "QIDI_MAX_CHAMBER_TEMP": "-1"})


def test_clamp_never_produces_an_empty_range():
    """Defence in depth: even if a negative slipped through, clamping must yield
    'off only' rather than 'nothing permitted'."""
    from dataclasses import replace as dc_replace

    from qidi_mcp.limits import MAX4, HeaterLimits, clamp_envelope

    base = load_settings({"QIDI_PRINTER_URL": "127.0.0.1"}).safety
    hostile = dc_replace(base, chamber=HeaterLimits(min=0.0, max=-1.0, confirm_above=-1.0))
    clamped = clamp_envelope(hostile, MAX4)
    assert clamped.chamber.max >= 0.0
    assert clamped.chamber.contains(0.0), "cannot turn the chamber heater off"


# --------------------------------------------------------------------------
# Multi-line smuggling.
#
# klipper_command_name() resolves ONE line, while Klipper's dispatcher accepts a
# list. Given "M105\nSAVE_CONFIG" the screen sees only M105. That divergence is
# currently unreachable because every call site splits lines first (run_gcode) or
# rejects newlines outright (run_macro) -- but "currently unreachable" is a
# property of the callers, not of the screen, so it is pinned here. If someone
# adds a third path that hands raw multi-line text to the screen, these fail.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "M105\nSAVE_CONFIG",
        "M105\r\nSAVE_CONFIG",
        "M105\nM502",
        "M105\nFORCE_MOVE STEPPER=stepper_x DISTANCE=10 VELOCITY=50",
        "M105\nSET_KINEMATIC_POSITION X=0",
        "M114\nPID_CALIBRATE HEATER=extruder TARGET=240",
    ],
)
async def test_embedded_newline_cannot_smuggle_a_dangerous_command(call_tool, moonraker, script):
    _, printer = moonraker
    result = await call_tool("run_gcode", script=script, confirm=True)
    if result.get("confirm_token"):
        result = await call_tool(
            "run_gcode", script=script, confirm=True, confirm_token=result["confirm_token"]
        )
    assert result["ok"] is False, f"{script!r} was accepted"
    assert result["error"] == "safety_violation", result
    forbidden = ("SAVE_CONFIG", "M502", "FORCE_MOVE", "SET_KINEMATIC", "PID_CALIBRATE")
    assert not [c for c in printer.commands if any(f in c.upper() for f in forbidden)]


def test_the_screen_itself_only_speaks_for_one_line():
    """Documents the sharp edge deliberately, so it cannot be misread as safe.

    screen_gcode() answers for the FIRST line only. Callers must split. This test
    exists so that fact is asserted rather than assumed.
    """
    screen = screen_gcode("M105\nSAVE_CONFIG")
    assert screen.command == "M105"
    assert screen.is_dangerous is False, (
        "If this ever becomes True the screen has grown multi-line awareness, and "
        "the callers' split-first contract should be revisited."
    )


# --------------------------------------------------------------------------
# Argument-sensitive screening.
#
# Some commands are safe or dangerous depending on an argument.
# BED_MESH_PROFILE LOAD= is routine; SAVE=/REMOVE= rewrite the stored profile.
# Denying the whole command would block the harmless form and push agents toward
# the escape hatch -- the opposite of what a deny-list is for.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "dangerous"),
    [
        ("BED_MESH_PROFILE LOAD=default", False),
        ("BED_MESH_PROFILE SAVE=mine", True),
        ("BED_MESH_PROFILE REMOVE=old", True),
        ("BED_MESH_PROFILE LOAD=default SAVE=mine", True),
        ("N5 BED_MESH_PROFILE SAVE=x", True),        # composes with the line-number fix
        ("bed_mesh_profile save=x", True),           # and with case folding
    ],
)
def test_argument_sensitive_commands_are_screened_on_their_arguments(line, dangerous):
    assert screen_gcode(line).is_dangerous is dangerous, line


@pytest.mark.parametrize(
    "tool",
    ["bed_mesh_calibrate", "z_tilt_adjust"],
)
async def test_denying_a_command_does_not_disable_its_dedicated_tool(call_tool, moonraker, tool):
    """The deny-list governs the RAW path only.

    The unattended-calibration commands are denied as raw text because they move
    the machine for minutes with nobody watching. The dedicated tools wrap the
    same commands with interlocks, progress reporting and a timeout, and must keep
    working -- otherwise a broad deny-list quietly removes functionality instead
    of adding safety.
    """
    _, printer = moonraker
    result = await call_tool(tool, confirm=True)
    if result.get("confirm_token"):
        result = await call_tool(tool, confirm=True, confirm_token=result["confirm_token"])
    assert result["ok"] is True, result
    assert printer.commands, f"{tool} sent nothing"


# --------------------------------------------------------------------------
# Refusals must be audited.
#
# Gate decisions inside authorize() were logged, but a refusal raised BEFORE the
# gate -- an out-of-envelope temperature, a NaN, a deny-listed command on the raw
# path -- left no trace, while the harmless calls that reached the gate were all
# recorded. Backwards for anyone reading the log to learn what an agent tried.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs", "detail"),
    [
        ("run_gcode", {"script": "SAVE_CONFIG", "confirm": True}, "safety_violation"),
        ("run_gcode", {"script": "M104 S374", "confirm": True}, "limit_exceeded"),
        (
            "set_temperature",
            {"heater": "extruder", "target": 374, "confirm": True},
            "limit_exceeded",
        ),
        ("extrude_filament", {"distance": float("nan"), "confirm": True}, "limit_exceeded"),
        ("move_absolute", {"x": 99999, "confirm": True}, "limit_exceeded"),
    ],
)
async def test_pre_gate_refusals_reach_the_audit_log(moonraker, tmp_path, tool, kwargs, detail):
    from qidi_mcp.server import build_server

    base, _printer = moonraker
    log = tmp_path / "audit.jsonl"
    settings = load_settings(
        {"QIDI_PRINTER_URL": base, "QIDI_TEST_MODE": "1", "QIDI_AUDIT_LOG": str(log)}
    )
    server, rt = build_server(settings)
    try:
        await server.call_tool(tool, kwargs)
    finally:
        await rt.aclose()

    assert log.exists(), f"{tool} refusal produced no audit log at all"
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    refusals = [e for e in entries if e["outcome"] == "refused"]
    assert refusals, f"{tool} was refused but nothing was recorded: {entries}"
    assert any(e["detail"] == detail for e in refusals), refusals
    assert any(e["tool"] == tool for e in refusals), refusals


async def test_successful_reads_are_not_logged(moonraker, tmp_path):
    """Read queries stay out of the log; otherwise polling drowns the signal."""
    from qidi_mcp.server import build_server

    base, _printer = moonraker
    log = tmp_path / "audit.jsonl"
    settings = load_settings(
        {"QIDI_PRINTER_URL": base, "QIDI_TEST_MODE": "1", "QIDI_AUDIT_LOG": str(log)}
    )
    server, rt = build_server(settings)
    try:
        for _ in range(5):
            await server.call_tool("get_printer_status", {})
    finally:
        await rt.aclose()
    entries = [] if not log.exists() else [
        json.loads(line) for line in log.read_text().splitlines() if line.strip()
    ]
    assert entries == [], f"read queries polluted the audit log: {entries}"
