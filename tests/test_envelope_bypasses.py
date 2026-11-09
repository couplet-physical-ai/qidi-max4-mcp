"""Regression tests for safety-gate bypasses found during the OSS export review.

An independent review drove the mock printer through each of these with the
two-step confirmation token redeemed, and every one reached the wire:

* ``run_macro("M104", {"S": 374})`` -- run_macro screened only the macro name;
* ``SET_HEATER_TEMPERATURE_SCALED ... TARGET=374`` -- not a known heater command;
* ``M203 X99999 Y99999`` (and M201/M204/M205) -- kinematic limits unchecked;
* ``SAVE_CONFIG`` with ``allow_dangerous=true`` -- an agent-supplied flag behind an
  agent-redeemed token, with no operator in the loop;
* fifteen stock vendor macros that reach a restricted primitive under an
  innocuous name (docs/security/proofs/L5_denylist.py).

Every test drives the full handshake: if the first call hands back a token, the
second call redeems it, so a pass means the command was refused outright rather
than merely parked behind a confirmation.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from qidi_mcp.config import load_settings
from qidi_mcp.limits import (
    DANGEROUS_GCODE_COMMANDS,
    DEFAULT_SAFETY,
    command_params,
    klipper_extended_params,
)

ROOT = Path(__file__).resolve().parent.parent


async def _drive(call_tool, tool: str, **kwargs):
    """Call a tool, redeeming the confirmation token if it hands one back."""
    result = await call_tool(tool, confirm=True, **kwargs)
    if isinstance(result, dict) and result.get("confirm_token"):
        result = await call_tool(
            tool, confirm=True, confirm_token=result["confirm_token"], **kwargs
        )
    return result


def _assert_refused(result, printer, *, errors=("limit_exceeded",)):
    assert result["ok"] is False, f"accepted: {result}"
    assert result["error"] in errors, result
    assert printer.commands == [], f"reached the printer: {printer.commands}"


# --------------------------------------------------------------------------
# 1. run_macro shares run_gcode's screen
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("macro", "params"),
    [
        ("M104", {"S": 374}),             # the reproduced bypass
        ("M109", {"S": 374}),
        ("M140", {"S": 200}),
        ("M141", {"S": 90}),
        ("M190", {"S": 200}),
        ("M191", {"S": 90}),
        ("PRINT_START", {"HOTEND": 374}),  # vendor macro -> M104 S{HOTEND}
        ("PRINT_START", {"BED": 200}),
        ("PRINT_START", {"CHAMBER": 90}),
        ("CLEAR_NOZZLE", {"HOTEND": 374}),
    ],
)
async def test_run_macro_params_are_range_checked(call_tool, moonraker, macro, params):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro=macro, params=params)
    _assert_refused(result, printer)
    assert printer.extruder_target == 0.0


async def test_run_macro_motion_params_are_bounds_checked(call_tool, moonraker):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro="G1", params={"X": 99999})
    _assert_refused(result, printer, errors=("limit_exceeded", "invalid_request"))


async def test_run_macro_within_the_envelope_still_works(call_tool, moonraker):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro="M104", params={"S": 220})
    assert result["ok"] is True, result
    assert any(c.startswith("M104") for c in printer.commands)


async def test_run_macro_refuses_deny_listed_names_even_with_the_operator_switch(
    call_tool, moonraker, allow_dangerous_gcode
):
    """run_macro has no allow_dangerous argument; deny-listed names never pass."""
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro="M4029")
    _assert_refused(result, printer, errors=("safety_violation",))


# --------------------------------------------------------------------------
# 2. Heater commands: QIDI's scaled setter, R parameters, live scaling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "SET_HEATER_TEMPERATURE_SCALED HEATER=extruder TARGET=374",
        # Within the extruder's 300 but over the bed's 120: proves the heater
        # name is read (it used to be lost and every line checked as extruder).
        "SET_HEATER_TEMPERATURE_SCALED HEATER=heater_bed TARGET=150",
        'SET_HEATER_TEMPERATURE_SCALED HEATER="heater_generic chamber" TARGET=90',
        "SET_HEATER_TEMPERATURE_SCALED HEATER=chamber TARGET=90",
        "M109 R374",                      # QIDI's wait wrapper falls back to R
        "M190 R200",
        "M191 R90",
    ],
)
async def test_raw_heater_spellings_cannot_exceed_the_envelope(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


async def test_scaled_setter_without_a_heater_is_refused(call_tool, moonraker):
    _, printer = moonraker
    result = await _drive(
        call_tool, "run_gcode", script="SET_HEATER_TEMPERATURE_SCALED TARGET=200"
    )
    _assert_refused(result, printer, errors=("safety_violation",))


async def test_scaled_setter_within_the_envelope_still_works(call_tool, moonraker):
    _, printer = moonraker
    script = "SET_HEATER_TEMPERATURE_SCALED HEATER=extruder TARGET=200"
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


@pytest.mark.parametrize(
    "script",
    ["M104 S250", "SET_HEATER_TEMPERATURE_SCALED HEATER=extruder TARGET=250"],
)
async def test_live_heater_scaling_is_applied_before_the_check(call_tool, moonraker, script):
    """If the firmware's scaling has been changed, check what the heater will get.

    QIDI's macro sends min(max((T + bump) * scale, minimum), maximum). With
    scale=1.5 a requested 250 becomes 370, far past the 300 envelope.
    """
    _, printer = moonraker
    printer.extra_status["gcode_macro set_heater_scaling"] = {
        "scaled_heaters": {
            "extruder": {"scale": 1.5, "bump": 0.0, "minimum": 0.0, "maximum": 370.0}
        }
    }
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


# --------------------------------------------------------------------------
# 3. Kinematic limits: M201 / M203 / M204 / M205
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "M203 X99999 Y99999",            # the reproduced bypass
        "M203 X801",
        "M203 X0",
        "M203 X-5",
        "M201 X99999 Y99999",
        "M201 Y30001",
        "M204 S99999",
        "M204 P99999 T100",
        "M205 X50 Y50",
        "M205 X-1",
    ],
)
async def test_raw_gcode_cannot_raise_kinematic_limits(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("limit_exceeded", "invalid_request"))


async def test_run_macro_cannot_raise_kinematic_limits(call_tool, moonraker):
    _, printer = moonraker
    printer.extra_help["M203"] = "Sets maximum velocity."
    result = await _drive(call_tool, "run_macro", macro="M203", params={"X": 99999})
    _assert_refused(result, printer)


@pytest.mark.parametrize(
    "script", ["M203 X300 Y300", "M201 X10000", "M204 S5000", "M205 X5", "M203"]
)
async def test_kinematic_limits_within_the_envelope_still_work(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


def test_kinematic_defaults_are_the_printer_cfg_values():
    assert DEFAULT_SAFETY.max_velocity == 800.0           # [printer] max_velocity
    assert DEFAULT_SAFETY.max_accel == 30000.0            # [printer] max_accel
    assert DEFAULT_SAFETY.max_square_corner_velocity == 8.0


def test_kinematic_limits_can_be_lowered_but_not_raised():
    base = {"QIDI_PRINTER_URL": "http://127.0.0.1:7125", "QIDI_TEST_MODE": "1"}
    low = load_settings(
        {**base, "QIDI_MAX_VELOCITY": "300", "QIDI_MAX_ACCEL": "5000",
         "QIDI_MAX_SQUARE_CORNER_VELOCITY": "4"}
    ).safety
    assert (low.max_velocity, low.max_accel, low.max_square_corner_velocity) == (
        300.0, 5000.0, 4.0
    )
    high = load_settings(
        {**base, "QIDI_MAX_VELOCITY": "99999", "QIDI_MAX_ACCEL": "999999",
         "QIDI_MAX_SQUARE_CORNER_VELOCITY": "100"}
    ).safety
    assert (high.max_velocity, high.max_accel, high.max_square_corner_velocity) == (
        800.0, 30000.0, 8.0
    )


# --------------------------------------------------------------------------
# 4. allow_dangerous needs the operator-side switch
# --------------------------------------------------------------------------


def test_the_operator_switch_defaults_off():
    base = {"QIDI_PRINTER_URL": "http://127.0.0.1:7125", "QIDI_TEST_MODE": "1"}
    assert load_settings(base).allow_dangerous_gcode is False
    assert load_settings({**base, "QIDI_ALLOW_DANGEROUS_GCODE": "1"}).allow_dangerous_gcode


async def test_allow_dangerous_alone_is_refused_without_the_operator_switch(
    call_tool, moonraker
):
    """The reproduced bypass: flag + redeemed token used to send SAVE_CONFIG."""
    _, printer = moonraker
    first = await call_tool(
        "run_gcode", script="SAVE_CONFIG", allow_dangerous=True, confirm=True
    )
    assert first["ok"] is False
    assert first["error"] == "safety_violation", first
    assert "QIDI_ALLOW_DANGEROUS_GCODE" in (first.get("hint") or "")
    assert not first.get("confirm_token"), "must not even issue a token"
    assert printer.commands == []


async def test_allow_dangerous_with_the_operator_switch_still_needs_the_token(
    call_tool, moonraker, allow_dangerous_gcode
):
    _, printer = moonraker
    first = await call_tool(
        "run_gcode", script="SAVE_CONFIG", allow_dangerous=True, confirm=True
    )
    assert first.get("confirm_token"), first
    assert printer.commands == []
    second = await call_tool(
        "run_gcode", script="SAVE_CONFIG", allow_dangerous=True, confirm=True,
        confirm_token=first["confirm_token"],
    )
    assert second["ok"] is True, second
    assert "SAVE_CONFIG" in printer.commands


async def test_the_operator_switch_without_the_agent_flag_is_refused(
    call_tool, moonraker, allow_dangerous_gcode
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script="SAVE_CONFIG")
    _assert_refused(result, printer, errors=("safety_violation",))


async def test_the_operator_switch_does_not_widen_the_envelope(
    call_tool, moonraker, allow_dangerous_gcode
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script="M104 S374", allow_dangerous=True)
    _assert_refused(result, printer)


# --------------------------------------------------------------------------
# 5. Stock vendor macros that reach restricted primitives
# --------------------------------------------------------------------------

#: The fifteen L5 found. The four not denied by name are value-checked instead.
L5_DENIED = [
    "BEEP_ON", "BEEP_OFF", "M603", "M604", "PAUSE", "PRINT_END",
    "TEMPERATURE_WAIT_SCALED", "_CHECK_HEATER_PARAMS", "_KM_APPLY_PRINT_OFFSET",
    "_PRINT_START_BOX_PREPAR", "_PRINT_START_PHASE_INIT",
]
L5_VALUE_CHECKED = ["M201", "M203", "M205", "SET_HEATER_TEMPERATURE_SCALED"]


@pytest.mark.parametrize("macro", [*L5_DENIED, "DISABLE_ALL_SENSOR", "SET_GCODE_VARIABLE"])
async def test_vendor_macros_reaching_restricted_primitives_are_refused(
    call_tool, moonraker, macro
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=macro)
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.parametrize("macro", ["PAUSE", "M603", "M604"])
async def test_vendor_macros_are_refused_through_run_macro_too(call_tool, moonraker, macro):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro=macro)
    _assert_refused(result, printer, errors=("safety_violation",))


async def test_heater_scaling_cannot_be_rewritten_through_macro_variables(
    call_tool, moonraker
):
    _, printer = moonraker
    script = (
        "SET_GCODE_VARIABLE MACRO=set_heater_scaling VARIABLE=scaled_heaters "
        "VALUE=\"{'extruder': {'scale': 2.0}}\""
    )
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.skipif(
    not (ROOT / "research/upstream/qidi-max4/config/klipper-macros-qd").is_dir(),
    reason="optional GPLv3 vendor macros not fetched; run scripts/fetch_upstream_sources.sh",
)
def test_l5_reports_no_uncovered_vendor_macros():
    """The completeness proof itself, run against the vendored stock config."""
    proc = subprocess.run(
        [sys.executable, "docs/security/proofs/L5_denylist.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "QIDI_TEST_MODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    found = re.search(r"NOT covered \(denied or value-checked\): (\d+)", proc.stdout)
    assert found, proc.stdout[-2000:]
    assert int(found.group(1)) == 0, proc.stdout
    for name in L5_DENIED:
        assert name in DANGEROUS_GCODE_COMMANDS
    for name in L5_VALUE_CHECKED:
        assert name not in DANGEROUS_GCODE_COMMANDS


# --------------------------------------------------------------------------
# 6. Parameter parsing matches Klipper's two parsers
# --------------------------------------------------------------------------


def test_extended_params_keep_the_heater_name():
    assert command_params("SET_HEATER_TEMPERATURE_SCALED HEATER=heater_bed TARGET=60") == {
        "HEATER": "heater_bed",
        "TARGET": "60",
    }
    assert klipper_extended_params('SET_X HEATER="heater_generic chamber" T=1 ; c') == {
        "HEATER": "heater_generic chamber",
        "T": "1",
    }
    assert command_params("N5 SET_X TARGET=1") == {"TARGET": "1"}


def test_malformed_extended_params_are_reported_not_guessed():
    assert klipper_extended_params("SET_X TARGET") is None       # no '='
    assert klipper_extended_params('SET_X HEATER="open') is None  # unclosed quote


def test_traditional_params_use_the_letter_parser():
    assert command_params("M104 S220")["S"] == "220"
    params = command_params("M203 X300Y400")
    assert (params["X"], params["Y"]) == ("300", "400")


async def test_the_internal_wait_wrapper_is_range_checked(call_tool, moonraker):
    """_GCODE_WAIT_WRAPPER is what M109/M190 call; called directly it must not skip the check."""
    _, printer = moonraker
    result = await _drive(
        call_tool, "run_gcode", script="_GCODE_WAIT_WRAPPER HEATER=extruder S=374"
    )
    _assert_refused(result, printer)


# ==========================================================================
# Round 2: a second independent review, same method (token redeemed,
# QIDI_ALLOW_DANGEROUS_GCODE unset, against the mock). Every case below
# reached the wire on 1b1904c.
# ==========================================================================

# --------------------------------------------------------------------------
# 7. rename_existing targets: the raw built-ins behind QIDI's wrappers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        # Reproduced: the raw setters, past even QIDI's own 370/120 C clamp.
        "M104.6245197 S374",
        "M109.6245197 S374",
        "M140.6245197 S124",
        "M190.6245197 S124",
        "m104.6245197 s374",
        "N7 M104.6245197 S374",
    ],
)
async def test_renamed_heater_builtins_are_temperature_checked(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


async def test_renamed_heater_builtins_stay_checked_with_the_operator_switch(
    call_tool, moonraker, allow_dangerous_gcode
):
    """The switch may let the name through; the stem's envelope still applies."""
    _, printer = moonraker
    result = await _drive(
        call_tool, "run_gcode", script="M104.6245197 S374", allow_dangerous=True
    )
    _assert_refused(result, printer)


