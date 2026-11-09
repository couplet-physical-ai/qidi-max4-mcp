"""File existence and missing-file reporting.

Two bugs, one root cause -- "is this file there?" was answered by
``/server/files/metadata``, whose 404 the client maps to ``NotSupported``:

1. ``get_file_metadata`` on a misspelled name said *"This printer's Moonraker build
   does not provide /server/files/metadata"*, sending an agent to get_capabilities
   instead of to the file name. ``start_print`` repeated the same message.
2. The overwrite guard on ``move_file``/``copy_file`` asked the metadata route about
   the destination in move/copy form (``gcodes/x.gcode``), which the route never
   knows -- metadata paths are relative to the gcodes root. So clobbering an
   existing file was always judged a fresh write: LOW risk, no token. The same guard
   on ``upload_gcode`` missed files Moonraker had not parsed (yet).
"""

from __future__ import annotations


async def test_missing_file_is_not_found_not_unsupported(call_tool):
    r = await call_tool("get_file_metadata", filename="no-such-part.gcode")
    assert r["ok"] is False
    assert r["error"] == "file_not_found"
    assert "does not provide" not in r["message"]
    assert "list_gcode_files" in r["hint"]


async def test_start_print_on_missing_file_names_the_file(call_tool, claim_printer):
    await claim_printer()
    r = await call_tool("start_print", filename="no-such-part.gcode")
    assert r["ok"] is False
    assert "no-such-part.gcode" in r["message"]
    assert "does not provide" not in r["message"]


async def test_move_onto_an_existing_file_needs_the_token(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool(
        "move_file", source="gcodes/projects/bracket_abs.gcode", dest="gcodes/benchy.gcode",
        confirm=True,
    )
    assert r["ok"] is False
    assert r["status"] == "confirmation_required"
    assert r["replacing"] == "gcodes/benchy.gcode"
    assert r["risk"] == "destructive"


async def test_copy_onto_an_existing_file_needs_the_token(call_tool):
    r = await call_tool(
        "copy_file", source="gcodes/benchy.gcode", dest="gcodes/projects/bracket_abs.gcode",
        confirm=True,
    )
    assert r["status"] == "confirmation_required"
    assert r["replacing"] == "gcodes/projects/bracket_abs.gcode"


async def test_move_to_a_fresh_name_is_still_one_step(call_tool):
    r = await call_tool(
        "move_file", source="gcodes/benchy.gcode", dest="gcodes/archive/benchy.gcode",
        confirm=True,
    )
    assert r["ok"] is True
    assert r["replaced_existing"] is False


async def test_upload_over_an_unparsed_file_is_still_an_overwrite(call_tool, moonraker):
    """A file Moonraker has no metadata for is still a file, and overwriting it is
    still destructive."""
    _, printer = moonraker
    printer.files["projects/fresh.gcode.3mf"] = {
        "path": "projects/fresh.gcode.3mf", "size": 10, "modified": 1_756_600_000.0,
    }
    printer.unparsed.add("projects/fresh.gcode.3mf")

    r = await call_tool(
        "upload_gcode", filename="fresh.gcode.3mf", subdirectory="projects",
        content="G28\n", confirm=True,
    )
    assert r["ok"] is False
    assert r["error"] == "invalid_request"
    assert "already exists" in r["message"]

    r = await call_tool(
        "upload_gcode", filename="fresh.gcode.3mf", subdirectory="projects",
        content="G28\n", overwrite=True, confirm=True,
    )
    assert r["status"] == "confirmation_required"
    assert r["replacing"] == "projects/fresh.gcode.3mf"


async def test_upload_into_a_new_folder_is_a_fresh_write(call_tool, moonraker):
    _, printer = moonraker
    r = await call_tool(
        "upload_gcode", filename="part.gcode", subdirectory="brand/new", content="G28\n",
        confirm=True,
    )
    assert r["ok"] is True
    assert r["replaced_existing"] is False
    assert printer.uploads[-1]["filename"] == "brand/new/part.gcode"
