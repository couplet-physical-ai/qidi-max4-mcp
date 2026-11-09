"""Saying what is in each Box slot, and queueing prints without memorising filenames.

Like test_box.py this proves the server's logic against a mock: no Box has been connected
to it. The ID tables used here are QIDI's *real* ``officiall_filas_list.cfg`` (50 filaments,
24 colours), because name resolution is exactly the part where a toy table would hide a bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qidi_mcp.box import parse_filament_table
from qidi_mcp.errors import InvalidRequest
from qidi_mcp.filament_names import (
    colors_similar,
    resolve_color,
    resolve_filament,
    resolve_vendor,
    search_filaments,
)
from qidi_mcp.gcode_names import MAX_NAMES, resolve_gcode_name, resolve_gcode_names
from qidi_mcp.printfit import assess, wanted_filaments

TABLE_PATH = Path(__file__).parent.parent / "research/upstream/qidi-max4/config/officiall_filas_list.cfg"
if not TABLE_PATH.exists():
    pytest.skip("Optional QIDI GPLv3 table absent; run bash scripts/fetch_upstream_sources.sh first", allow_module_level=True)
REAL = TABLE_PATH.read_text()
TABLE = parse_filament_table(REAL)


def _boxed(printer, **vars_):
    """A one-Box printer with the real ID tables and two occupied slots."""
    printer.attach_box(1)
    printer.filament_table = REAL
    printer.saved_variables.update(slot0=1, slot1=1, slot2=0, **vars_)
    return printer


# --------------------------------------------------------------------------
# Filament names -> ids (pure, against QIDI's real table)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "fid", "assumed"),
    [
        ("PETG Basic", 39, False),
        ("petg basic", 39, False),
        ("PLA Matte", 2, False),
        ("pla-cf", 5, False),
        ("ASA", 18, False),
        ("PVA", 47, False),
        ("TPU", 50, True),  # the only TPU-typed profile, but still a guess about brand
        ("silk", 4, False),
        ("PETG", 39, True),  # a bare material -> its generic profile, and it says so
        ("pla", 7, True),
        (39, 39, False),
        ("39", 39, False),
    ],
)
def test_filament_names_resolve_to_the_right_profile(query, fid, assumed):
    choice = resolve_filament(TABLE.filaments, query)
    assert choice.id == fid
    assert choice.assumed is assumed
    assert choice.matched_by  # always explains itself


@pytest.mark.parametrize(
    ("query", "expect_in_candidates"),
    [
        ("ABS", "ABS Rapido"),
        ("matte", "PLA Matte Basic"),
        ("PA", "UltraPA"),
        ("nylon", None),
        ("definitely not a filament", None),
    ],
)
def test_ambiguous_or_unknown_filaments_are_errors_not_guesses(query, expect_in_candidates):
    with pytest.raises(InvalidRequest) as err:
        resolve_filament(TABLE.filaments, query)
    names = [c["name"] for c in err.value.extra.get("candidates", [])]
    if expect_in_candidates:
        assert expect_in_candidates in names
        assert len(names) > 1


def test_bad_filament_ids_and_types_are_rejected():
    for bad in (0, 999, -1, True, None, "", "   "):
        with pytest.raises(InvalidRequest):
            resolve_filament(TABLE.filaments, bad)


def test_an_empty_table_is_an_error_not_a_crash():
    with pytest.raises(InvalidRequest):
        resolve_filament({}, "PLA")
    with pytest.raises(InvalidRequest):
        resolve_color({}, "red")


def test_search_filters_by_every_word():
    names = [f["name"] for f in search_filaments(TABLE.filaments, "pla matte")]
    assert names == ["PLA Matte", "PLA Matte Basic"]
    # QIDI's file has 50 [filaN] sections but only 43 are named; blanks are not choices
    assert len(search_filaments(TABLE.filaments, None)) == 43
    assert search_filaments(TABLE.filaments, "zzzz") == []
    # id 9, 16, 21 ... are blank in the real file and must not surface as nameless rows
    assert all(f["name"] for f in search_filaments(TABLE.filaments, ""))


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "cid"),
    [
        ("black", 2),
        ("White", 1),
        ("red", 18),
        ("dark blue", 11),
        ("light-blue", 10),
        ("light_blue", 10),
        ("orange", 23),
        ("brown", 21),
        ("#1353AB", 14),
        ("1353ab", 14),
        (14, 14),
        ("14", 14),
    ],
)
def test_colours_resolve_to_the_printers_nearest_swatch(query, cid):
    assert resolve_color(TABLE.colors, query).id == cid


def test_a_hex_that_is_not_in_the_palette_snaps_and_is_marked_approximate():
    pure_red = resolve_color(TABLE.colors, "#FF0000")
    assert pure_red.id == 18 and pure_red.approximate is True
    assert resolve_color(TABLE.colors, "#1353AB").approximate is False
    # words are near by nature; a word that lands on its swatch is not flagged
    assert resolve_color(TABLE.colors, "red").approximate is False


@pytest.mark.parametrize("bad", ["teal", "transparent", "", "#12", "#GGGGGG", 0, 25, True, None])
def test_unknown_colours_are_rejected_with_the_palette(bad):
    with pytest.raises(InvalidRequest) as err:
        resolve_color(TABLE.colors, bad)
    if isinstance(bad, str) and bad:
        assert len(err.value.extra["palette"]) == 24


def test_colour_similarity_is_perceptual_not_a_hex_string_compare():
    assert colors_similar("#FF362D", "#FF0000") is True
    assert colors_similar("#060606", "#FAFAFA") is False
    assert colors_similar("#2850FF", "#1714B0") is False  # blue vs dark blue
    assert colors_similar(None, "#FAFAFA") is None
    assert colors_similar("not a colour", "#FAFAFA") is None


def test_vendors():
    assert resolve_vendor(TABLE.vendors, "qidi") == (1, "QIDI")
    assert resolve_vendor(TABLE.vendors, " Generic ") == (0, "Generic")
    assert resolve_vendor(TABLE.vendors, 1) == (1, "QIDI")
    for bad in ("Prusament", 7, True, None):
        with pytest.raises(InvalidRequest):
            resolve_vendor(TABLE.vendors, bad)


# --------------------------------------------------------------------------
# Loose gcode names (pure)
# --------------------------------------------------------------------------

FILES = ["benchy.gcode", "projects/bracket_abs.gcode", "projects/bracket_pla.gcode", "Cube.gcode"]


@pytest.mark.parametrize(
    ("typed", "path"),
    [
        ("benchy.gcode", "benchy.gcode"),
        ("benchy", "benchy.gcode"),
        ("BENCHY", "benchy.gcode"),
        ("/benchy.gcode", "benchy.gcode"),
        ("cube", "Cube.gcode"),
        ("bracket abs", "projects/bracket_abs.gcode"),
        ("bracket_pla", "projects/bracket_pla.gcode"),
        ("projects/bracket_abs.gcode", "projects/bracket_abs.gcode"),
    ],
)
def test_loose_names_resolve_to_one_file(typed, path):
    assert resolve_gcode_name(typed, FILES) == path


def test_ambiguous_names_list_candidates_instead_of_picking():
    with pytest.raises(InvalidRequest) as err:
        resolve_gcode_name("bracket", FILES)
    assert err.value.extra["candidates"] == [
        "projects/bracket_abs.gcode",
        "projects/bracket_pla.gcode",
    ]


def test_unknown_names_suggest_near_misses():
    with pytest.raises(InvalidRequest) as err:
        resolve_gcode_name("benchyy.gcode", FILES)
    assert "benchy.gcode" in err.value.extra["candidates"]
    with pytest.raises(InvalidRequest):
        resolve_gcode_name("", FILES)


def test_one_bad_name_fails_the_whole_batch():
    with pytest.raises(InvalidRequest):
        resolve_gcode_names(["benchy", "no such file"], FILES)
    with pytest.raises(InvalidRequest):
        resolve_gcode_names([], FILES)
    with pytest.raises(InvalidRequest):
        resolve_gcode_names(["benchy"] * (MAX_NAMES + 1), FILES)
    with pytest.raises(InvalidRequest):
        resolve_gcode_names([7], FILES)  # type: ignore[list-item]


# --------------------------------------------------------------------------
# Fit report (pure)
# --------------------------------------------------------------------------


def _slot(n, ftype, color, loaded=True):
    return {
        "slot": n,
        "loaded": loaded,
        "label": f"slot {n}",
        "filament": {"type": ftype} if ftype else None,
        "color_hex": color,
    }


def test_wanted_filaments_read_the_slicers_lists():
    meta = {"filament_type": "PLA;PETG", "filament_colors": ["#ff0000", "#00FF00"],
            "referenced_tools": [0, 1]}
    assert wanted_filaments(meta) == [
        {"tool": 0, "type": "PLA", "color_hex": "#FF0000"},
        {"tool": 1, "type": "PETG", "color_hex": "#00FF00"},
    ]
    # a single-filament file with nothing else still has one thing to compare
    assert wanted_filaments({"filament_type": "ABS"}) == [
        {"tool": 0, "type": "ABS", "color_hex": None}
    ]
    assert wanted_filaments({}) == [{"tool": 0, "type": None, "color_hex": None}]


def test_matching_material_and_colour_is_ok():
    r = assess({"filament_type": "PLA", "filament_colors": ["#060606"]},
               [_slot(0, "PLA", "#060606"), _slot(1, "PETG", "#FAFAFA")])
    assert r["verdict"] == "ok"
    assert [s["slot"] for s in r["wants"][0]["slots"]] == [0]


def test_the_wrong_material_is_a_mismatch_and_names_what_is_loaded():
    r = assess({"filament_type": "ABS"}, [_slot(0, "PLA", None), _slot(1, "PETG", None)])
    assert r["verdict"] == "mismatch"
    assert "ABS" in r["summary"] and "PETG" in r["summary"] and "PLA" in r["summary"]


def test_right_material_wrong_colour_is_check_not_mismatch():
    r = assess({"filament_type": "PLA", "filament_colors": ["#FF362D"]},
               [_slot(0, "PLA", "#FAFAFA"), _slot(1, "PLA", "#060606")])
    assert r["verdict"] == "check"


def test_cf_filament_is_not_plain_pla():
    """A carbon-fibre spool is abrasive; "PLA" must not silently accept it."""
    r = assess({"filament_type": "PLA"}, [_slot(0, "PLA-CF", None)])
    assert r["verdict"] == "mismatch"


def test_an_empty_box_is_a_mismatch_and_unread_tables_are_unknown_not_ok():
    assert assess({"filament_type": "PLA"}, [_slot(0, "PLA", None, loaded=False)])["verdict"] == "mismatch"
    r = assess({"filament_type": "PLA"}, [_slot(0, None, None)])
    assert r["verdict"] == "unknown"  # could not name the slot's material: not a match


def test_a_file_that_says_nothing_cannot_be_called_ok():
    assert assess({}, [_slot(0, "PLA", "#FAFAFA")])["verdict"] == "unknown"


def test_multi_colour_files_are_judged_per_tool_and_the_worst_wins():
    meta = {"filament_type": "PLA;PETG", "filament_colors": ["#FAFAFA", "#2850FF"],
            "referenced_tools": [0, 1]}
    r = assess(meta, [_slot(0, "PLA", "#FAFAFA"), _slot(1, "PLA", "#FAFAFA")])
    assert [w["verdict"] for w in r["wants"]] == ["ok", "mismatch"]
    assert r["verdict"] == "mismatch"


def test_the_report_never_claims_to_know_the_routing():
    assert "not verified" in assess({"filament_type": "PLA"}, [_slot(0, "PLA", None)])["note"]


# --------------------------------------------------------------------------
# get_box_status / list_box_filament_options
# --------------------------------------------------------------------------


async def test_status_has_one_readable_line_per_slot(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer, filament_slot0=2, color_slot0=2, vendor_slot0=1)
    r = await call_tool("get_box_status")
    assert r["summary"][0] == "slot 0 (unit 1, position 1): PLA Matte, black (QIDI)"
    assert r["summary"][2].startswith("slot 2 (unit 1, position 3): empty (last recorded:")
    assert len(r["summary"]) == 4


async def test_list_options_searches_the_printers_own_table(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer)
    r = await call_tool("list_box_filament_options", query="petg")
    assert r["ok"] is True
    assert {f["name"] for f in r["filaments"]} >= {"PETG Basic", "PETG-CF", "PETG Rapido"}
    assert "PLA Matte" not in {f["name"] for f in r["filaments"]}
    assert len(r["colors"]) == 24 and r["colors"][1]["name"] == "black"
    assert {"id": 1, "name": "QIDI"} in r["vendors"]


async def test_list_options_needs_the_table(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.filament_table = None
    r = await call_tool("list_box_filament_options")
    assert r["ok"] is False and r["error"] == "not_supported"


# --------------------------------------------------------------------------
# set_box_slot_filaments
# --------------------------------------------------------------------------


def _writes(printer):
    return [c for c in printer.commands if "SAVE_VARIABLE" in c]


async def test_naming_a_slot_previews_first_and_sends_nothing(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer)
    r = await call_tool(
        "set_box_slot_filaments",
        assignments=[{"slot": 0, "filament": "PETG Basic", "color": "dark blue", "vendor": "Generic"}],
    )
    assert r["ok"] is False  # a ticket, not an application
    plan = r["will_set"][0]
    assert plan["filament"]["id"] == 39 and plan["color"]["id"] == 11 and plan["vendor"]["id"] == 0
    assert plan["after"] == "slot 0 (unit 1, position 1): PETG Basic, dark blue (Generic)"
    assert sorted(r["will_send"]) == [
        "SAVE_VARIABLE VARIABLE=color_slot0 VALUE=11",
        "SAVE_VARIABLE VARIABLE=filament_slot0 VALUE=39",
        "SAVE_VARIABLE VARIABLE=vendor_slot0 VALUE=0",
    ]
    assert _writes(printer) == []


async def test_confirming_writes_exactly_the_resolved_ids_and_reads_them_back(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer)
    r = await call_tool(
        "set_box_slot_filaments",
        assignments=[
            {"slot": 0, "filament": "PLA Matte", "color": "black"},
            {"slot": 1, "filament": "PETG-CF", "color": "#1353AB", "vendor": "QIDI"},
        ],
        confirm=True,
    )
    assert r["ok"] is True and r["all_applied"] is True
    v = printer.saved_variables
    assert (v["filament_slot0"], v["color_slot0"]) == (2, 2)
    assert (v["filament_slot1"], v["color_slot1"], v["vendor_slot1"]) == (42, 14, 1)
    assert [s["now"] for s in r["slots"]][0].startswith("slot 0 (unit 1, position 1): PLA Matte, black")
    # nothing outside the named slots/fields moved
    assert v["filament_slot2"] == 1 and v["color_slot2"] == 1 and v["vendor_slot0"] == 1


async def test_only_named_fields_change(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer, filament_slot0=2, color_slot0=2, vendor_slot0=0)
    r = await call_tool(
        "set_box_slot_filaments", assignments=[{"slot": 0, "color": "red"}], confirm=True
    )
    assert r["all_applied"] is True
    assert _writes(printer) == ["SAVE_VARIABLE VARIABLE=color_slot0 VALUE=18"]
    assert printer.saved_variables["filament_slot0"] == 2


async def test_setting_what_is_already_there_writes_nothing(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer, filament_slot0=2, color_slot0=2)
    r = await call_tool(
        "set_box_slot_filaments",
        assignments=[{"slot": 0, "filament": "PLA Matte", "color": "black"}],
        confirm=True,
    )
    assert r["ok"] is True and r["all_applied"] is True and r["commands_sent"] == []
    assert _writes(printer) == []


async def test_one_bad_assignment_means_nothing_is_written(call_tool, moonraker):
    """All-or-nothing: a valid slot 0 must not be applied if slot 1's colour is nonsense."""
    _, printer = moonraker
    _boxed(printer)
    before = dict(printer.saved_variables)
    r = await call_tool(
        "set_box_slot_filaments",
        assignments=[
            {"slot": 0, "filament": "PLA Matte", "color": "black"},
            {"slot": 1, "filament": "PETG", "color": "chartreuse"},
        ],
        confirm=True,
    )
    assert r["ok"] is False and r["error"] == "invalid_request"
    assert printer.saved_variables == before and _writes(printer) == []