@pytest.mark.parametrize(
    "script",
    [
        "M104.6245197 S200",      # in range, but the unwrapped built-in
        "G28.6245197",
        "_BED_MESH_CALIBRATE",    # reproduced: BED_MESH_CALIBRATE is denied
        "_KM_PAUSE_BASE",         # reproduced: PAUSE is denied
        "_KM_CANCEL_PRINT_BASE",
        "_KM_RESUME_BASE",
        "_KM_CLEAR_PAUSE",
        "_KM_SAVE_GCODE_STATE",
        "_KM_RESTORE_GCODE_STATE NAME=x MOVE=1",
        "M24.6245197",
        "M25.6245197",
        # Decimal G/M codes the stock config does not define are refused.
        "M104.1 S200",
        "M140.0 S60",
        "G1.5 X10",
        "G28.1",
    ],
)
async def test_renamed_builtins_and_unknown_decimal_codes_are_refused(
    call_tool, moonraker, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.parametrize("script", ["M109.0 S200", "M109.1 S50"])
async def test_known_decimal_macros_still_work(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


async def test_rename_targets_in_the_printers_own_config_are_refused(
    call_tool, moonraker
):
    """A rename the stock list does not know is read from configfile at runtime."""
    from tests.mock_moonraker import CONFIG_SETTINGS

    _, printer = moonraker
    printer.extra_status["configfile"] = {
        "settings": {
            **CONFIG_SETTINGS,
            "gcode_macro my_home": {"rename_existing": "_my_raw_home", "gcode": ""},
        },
        "config": {"gcode_macro my_home": {"rename_existing": "_MY_RAW_HOME"}},
        "save_config_pending": False,
    }
    result = await _drive(call_tool, "run_gcode", script="_MY_RAW_HOME")
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.skipif(
    not (ROOT / "research/upstream/qidi-max4/config/klipper-macros-qd").is_dir(),
    reason="optional GPLv3 vendor macros not fetched; run scripts/fetch_upstream_sources.sh",
)
def test_every_stock_rename_target_is_deny_listed():
    """Read the vendored config, so a new rename in an update cannot slip past."""
    targets = set()
    for path in (ROOT / "research/upstream/qidi-max4/config/klipper-macros-qd").glob("*.cfg"):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*rename_existing\s*:\s*(\S+)", line)
            if m:
                targets.add(m.group(1).upper())
    assert targets, "no rename_existing found; has the vendored config moved?"
    assert targets <= DANGEROUS_GCODE_COMMANDS, targets - DANGEROUS_GCODE_COMMANDS


# --------------------------------------------------------------------------
# 8. Line separators: Klipper splits on \n only, and the last value wins
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "M104 S100\rS374",        # reproduced
        "M140 S60\x0cS200",       # reproduced
        "M203 X100\u2028X99999",  # reproduced
        "G1 X10\x1cX9999",        # reproduced
        "M104 S100\x0bS374",
        "M104 S100\x1dS374",
        "M104 S100\x1eS374",
        "M104 S100\x85S374",
        "M104 S100\u2029S374",
        "M104 S100\x00S374",
        "M104 S200\n\rM140 S60",  # a lone \r not ending a line
    ],
)
async def test_scripts_with_other_line_separators_are_refused(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("invalid_request", "limit_exceeded"))


