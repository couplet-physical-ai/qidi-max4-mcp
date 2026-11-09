"""The master queue's filament gate: may this file start on this printer unattended?

``printfit.assess`` is advisory because a person presses the button. ``dispatch_fit``
is a decision, made with nobody at the machine, so the tests are about the cases
where missing or contradicting information must come out as "no": a Box that was
never assigned, no declaration, a declared PLA under a PETG file, a colour that
only differs, a multi-filament file nobody can route.

The ID tables are QIDI's real ``officiall_filas_list.cfg``, like the Box tests, so
name resolution and type lookups run against what the firmware ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qidi_mcp.box import BoxState, parse_filament_table
from qidi_mcp.printfit import dispatch_fit

TABLE_PATH = Path(__file__).parent.parent / "research/upstream/qidi-max4/config/officiall_filas_list.cfg"
if not TABLE_PATH.exists():
    pytest.skip("Optional QIDI GPLv3 table absent; run bash scripts/fetch_upstream_sources.sh first", allow_module_level=True)
REAL = TABLE_PATH.read_text()
TABLE = parse_filament_table(REAL)

PETG_BLACK = {"filament_type": "PETG", "filament_colors": ["#000000"]}
PETG_WHITE = {"filament_type": "PETG", "filament_colors": ["#FFFFFF"]}
PLA_FILE = {"filament_type": "PLA", "filament_colors": ["#FFFFFF"]}
TWO_TOOL = {"filament_type": "PLA;PETG", "filament_colors": ["#FFFFFF", "#000000"]}

DECLARED_PETG_BLACK = {"material": "PETG Basic", "material_type": "PETG", "color": "black",
                       "color_hex": "#060606", "verified": True}
DECLARED_PLA = {"material": "PLA Basic", "material_type": "PLA", "color": "white",
                "color_hex": "#FAFAFA", "verified": True}


def _box(**ids) -> BoxState:
    """A ready one-unit Box with slot 0 occupied; ids default to the factory 1/1/1."""
    variables = {
        "box_count": 1, "enable_box": 1, "slot0": 1, "slot1": 0, "slot2": 0, "slot3": 0,
        **{f"filament_slot{i}": 1 for i in range(17)},
        **{f"color_slot{i}": 1 for i in range(17)},
        **{f"vendor_slot{i}": 1 for i in range(17)},
        **ids,
    }
    return BoxState(variables=variables, extras={"is_tool_change": 0}, controller_present=True)


# --- the Box, when it is real evidence -------------------------------------------------


def test_a_ready_assigned_box_with_the_right_spool_is_eligible():
    box = _box(filament_slot0=39, color_slot0=2)       # PETG Basic, near-black
    fit = dispatch_fit(PETG_BLACK, box, TABLE, None, pinned=False)
    assert fit["basis"] == "box" and fit["verdict"] == "ok" and fit["eligible"] is True


def test_a_box_with_factory_ids_is_not_evidence_and_falls_back_to_the_declaration():
    """All 1/1/1 is the signature of slots nobody has set, not of a white PLA spool."""
    fit = dispatch_fit(PETG_BLACK, _box(), TABLE, None, pinned=False)
    assert fit["basis"] == "none" and fit["eligible"] is False
    assert "never been assigned" in fit["reason"]
    fit = dispatch_fit(PETG_BLACK, _box(), TABLE, DECLARED_PETG_BLACK, pinned=False)
    assert fit["basis"] == "declared" and fit["eligible"] is True


def test_a_disabled_box_defers_to_the_declaration():
    box = BoxState(variables={**_box(filament_slot0=39, color_slot0=2).variables, "enable_box": 0},
                   extras={}, controller_present=True)
    fit = dispatch_fit(PETG_BLACK, box, TABLE, DECLARED_PETG_BLACK, pinned=False)
    assert fit["basis"] == "declared" and fit["eligible"] is True


def test_the_box_wins_over_a_contradicting_declaration():
    """A declaration is a person's memory; the Box's record is the newer evidence."""
    box = _box(filament_slot0=7, color_slot0=1)        # PLA Basic, white
    fit = dispatch_fit(PETG_BLACK, box, TABLE, DECLARED_PETG_BLACK, pinned=True,
                       accept_unverified=True)
    assert fit["basis"] == "box" and fit["verdict"] == "mismatch" and fit["eligible"] is False


def test_a_box_colour_difference_needs_the_waiver():
    box = _box(filament_slot0=39, color_slot0=1)       # PETG, white
    fit = dispatch_fit(PETG_BLACK, box, TABLE, None, pinned=False)
    assert fit["verdict"] == "check" and fit["eligible"] is False
    fit = dispatch_fit(PETG_BLACK, box, TABLE, None, pinned=False, allow_color_mismatch=True)
    assert fit["eligible"] is True


def test_an_unreadable_table_makes_the_box_no_evidence():
    box = _box(filament_slot0=39, color_slot0=2)
    fit = dispatch_fit(PETG_BLACK, box, None, None, pinned=False)
    assert fit["basis"] == "none" and fit["eligible"] is False


def test_a_file_without_a_material_is_unknown_on_the_box_path_too():
    """Review finding: a colour alone matched a Box slot and came out 'ok'.

    The declared path already required a material; the Box path let a file that
    named only a colour start unpinned on the strength of a similar swatch.
    """
    box = _box(filament_slot0=39, color_slot0=2)
    fit = dispatch_fit({"filament_colors": ["#000000"]}, box, TABLE, None, pinned=False)
    assert fit["basis"] == "box" and fit["verdict"] == "unknown" and fit["eligible"] is False
    assert dispatch_fit({"filament_colors": ["#000000"]}, box, TABLE, None, pinned=True,
                        accept_unverified=True)["eligible"] is True


def test_the_box_must_agree_in_every_loaded_slot_because_the_feeding_slot_is_unknown():
    """Review finding: 'ok' meant some slot holds the material, not the slot that feeds.

    Slot routing is unverified, so a Box with PETG in slot 0 and PLA in slot 1 is
    not evidence that a PETG file will be fed PETG. Only when every loaded slot
    agrees (or exactly one is loaded) is the Box an 'ok'; anything else is
    unknown, which starts only where a person pinned it and accepted that.
    """
    agree = _box(filament_slot0=39, color_slot0=2, slot1=1, filament_slot1=39, color_slot1=2)
    assert dispatch_fit(PETG_BLACK, agree, TABLE, None, pinned=False)["verdict"] == "ok"
    mixed = _box(filament_slot0=39, color_slot0=2, slot1=1, filament_slot1=7, color_slot1=1)
    fit = dispatch_fit(PETG_BLACK, mixed, TABLE, None, pinned=False)
    assert fit["basis"] == "box" and fit["verdict"] == "unknown" and fit["eligible"] is False
    assert "slot 1" in fit["reason"] and "feeds" in fit["reason"]
    assert dispatch_fit(PETG_BLACK, mixed, TABLE, None, pinned=True, accept_unverified=True)["eligible"]
    # A second loaded slot with the right material but another colour is a colour check.
    shade = _box(filament_slot0=39, color_slot0=2, slot1=1, filament_slot1=39, color_slot1=1)
    fit = dispatch_fit(PETG_BLACK, shade, TABLE, None, pinned=False)
    assert fit["verdict"] == "check" and fit["eligible"] is False
    assert dispatch_fit(PETG_BLACK, shade, TABLE, None, pinned=False, allow_color_mismatch=True)["eligible"]


# --- the declared extruder filament (Amendment A) ------------------------------------------


@pytest.mark.parametrize("box", [None, BoxState(variables={"box_count": 0})])
def test_a_matching_declaration_is_enough_without_a_box(box):
    fit = dispatch_fit(PETG_BLACK, box, TABLE, DECLARED_PETG_BLACK, pinned=False)
    assert fit == {**fit, "basis": "declared", "verdict": "ok", "eligible": True}
    assert "PETG" in fit["reason"]


def test_the_wrong_declared_material_is_a_mismatch_even_when_pinned_and_acknowledged():
    """The acknowledgement covers 'nobody checked', never 'it is known to be wrong'."""
    fit = dispatch_fit(PETG_BLACK, None, TABLE, DECLARED_PLA, pinned=True,
                       accept_unverified=True, allow_color_mismatch=True)
    assert fit["verdict"] == "mismatch" and fit["eligible"] is False
    assert "wants PETG" in fit["reason"] and "PLA" in fit["reason"]


def test_a_declared_colour_that_differs_is_check_and_needs_the_waiver():
    fit = dispatch_fit(PETG_WHITE, None, TABLE, DECLARED_PETG_BLACK, pinned=False)
    assert fit["verdict"] == "check" and fit["eligible"] is False
    assert "white" in fit["reason"]
    fit = dispatch_fit(PETG_WHITE, None, TABLE, DECLARED_PETG_BLACK, pinned=False,
                       allow_color_mismatch=True)
    assert fit["eligible"] is True


def test_a_declaration_without_a_colour_is_check_when_the_file_names_one():
    bare = {"material": "PETG", "material_type": "PETG"}
    fit = dispatch_fit(PETG_BLACK, None, TABLE, bare, pinned=False)
    assert fit["verdict"] == "check" and fit["eligible"] is False
    assert dispatch_fit({"filament_type": "PETG"}, None, TABLE, bare, pinned=False)["eligible"]


def test_a_file_with_no_material_is_unknown_whatever_is_declared():
    fit = dispatch_fit({"estimated_time": 100}, None, TABLE, DECLARED_PETG_BLACK, pinned=False)
    assert fit["verdict"] == "unknown" and fit["eligible"] is False
    assert dispatch_fit({}, None, TABLE, DECLARED_PETG_BLACK, pinned=True,
                        accept_unverified=True)["eligible"] is True


def test_the_material_type_is_what_matters_not_the_profile_name():
    declared = {"material": "PETG Tough", "material_type": "PETG", "color_hex": "#060606"}
    assert dispatch_fit(PETG_BLACK, None, TABLE, declared, pinned=False)["eligible"]
    free_text = {"material": "petg", "verified": False, "color_hex": "#060606"}
    assert dispatch_fit(PETG_BLACK, None, TABLE, free_text, pinned=False)["eligible"]


# --- nothing known ------------------------------------------------------------------------


def test_no_box_and_no_declaration_is_unknown_and_pinned_acknowledged_only():
    fit = dispatch_fit(PETG_BLACK, None, TABLE, None, pinned=False)
    assert fit["verdict"] == "unknown" and fit["eligible"] is False
    assert "no loaded filament declared" in fit["reason"]
    assert not dispatch_fit(PETG_BLACK, None, TABLE, None, pinned=True)["eligible"]
    assert not dispatch_fit(PETG_BLACK, None, TABLE, None, pinned=False,
                            accept_unverified=True)["eligible"]
    assert dispatch_fit(PETG_BLACK, None, TABLE, None, pinned=True,
                        accept_unverified=True)["eligible"]


# --- several filaments ----------------------------------------------------------------------


def test_a_multi_filament_file_only_runs_where_it_was_pinned_and_acknowledged():
    box = _box(filament_slot0=7, color_slot0=1, slot1=1, filament_slot1=39, color_slot1=2)
    fit = dispatch_fit(TWO_TOOL, box, TABLE, DECLARED_PETG_BLACK, pinned=False)
    assert fit["basis"] == "none" and fit["eligible"] is False and len(fit["wants"]) == 2
    assert "not published" in fit["reason"]
    assert not dispatch_fit(TWO_TOOL, box, TABLE, None, pinned=True)["eligible"]
    assert dispatch_fit(TWO_TOOL, box, TABLE, None, pinned=True, accept_unverified=True)["eligible"]