@pytest.mark.parametrize(
    "assignments",
    [
        [],
        [{"slot": 0}],  # names nothing
        [{"slot": 0, "filament": "PLA", "nonsense": 1}],  # unknown field
        [{"slot": 4, "filament": "PLA"}],  # a one-Box printer has slots 0-3
        [{"slot": 16, "filament": "PLA"}],  # the extruder pseudo-slot
        [{"slot": -1, "filament": "PLA"}],
        [{"slot": "0", "filament": "PLA"}],
        [{"slot": True, "filament": "PLA"}],
        [{"slot": 0, "filament": "PLA"}, {"slot": 0, "color": "red"}],  # twice
        [{"slot": 0, "filament": "ABS"}],  # ambiguous
    ],
)
async def test_malformed_assignments_are_refused_before_any_write(call_tool, moonraker, assignments):
    _, printer = moonraker
    _boxed(printer)
    r = await call_tool("set_box_slot_filaments", assignments=assignments, confirm=True)
    assert r["ok"] is False and r["error"] == "invalid_request"
    assert _writes(printer) == []


async def test_it_needs_a_detected_box(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool(
        "set_box_slot_filaments", assignments=[{"slot": 0, "filament": "PLA"}], confirm=True
    )
    assert r["ok"] is False and r["error"] == "not_supported"
    assert _writes(printer) == []


async def test_it_will_not_write_ids_it_could_not_check_against_the_table(call_tool, moonraker):
    _, printer = moonraker
    printer.attach_box(1)
    printer.filament_table = None
    r = await call_tool(
        "set_box_slot_filaments", assignments=[{"slot": 0, "filament": 39}], confirm=True
    )
    assert r["ok"] is False and r["error"] == "not_supported"
    assert _writes(printer) == []


async def test_it_is_refused_while_printing(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer)
    printer.print_state = "printing"
    printer.print_filename = "benchy.gcode"
    r = await call_tool(
        "set_box_slot_filaments", assignments=[{"slot": 0, "filament": "PLA Matte"}], confirm=True
    )
    assert r["ok"] is False and r["error"] == "interlock_blocked"
    assert _writes(printer) == []


async def test_guesses_are_called_out_in_the_preview(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer)
    r = await call_tool(
        "set_box_slot_filaments",
        assignments=[
            {"slot": 0, "filament": "PETG", "color": "#FF0000"},  # generic + snapped
            {"slot": 2, "filament": "PLA Matte"},  # the Box says slot 2 is empty
        ],
    )
    text = " | ".join(r["warnings"])
    assert "Basic" in text  # PETG -> PETG Basic was an assumption
    assert "nearest" in text  # red snapped to a swatch
    assert "slot 2" in text and "no filament" in text
    assert "auto_read_rfid" in text  # the RFID tag can overwrite this


async def test_a_write_the_firmware_swallows_is_reported_not_assumed(
    call_tool, moonraker, server_rt, monkeypatch
):
    _, printer = moonraker
    _boxed(printer)
    _, rt = server_rt
    real = rt.client.gcode

    async def swallow(script, *a, **kw):
        if "SAVE_VARIABLE" in script:
            return None
        return await real(script, *a, **kw)

    monkeypatch.setattr(rt.client, "gcode", swallow)
    r = await call_tool(
        "set_box_slot_filaments", assignments=[{"slot": 0, "filament": "PETG Basic"}], confirm=True
    )
    assert r["ok"] is True and r["all_applied"] is False
    assert r["slots"][0]["applied"] is False
    assert "Not everything took" in r["note"]


async def test_a_mid_batch_failure_says_what_was_and_was_not_sent(
    call_tool, moonraker, server_rt, monkeypatch
):
    from qidi_mcp.errors import QidiError

    _, printer = moonraker
    _boxed(printer)
    _, rt = server_rt
    real = rt.client.gcode
    calls = {"n": 0}

    async def flaky(script, *a, **kw):
        if "SAVE_VARIABLE" in script:
            calls["n"] += 1
            if calls["n"] == 2:
                raise QidiError("klippy said no")
        return await real(script, *a, **kw)

    monkeypatch.setattr(rt.client, "gcode", flaky)
    r = await call_tool(
        "set_box_slot_filaments",
        assignments=[{"slot": 0, "filament": "PETG Basic", "color": "red"}],
        confirm=True,
    )
    assert r["ok"] is True and r["all_applied"] is False
    assert len(r["commands_sent"]) == 1 and "klippy said no" in r["error"]


# --------------------------------------------------------------------------
# Queueing
# --------------------------------------------------------------------------


async def test_queueing_by_loose_name_resolves_and_previews(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("queue_jobs", filenames=["benchy", "bracket abs"])
    assert r["ok"] is False  # preview
    assert [(w["requested"], w["file"]) for w in r["will_queue"]] == [
        ("benchy", "benchy.gcode"),
        ("bracket abs", "projects/bracket_abs.gcode"),
    ]
    assert r["will_queue"][0]["estimated_time_h"] == 1.62
    assert r["total_estimated_time_h"] == round((5820 + 24300) / 3600, 2)
    assert printer.queued_jobs == []


async def test_confirming_queues_the_resolved_paths(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("queue_jobs", filenames=["benchy", "bracket abs"], confirm=True)
    assert r["ok"] is True
    assert [j["filename"] for j in printer.queued_jobs] == [
        "benchy.gcode",
        "projects/bracket_abs.gcode",
    ]
    assert [q["file"] for q in r["queued"]] == ["benchy.gcode", "projects/bracket_abs.gcode"]


async def test_an_unknown_name_queues_nothing_even_when_others_are_fine(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool("queue_jobs", filenames=["benchy", "no such thing"], confirm=True)
    assert r["ok"] is False and r["error"] == "invalid_request"
    assert printer.queued_jobs == []


async def test_an_ambiguous_name_lists_the_candidates(call_tool, moonraker):
    _, printer = moonraker
    printer.files["projects/bracket_pla.gcode"] = dict(printer.files["projects/bracket_abs.gcode"])
    r = await call_tool("queue_jobs", filenames=["bracket"], confirm=True)
    assert r["ok"] is False and len(r["candidates"]) == 2
    assert printer.queued_jobs == []


async def test_a_live_queue_still_needs_the_lease_under_loose_names(
    call_tool, moonraker, claim_printer
):
    """Resolving names must not weaken the 'a ready queue is a print start' rule."""
    _, printer = moonraker
    printer.queue_state = "ready"
    refused = await call_tool("queue_jobs", filenames=["benchy"], confirm=True)
    assert refused["ok"] is False and "lease" in str(refused).lower()
    assert printer.queued_jobs == []

    await claim_printer()
    r = await call_tool("queue_jobs", filenames=["benchy"])  # unconfirmed: a preview
    assert r["error"] == "confirmation_required"
    assert r["will_start_automatically"] is True and "print start" in r["warning"]


async def test_the_queue_preview_says_whether_it_moves_on_by_itself(call_tool, moonraker):
    r = await call_tool("queue_jobs", filenames=["benchy"])
    assert r["advance"]["automatic_transition"] is False
    assert r["advance"]["source"] == "Moonraker's default (not set)"
    assert "clear the plate" in r["advance"]["meaning"]


async def test_an_auto_advancing_queue_is_flagged_loudly(call_tool, moonraker, server_rt, monkeypatch):
    _, rt = server_rt

    async def cfg():
        return {"config": {"job_queue": {"automatic_transition": "True"}}}

    monkeypatch.setattr(rt.client, "server_config", cfg)
    r = await call_tool("queue_jobs", filenames=["benchy"])
    assert r["advance"]["automatic_transition"] is True
    assert r["advance"]["source"] == "the printer's Moonraker config"
    assert r["advance"]["meaning"].startswith("WARNING")


async def test_an_unreadable_moonraker_config_does_not_break_queueing(
    call_tool, moonraker, server_rt, monkeypatch
):
    from qidi_mcp.errors import ConnectionFailed

    _, rt = server_rt

    async def boom():
        raise ConnectionFailed("nope")

    monkeypatch.setattr(rt.client, "server_config", boom)
    r = await call_tool("queue_jobs", filenames=["benchy"])
    assert r["advance"] is None and r["will_queue"][0]["file"] == "benchy.gcode"


async def test_queue_preview_includes_the_box_fit(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer, filament_slot0=7, color_slot0=1, filament_slot1=7, color_slot1=1)
    r = await call_tool("queue_jobs", filenames=["benchy", "bracket abs"])
    fits = {w["file"]: w["box_fit"] for w in r["will_queue"]}
    assert fits["benchy.gcode"]["verdict"] in {"ok", "unknown"}  # PLA file, PLA loaded
    assert fits["projects/bracket_abs.gcode"]["verdict"] == "mismatch"  # ABS file, PLA loaded
    assert "ABS" in fits["projects/bracket_abs.gcode"]["summary"]


async def test_no_box_means_no_fit_claims(call_tool, moonraker):
    r = await call_tool("queue_jobs", filenames=["benchy"])
    assert r["will_queue"][0]["box_fit"] is None


async def test_the_queue_reads_back_as_plain_lines(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer, filament_slot0=7, color_slot0=1)
    await call_tool("queue_jobs", filenames=["benchy", "bracket abs"], confirm=True)
    r = await call_tool("get_job_queue")
    assert r["ok"] is True and len(r["queued_jobs"]) == 2
    assert r["summary"][0].startswith("1. benchy.gcode -- 1.62 h, PLA")
    assert r["summary"][1].startswith("2. projects/bracket_abs.gcode") and "mismatch" in r["summary"][1]
    assert r["total_estimated_time_h"] == round((5820 + 24300) / 3600, 2)
    assert r["advance"]["automatic_transition"] is False


async def test_a_queued_file_without_metadata_does_not_break_the_listing(call_tool, moonraker):
    _, printer = moonraker
    await call_tool("queue_jobs", filenames=["benchy"], confirm=True)
    printer.unparsed.add("benchy.gcode")
    r = await call_tool("get_job_queue")
    assert r["ok"] is True and "no slicer metadata" in r["summary"][0]


# --------------------------------------------------------------------------
# check_print_fit / start_print
# --------------------------------------------------------------------------


async def test_check_print_fit_compares_files_with_the_box(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer, filament_slot0=7, color_slot0=1)
    r = await call_tool("check_print_fit", filenames=["benchy", "bracket abs"])
    assert r["ok"] is True and r["box_present"] is True
    by_file = {f["file"]: f["fit"]["verdict"] for f in r["files"]}
    assert by_file["projects/bracket_abs.gcode"] == "mismatch"
    assert r["overall"] == "mismatch"
    assert "not verified" in r["files"][0]["fit"]["note"]


async def test_check_print_fit_without_a_box_says_so(call_tool, moonraker):
    r = await call_tool("check_print_fit", filenames=["benchy"])
    assert r["ok"] is True and r["box_present"] is False and r["overall"] is None
    assert "No Box" in r["note"]


async def test_check_print_fit_reports_missing_metadata_as_unknown(call_tool, moonraker):
    _, printer = moonraker
    _boxed(printer)
    printer.unparsed.add("benchy.gcode")
    r = await call_tool("check_print_fit", filenames=["benchy"])
    assert r["files"][0]["fit"]["verdict"] == "unknown"


async def test_check_print_fit_refuses_unknown_names(call_tool, moonraker):
    r = await call_tool("check_print_fit", filenames=["nothing like it"])
    assert r["ok"] is False and r["error"] == "invalid_request"


async def test_start_print_ticket_carries_the_fit_verdict(call_tool, moonraker, claim_printer):
    _, printer = moonraker
    _boxed(printer, filament_slot0=7, color_slot0=1)
    await claim_printer()
    r = await call_tool("start_print", filename="projects/bracket_abs.gcode", confirm=True)
    assert r["ok"] is False and r["confirm_token"]
    assert r["box_fit"]["verdict"] == "mismatch"


async def test_start_print_without_a_box_has_no_fit_section(call_tool, moonraker, claim_printer):
    await claim_printer()
    r = await call_tool("start_print", filename="benchy.gcode", confirm=True)
    assert "box_fit" not in r


async def test_junk_print_times_in_metadata_do_not_crash_the_listing(call_tool, moonraker):
    _, printer = moonraker
    printer.files["benchy.gcode"]["estimated_time"] = "soon"
    await call_tool("queue_jobs", filenames=["benchy"], confirm=True)
    r = await call_tool("get_job_queue")
    assert r["ok"] is True and r["total_estimated_time_h"] is None