async def test_crlf_and_tabs_still_work(call_tool, moonraker):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script="M104\tS200\r\nM140 S60\r\n")
    assert result["ok"] is True, result
    assert result["lines_sent"] == 2


@pytest.mark.parametrize("value", ["100\rS374", "100\u2028S374", "100\x0cS374", '1" S=374'])
async def test_run_macro_values_cannot_carry_separators(call_tool, moonraker, value):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro="M104", params={"S": value})
    _assert_refused(result, printer, errors=("invalid_request", "limit_exceeded"))


def test_split_matches_klipper():
    from qidi_mcp.limits import split_gcode_script

    assert split_gcode_script("M104 S200\r\n\n  G28 \n") == ["M104 S200", "G28"]
    with pytest.raises(ValueError):
        split_gcode_script("M104 S100\rS374")


# --------------------------------------------------------------------------
# 9. Stock-macro parameters that reach a heater, limit or motion primitive
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "M600 R374",                          # reproduced: M109 S{params.R}
        "EXTRUSION_AND_FLUSH HOTEND=374",     # reproduced: M109 S{params.HOTEND}
        "BOX_PRINT_START EXTRUDER=0 HOTENDTEMP=374",
        "PRINT_START HOTEND_TEMP=374",
        "PRINT_START BED_TEMP=200",
        # Coordinates PARK hands to G0 unchecked by the envelope.
        "PARK X=9999",
        "PARK Y=-50",
        "G27 X9999",
        "M600 X9999",
        # G-code offsets: MOVE=1 moves the toolhead by the change.
        "SET_GCODE_OFFSET Z=-10 MOVE=1",
        "SET_GCODE_OFFSET Z_ADJUST=-6",
        "M290 Z-10",                          # -> SET_GCODE_OFFSET Z_ADJUST
    ],
)
async def test_stock_macro_params_are_range_checked(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


@pytest.mark.parametrize(
    "script",
    [
        # Reproduced: plant HOTEND in PRINT_START_SET's state, then run the phase
        # that reads it back (start_end.cfg: M109 S{print.HOTEND}).
        'PRINT_START_SET PRINT_START_PHASE="extruder" HOTEND=374\n_PRINT_START_PHASE_EXTRUDER',
        "PRINT_START_SET HOTEND=374",
        "PRINT_START_SET BED=200",
        "_PRINT_START_PHASE_EXTRUDER",
        "_PRINT_START_PHASE_PREHEAT",
        "_PRINT_START_PHASE_PROBING",
        "_PRINT_START_PHASE_CHECK PHASE=none",
        # M{params.M}.6245197 picks which raw built-in gets the target.
        "_GCODE_WAIT_WRAPPER_INNER HEATER=extruder M=140 R=1",
        # Stores COMMAND and runs it later, unscreened.
        "GCODE_ON_PRINT_STATUS STATUS=all WHEN=ALWAYS COMMAND=SAVE_CONFIG",
        "_KM_PRINT_STATUS ACTION=ADD_EVENT STATUS=ready COMMAND=SAVE_CONFIG",
        "_PARK_INNER X=9999 Y=0 Z=0 P=1 LAZY=0",
        "M0",
        "M25",
        "M601",
        "M1002 S1",                           # SAVE_VARIABLE z_offset
        "M900 K1",                            # SET_PRESSURE_ADVANCE
        "SET_OPERATING_CURRENT STEPPER=x VALUE=3000",
        "SET_HOMING_STATE STEPPER=x VALUE=1",
    ],
)
async def test_stock_macros_that_launder_values_are_refused(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    # PRINT_START_SET and _PARK_INNER are value-checked as well as denied, so
    # the out-of-range ones are refused by the envelope before the deny-list.
    _assert_refused(result, printer, errors=("safety_violation", "limit_exceeded"))


@pytest.mark.parametrize(
    "script",
    [
        # M109 forwards every param to _GCODE_WAIT_WRAPPER_INNER, whose
        # M{params.M}.6245197 S{target} sent the checked extruder target (200)
        # to the bed's raw built-in: M140.6245197 S200.
        "M109 S200 R1 M140",
        "M109 S200 R1 M190",
        "M190 S60 R1 M104",
        "M109 S200 HEATER5",
    ],
)
async def test_heater_wrappers_refuse_parameters_they_would_forward(
    call_tool, moonraker, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.parametrize(
    "script",
    ["M109 S200", "M109 R200 T0", "M190 S60", "N3 M109 S200", "M600 R220", "PARK X=100 Y=100",
     "M290 Z0.05", "SET_GCODE_OFFSET Z=0.1"],
)
async def test_documented_forms_still_work(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


async def test_a_failed_heater_scaling_read_fails_closed(call_tool, moonraker, server_rt):
    from qidi_mcp.errors import QidiError

    _, printer = moonraker
    _, rt = server_rt
    real = rt.client.query

    async def flaky(objects, *a, **kw):
        if "gcode_macro set_heater_scaling" in objects:
            raise QidiError("simulated timeout")
        return await real(objects, *a, **kw)

    rt.client.query = flaky
    result = await _drive(call_tool, "run_gcode", script="M104 S200")
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.skipif(
    not (ROOT / "research/upstream/qidi-max4/config/klipper-macros-qd").is_dir(),
    reason="optional GPLv3 vendor macros not fetched; run scripts/fetch_upstream_sources.sh",
)
def test_l5_reports_no_uncovered_parameter_flows():
    """L5 (e): every stock-macro parameter reaching a heater/limit/motion primitive."""
    proc = subprocess.run(
        [sys.executable, "docs/security/proofs/L5_denylist.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "QIDI_TEST_MODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    found = re.search(r"parameter flows NOT covered: (\d+)", proc.stdout)
    assert found, proc.stdout[-3000:]
    assert int(found.group(1)) == 0, proc.stdout[-3000:]
    # The flows the review found must be among the ones L5 sees.
    for flow in ("EXTRUSION_AND_FLUSH.HOTEND", "M600.R", "PRINT_START_SET.HOTEND", "M290.Z"):
        assert flow in proc.stdout, flow


# --------------------------------------------------------------------------
# 10. Follow-up to #48's re-review: Box temperatures, PRINT_START_SET value
#     checks under the operator switch, padded parameter names, the sensor
#     primitive, raw print starts, and the Z floor after G91.
# --------------------------------------------------------------------------

_EITHER = ("safety_violation", "limit_exceeded")


@pytest.mark.parametrize(
    "script",
    [
        "E_LOAD SLOT=0 S=374",               # reproduced: reached the wire on 124349b
        "E_LOAD SLOT=16 S=374",              # filament.cfg's form (M604 -> E_LOAD)
        # Box firmware commands with unpublished parameters: anything that names
        # a temperature is held to the extruder envelope.
        "MULTI_COLOR_LOAD SLOT=0 TEMP=374",
        "MULTI_COLOR_LOAD SLOT=0 HOTEND_TEMP=374",
        "MULTI_COLOR_UNLOAD SLOT=0 S=374",
        "MULTI_COLOR_BOX_UNLOAD SLOT=0 TEMPERATURE=374",
        "EXTRUDER_LOAD SLOT=1 S=374",
        "E_UNLOAD SLOT=16 TEMP=374",
        "BOX_PRINT_START EXTRUDER=0 HOTENDTEMP=374",
    ],
)
async def test_box_temperatures_are_range_checked(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


async def test_e_load_is_range_checked_through_run_macro(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box()
    result = await _drive(call_tool, "run_macro", macro="E_LOAD", params={"SLOT": 0, "S": 374})
    _assert_refused(result, printer)


@pytest.mark.parametrize("script", ["MULTI_COLOR_DRY TEMP=60", "MULTI_COLOR_DRY HEATER_TARGET=60"])
async def test_box_heater_temperatures_without_an_envelope_are_refused(
    call_tool, moonraker, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))


@pytest.mark.parametrize(
    "script",
    ["E_LOAD SLOT=0 S=230", "MULTI_COLOR_LOAD SLOT=0", "MULTI_COLOR_LOAD SLOT=0 TEMP=230",
     "MULTI_COLOR_DRY TIME=4"],
)
async def test_box_commands_within_the_envelope_still_work(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


#: Deny-listed lines whose temperature (or coordinate) is now value-checked, so
#: the operator switch cannot reopen them.
_PLANTED = [
    # The reproduced bypass: plant HOTEND, then run the phase that heats to it.
    'PRINT_START_SET PRINT_START_PHASE="extruder" HOTEND=374\n_PRINT_START_PHASE_EXTRUDER',
    'PRINT_START_SET PRINT_START_PHASE="extruder" HOTEND_TEMP=374\n_PRINT_START_PHASE_EXTRUDER',
    "PRINT_START_SET BED=200",
    "PRINT_START_SET BED_TEMP=200",
    "PRINT_START_SET CHAMBER=90",
    "_PRINT_START_PHASE_INIT HOTEND=374 BED=60",
    "M4029 S200 R0",                         # qd_macro.cfg: M140 S{params.S}
    "M603 S374",
    "M604 S374",
    "PAUSE Z=-5",
    "_PARK_INNER X=9999 Y=0 Z=0 P=1 LAZY=0",
]


@pytest.mark.parametrize("script", _PLANTED)
async def test_planted_values_are_refused_with_the_operator_switch_on(
    call_tool, moonraker, allow_dangerous_gcode, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script, allow_dangerous=True)
    _assert_refused(result, printer)


@pytest.mark.parametrize("script", _PLANTED)
async def test_planted_values_are_refused_with_the_operator_switch_off(
    call_tool, moonraker, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script, allow_dangerous=True)
    _assert_refused(result, printer, errors=_EITHER)


@pytest.mark.parametrize(
    "script",
    [
        # Plants the same state without naming a checked parameter.
        "SET_GCODE_VARIABLE MACRO=print_start_set VARIABLE=print "
        "VALUE=\"{'HOTEND': 374, 'PRINT_START_PHASE': 'extruder'}\"",
        # Multiplies every later checked target.
        "SET_HEATER_SCALING HEATER=extruder SCALE=2\nM104 S200",
        "RESET_HEATER_SCALING",
        "HEATER_TEMP_LIMIT",
        # Stores a command and runs it later, unscreened.
        'GCODE_ON_PRINT_STATUS STATUS=ready WHEN=ALWAYS COMMAND="M104 S374"',
        # Sends the extruder's checked target to the bed's raw built-in.
        "_GCODE_WAIT_WRAPPER_INNER HEATER=extruder M=140 R=1",
        "_GCODE_WAIT_WRAPPER HEATER=extruder S=200 R=1 M=140",
    ],
)
async def test_envelope_defeating_commands_stay_refused_with_the_operator_switch(
    call_tool, moonraker, allow_dangerous_gcode, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script, allow_dangerous=True)
    _assert_refused(result, printer, errors=("safety_violation",))
    assert "even with QIDI_ALLOW_DANGEROUS_GCODE=1" in result["message"], result


@pytest.mark.parametrize(
    "script", ["PRINT_START_SET HOTEND=220 BED=60", "M4029 S60 R0", "M604 S220"]
)
async def test_in_envelope_deny_listed_forms_still_pass_with_the_operator_switch(
    call_tool, moonraker, allow_dangerous_gcode, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script, allow_dangerous=True)
    assert result["ok"] is True, result
    assert script in printer.commands


@pytest.mark.parametrize(
    "script",
    [
        # Reproduced (round-3 export review): Klipper keeps " HOTEND" as the key;
        # _print_start_phase_init re-renders it as HOTEND="374", the last HOTEND
        # wins, and _PRINT_START_PHASE_EXTRUDER sends M109 S374.
        'PRINT_START HOTEND=200 " HOTEND"=374 BED=60',
        'PRINT_START HOTEND=200 "\tHOTEND"=374 BED=60',
        'PRINT_START HOTEND=200 BED=60 " BED"=200',
        'PRINT_START HOTEND=200 "HOTEND "=374 BED=60',
        "PRINT_START HOTEND=200 'HOTEND\t'=374 BED=60",
        'CLEAR_NOZZLE " HOTEND"=374',
    ],
)
async def test_padded_or_quoted_parameter_names_are_refused(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("invalid_request",))


async def test_padded_parameter_names_are_refused_under_the_operator_switch(
    call_tool, moonraker, allow_dangerous_gcode
):
    _, printer = moonraker
    script = 'PRINT_START_SET HOTEND=200 " HOTEND"=374'
    result = await _drive(call_tool, "run_gcode", script=script, allow_dangerous=True)
    _assert_refused(result, printer, errors=("invalid_request",))


@pytest.mark.parametrize(
    "params",
    [
        {"HOTEND": 200, " HOTEND": 374, "BED": 60},
        {"HOTEND": 200, "\tHOTEND": 374, "BED": 60},
        {"HOTEND": 200, '"HOTEND"': 374, "BED": 60},
        {"HOTEND": 200, "HOTEND ": 374, "BED": 60},
        {"hotend": 200, "HOTEND": 374, "BED": 60},   # the same name twice
    ],
)
async def test_run_macro_refuses_padded_or_repeated_parameter_names(
    call_tool, moonraker, params
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro="PRINT_START", params=params)
    _assert_refused(result, printer, errors=("invalid_request",))


@pytest.mark.parametrize(
    "line",
    ['PRINT_START HOTEND=200 " HOTEND"=374', 'PRINT_START "HOTEND "=374',
     "PRINT_START HÖTEND=5"],
)
def test_extended_params_refuse_keys_klipper_would_keep_verbatim(line):
    assert klipper_extended_params(line) is None


@pytest.mark.parametrize(
    "script",
    [
        "SET_FILAMENT_SENSOR SENSOR=filament_switch_sensor ENABLE=0",
        "SET_FILAMENT_SENSOR SENSOR=box_motion_sensor ENABLE=0",
        "SET_FILAMENT_SENSOR SENSOR=filament_switch_sensor ENABLE=00",
        "SET_FILAMENT_SENSOR SENSOR=filament_switch_sensor ENABLE=off",
    ],
)
async def test_turning_off_a_filament_sensor_is_refused(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))


async def test_turning_a_filament_sensor_on_still_works(call_tool, moonraker):
    _, printer = moonraker
    script = "SET_FILAMENT_SENSOR SENSOR=filament_switch_sensor ENABLE=1"
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


@pytest.mark.parametrize(
    "script",
    ["SDCARD_PRINT_FILE FILENAME=benchy.gcode", "M23 benchy.gcode\nM24", "M24",
     "M32 benchy.gcode", "M23 benchy.gcode"],
)
async def test_raw_print_starts_are_refused(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))
    assert "start_print" in result.get("hint", ""), result


@pytest.mark.parametrize(
    "script",
    [
        "G91\nPARK Z=-5",                    # PARK switches to G90 itself
        "G91\nPARK X=9999",
        "G91\nG27 Z-1",
        "G91\nM600 Z-1",
        "G91\nG1 Z1\nG90\nG1 Z-5",            # back in absolute mode: checked again
        "G91\nRESTORE_GCODE_STATE NAME=x\nG1 Z-5",
    ],
)
async def test_the_z_floor_holds_after_a_relative_mode_switch(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


@pytest.mark.parametrize("script", ["G91\nG1 Z5\nG90", "G91\nG1 X-10 Y-10\nG90\nG1 X100"])
async def test_relative_moves_still_work(call_tool, moonraker, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result


@pytest.mark.skipif(
    not (ROOT / "research/upstream/qidi-max4/config/klipper-macros-qd").is_dir(),
    reason="optional GPLv3 vendor macros not fetched; run scripts/fetch_upstream_sources.sh",
)
def test_l5_reports_no_value_flow_behind_the_deny_list_alone():
    """L5 (e): no parameter flow relies on the deny-list alone, which the switch opens."""
    proc = subprocess.run(
        [sys.executable, "docs/security/proofs/L5_denylist.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "QIDI_TEST_MODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    found = re.search(r"parameter flows covered by the deny-list alone: (\d+)", proc.stdout)
    assert found, proc.stdout[-3000:]
    assert int(found.group(1)) == 0, proc.stdout[-3000:]
    found = re.search(r"forwarded parameter names NOT validated: (\d+)", proc.stdout)
    assert found, proc.stdout[-3000:]
    assert int(found.group(1)) == 0, proc.stdout[-3000:]


# --------------------------------------------------------------------------
# 11. Classic (letter + number) values that Klipper keeps verbatim
# --------------------------------------------------------------------------
#
# Klipper's letter parser keeps whatever follows a parameter name, so in
# ``G27 P1 Z=20`` the value of Z is "=20". Its native handlers reject that, but
# the G27/M600 macros forward it to PARK as Z="=20", and PARK's |float reads it as
# 0.0. The screen used to strip the '=' and approve Z=20, so the toolhead went to
# Z=0 under a raised QIDI_MIN_JOG_Z floor. The same happened with an empty value
# ("G27 P1 Z" -> Z="" -> 0.0) and a quoted one (the screen stripped the quotes).


def _load_vendored_klipper_parser():
    """Klipper v0.13.0's gcode.py, vendored under research/upstream (stdlib only)."""
    import importlib.util

    path = ROOT / "research" / "upstream" / "klipper_gcode_parser.py"
    spec = importlib.util.spec_from_file_location("_vendored_klipper_gcode", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _klipper_parse(line: str) -> tuple[str, dict[str, str]]:
    """Run Klipper's own ``_process_commands`` on one line; return (cmd, params)."""
    gcode = _load_vendored_klipper_parser()
    seen: list = []

    class _Dispatch:
        args_r = gcode.GCodeDispatch.args_r
        gcode_handlers: dict = {}
        error = gcode.CommandError

        def respond_info(self, *_a, **_k):
            pass

        respond_raw = respond_info

        def cmd_default(self, gcmd):
            seen.append(gcmd)

    gcode.GCodeDispatch._process_commands(_Dispatch(), [line], need_ack=False)
    (gcmd,) = seen
    return gcmd.get_command(), dict(gcmd.get_command_parameters())


@pytest.mark.parametrize(
    "line",
    [
        "G27 P1 Z=20", "G27 P1 Z==20", "G27 P1 Z = 20", "G27 P1 Z", "M600 X=100 Y=100 Z=20",
        'G27 P1 Z"20"', "M104 S200", "M104S200", "N5 G1 X10 Y20*42", "G1 X10 ; Z=0",
        "  m104 s200  ", "M203 X300Y400", "M600 XY5", "G1 X1_0", "M104.6245197 S200",
    ],
)
@pytest.mark.skipif(
    not (ROOT / "research/upstream/klipper_gcode_parser.py").is_file(),
    reason="optional GPLv3 Klipper parser not fetched; run scripts/fetch_upstream_sources.sh",
)
def test_classic_params_match_klippers_own_parser(line):
    from qidi_mcp.limits import klipper_command_name, klipper_params

    cmd, params = _klipper_parse(line)
    assert klipper_command_name(line) == cmd
    assert klipper_params(line) == params


def test_classic_values_keep_their_separator():
    assert command_params("G27 P1 Z=20")["Z"] == "=20"
    assert command_params("M104 S220")["S"] == "220"


@pytest.fixture
def raised_z_floor(server_rt):
    """QIDI_MIN_JOG_Z=10 on the live settings."""
    from dataclasses import replace

    _, runtime = server_rt
    settings = runtime.policy.settings
    runtime.policy.settings = replace(settings, safety=replace(settings.safety, min_jog_z=10.0))
    runtime.settings = runtime.policy.settings
    # sync_limits rebuilds the envelope from the capability probe's settings.
    runtime.caps.settings = runtime.policy.settings
    return runtime


@pytest.mark.parametrize(
    "script",
    [
        "G27 P1 Z=20",                      # reproduced: PARK Z="=20" -> Z=0.0
        "G27 P1 Z==20",
        "G27 P1 Z = 20",
        "G27 P1 X=100 Y=100 Z=20",
        "M600 Z=20",                        # PAUSE ... Z="=20" -> PARK -> Z=0.0
        "M600 X=100 Y=100 Z=20",
        "M600 Z20=",
        "G27 P1 Z",                         # empty: PARK Z="" -> 0.0
        "M600 Z",
        'G27 P1 Z"20"',                     # quoted: the screen stripped the quotes
        "PARK P=1 Z=",
        'PARK P=1 Z=""',
        "PARK P=1 Z='\"20\"'",
        "G1 Z=20",                          # native handlers fail on it anyway
        "M104 S=200",
        "M600 R=220",
    ],
)
async def test_classic_values_klipper_cannot_read_are_refused(
    call_tool, moonraker, raised_z_floor, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("invalid_request",))


@pytest.mark.parametrize("script", ["G27 P1 Z5", "M600 Z5", "PARK P=1 Z=5"])
async def test_the_raised_z_floor_holds_for_park_macros(
    call_tool, moonraker, raised_z_floor, script
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


@pytest.mark.parametrize(
    "script", ["G27 P1 Z20", "M600 X100 Y100 Z20", "PARK P=1 Z=20", "M117 Z=0 is a message"]
)
async def test_readable_classic_values_still_work(call_tool, moonraker, raised_z_floor, script):
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    assert result["ok"] is True, result
    assert script in printer.commands


async def test_run_macro_writes_classic_values_without_equals(call_tool, moonraker, raised_z_floor):
    """run_macro("M600", {"Z": 20}) used to send M600 Z=20, which parks at Z=0."""
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro="M600", params={"Z": 20})
    assert result["ok"] is True, result
    assert "M600 Z20" in printer.commands
    result = await _drive(call_tool, "run_macro", macro="M104", params={"S": 220})
    assert result["ok"] is True, result
    assert "M104 S220" in printer.commands


@pytest.mark.parametrize(
    ("macro", "params"),
    [("M600", {"Z": "=20"}), ("M600", {"Z": ""}), ("M600", {"Z": "2 0"}), ("M104", {"S": "2e2"}),
     ("M600", {"Z1": 20})],
)
async def test_run_macro_refuses_classic_values_that_are_not_numbers(
    call_tool, moonraker, raised_z_floor, macro, params
):
    _, printer = moonraker
    result = await _drive(call_tool, "run_macro", macro=macro, params=params)
    _assert_refused(result, printer, errors=("invalid_request",))


@pytest.mark.parametrize(
    "script",
    ["M104 S200 S374", "SET_HEATER_TEMPERATURE_SCALED HEATER=extruder TARGET=200 TARGET=374"],
)
async def test_run_gcode_checks_a_repeated_name_at_its_last_value(call_tool, moonraker, script):
    """Klipper keeps the last of a repeated name; the screen checks that one (docs)."""
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer)


@pytest.mark.parametrize(
    "script",
    ["SDCARD_PRINT_FILE FILENAME=benchy.gcode",
     "SET_FILAMENT_SENSOR SENSOR=filament_switch_sensor ENABLE=0"],
)
async def test_print_starts_and_sensor_off_reopen_only_with_both_switches(
    call_tool, moonraker, allow_dangerous_gcode, script
):
    """As documented: the operator switch plus allow_dangerous reopens these."""
    _, printer = moonraker
    result = await _drive(call_tool, "run_gcode", script=script)
    _assert_refused(result, printer, errors=("safety_violation",))
    result = await _drive(call_tool, "run_gcode", script=script, allow_dangerous=True)
    assert result["ok"] is True, result
    assert script in printer.commands
