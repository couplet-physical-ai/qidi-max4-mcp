"""AI failure detection, and the three gaps found by going live against a Max4.

1. ``upload_gcode`` rejected ``.gcode.3mf`` -- the printer's native format.
2. ``SET_AI_DETECT_RESULT`` passed the raw-G-code screen as harmless, but it
   forges (or clears) the vision service's failure verdict.
3. There was no way to read or set AI detection at all.
"""

from __future__ import annotations

import base64

import pytest

from qidi_mcp.limits import screen_gcode
from qidi_mcp.tools.files import _GCODE_SUFFIXES


# --------------------------------------------------------------------------
# 1. the printer's native upload format
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "part.gcode",
        "part.g",
        "part.gco",
        "part.gcode.3mf",  # what OrcaSlicer exports and QIDI's screen writes
        "PART.GCODE.3MF",  # case must not matter
    ],
)
async def test_upload_accepts_printable_formats(call_tool, printer, name):
    r = await call_tool(
        "upload_gcode",
        filename=name,
        content_base64=base64.b64encode(b"G28\nM104 S200\n").decode(),
        confirm=True,
    )
    assert r["ok"] is True, r
    assert r["uploaded"] == name


async def test_upload_still_refuses_a_bare_project_3mf(call_tool):
    """A plain .3mf may carry no toolpath at all; Klipper cannot print it."""
    r = await call_tool(
        "upload_gcode",
        filename="model.3mf",
        content_base64=base64.b64encode(b"PK\x03\x04").decode(),
        confirm=True,
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_request"
    # The hint must name the formats, so the caller can fix it without guessing.
    assert ".gcode.3mf" in r["hint"]


def test_bare_3mf_is_not_in_the_accepted_suffixes():
    assert ".gcode.3mf" in _GCODE_SUFFIXES
    assert ".3mf" not in _GCODE_SUFFIXES


# --------------------------------------------------------------------------
# 2. a forged AI verdict must not reach the printer
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "SET_AI_DETECT_RESULT TYPE=noodle STATE=1",
        "set_ai_detect_result type=noodle state=1",
        "N7 SET_AI_DETECT_RESULT TYPE=foreign STATE=1",  # Klipper skips line numbers
        "SET_AI_DETECT_RESULT*22 TYPE=noodle STATE=1",  # checksum suffix
        "SET_AI_DETECT_RESULT TYPE=noodle STATE=0",  # clearing a REAL detection
    ],
)
def test_forging_an_ai_verdict_is_screened_as_dangerous(line):
    screen = screen_gcode(line)
    assert screen.is_dangerous is True, f"{line!r} slipped through as safe"


async def test_run_gcode_refuses_to_forge_a_detection(call_tool, printer):
    """STATE=1 would fake a failure; STATE=0 would suppress a real one."""
    for line in (
        "SET_AI_DETECT_RESULT TYPE=noodle STATE=1",
        "SET_AI_DETECT_RESULT TYPE=noodle STATE=0",
    ):
        r = await call_tool("run_gcode", script=line, confirm=True)
        assert r["ok"] is False, f"{line!r} was accepted"

    # Nothing landed: no forged verdict, and the command never hit the wire.
    assert printer.ai_counts == 0
    assert printer.ai_error_type == ""
    assert not any("SET_AI_DETECT_RESULT" in c.upper() for c in printer.commands)


# --------------------------------------------------------------------------
# 3. reading and setting detection
# --------------------------------------------------------------------------
async def test_status_reports_both_detectors_and_flags_that_off_means_unknown(call_tool):
    r = await call_tool("get_ai_detection_status")
    assert r["ok"] is True
    assert r["detectors"]["noodle"]["enabled"] is False
    assert r["detectors"]["foreign"]["enabled"] is False
    assert r["detectors"]["foreign"]["sensitivity"] == "MEDIUM"
    assert r["any_enabled"] is False
    # The note must stop a caller reading counts==0 from a disabled detector as
    # "the print is fine" -- that was the trap when qualifying the real printer.
    assert "meaningless" in r["note"].lower()


async def test_enabling_is_free_because_friction_would_discourage_the_safe_action(
    call_tool, printer
):
    r = await call_tool(
        "set_ai_detection", detector="foreign", enabled=True, sensitivity="high"
    )
    assert r["ok"] is True, r
    assert printer.ai_detect["foreign"] == {"enable": 1, "sensitivity": "HIGH"}

    status = await call_tool("get_ai_detection_status")
    assert status["detectors"]["foreign"]["enabled"] is True
    assert status["any_enabled"] is True
    assert "meaningless" not in status["note"].lower()


async def test_disabling_detection_is_also_gated(call_tool, printer):
    """Turning the safety net off must not be a free action."""
    await call_tool("set_ai_detection", detector="noodle", enabled=True)
    assert printer.ai_detect["noodle"]["enable"] == 1

    unconfirmed = await call_tool("set_ai_detection", detector="noodle", enabled=False)
    assert unconfirmed["ok"] is False
    assert printer.ai_detect["noodle"]["enable"] == 1, "detection was disabled without confirm"

    confirmed = await call_tool(
        "set_ai_detection", detector="noodle", enabled=False, confirm=True
    )
    assert confirmed["ok"] is True
    assert printer.ai_detect["noodle"]["enable"] == 0


@pytest.mark.parametrize(
    ("kwargs", "bad"),
    [
        ({"detector": "spaghetti", "enabled": True}, "detector"),
        ({"detector": "noodle", "enabled": True, "sensitivity": "MAXIMUM"}, "sensitivity"),
    ],
)
async def test_bad_arguments_are_refused_with_a_usable_hint(call_tool, printer, kwargs, bad):
    r = await call_tool("set_ai_detection", confirm=True, **kwargs)
    assert r["ok"] is False
    assert r["error"] == "invalid_request"
    assert bad in (r.get("hint", "") + r.get("message", "")).lower() or r.get("hint")
    assert not any("AI_DETECT" in c.upper() for c in printer.commands)
