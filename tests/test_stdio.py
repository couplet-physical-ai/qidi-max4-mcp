"""End-to-end test over the real stdio transport, the way an MCP client launches it.

Everything else in the suite calls tools in-process. This file proves the thing
an MCP client actually does works: spawn `python -m qidi_mcp.server`, complete an MCP
handshake, list tools, and call one — including the case where the printer is
unreachable, which must produce a clean structured error rather than a hang or a
dead server.

The subprocess is pointed at a loopback port with nothing on it, so no printer
is contacted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest  # noqa: F401
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _params(**env_extra: str) -> StdioServerParameters:
    env = {
        **os.environ,
        "QIDI_PRINTER_URL": "http://127.0.0.1:9",  # discard port: nothing listens
        "QIDI_TEST_MODE": "1",
        "QIDI_TIMEOUT": "2",
        "QIDI_LOG_LEVEL": "ERROR",
        **env_extra,
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "qidi_mcp.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )


async def test_server_completes_handshake_and_lists_tools():
    async with stdio_client(_params()) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "qidi"
        assert init.instructions, "the server must ship usage instructions"
        assert "emergency_stop" in init.instructions

        listing = await session.list_tools()
        names = {t.name for t in listing.tools}
        assert len(names) >= 70, f"only {len(names)} tools exposed"
        for expected in [
            "get_printer_status", "emergency_stop", "start_print",
            "chamber_heater", "get_webcam_snapshot", "run_gcode",
        ]:
            assert expected in names


async def test_unreachable_printer_returns_structured_error_over_stdio():
    """The failure mode a user will actually hit first: printer off or wrong IP."""
    async with stdio_client(_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("get_printer_status", {})
        text = "".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        assert "connection" in text.lower() or "not ready" in text.lower()
        # It must offer a next step, not just fail.
        assert "hint" in text.lower()


async def test_tool_annotations_survive_the_wire():
    """An agent decides what is safe from annotations; they must arrive intact."""
    async with stdio_client(_params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}

        status = tools["get_printer_status"]
        assert status.annotations is not None
        assert status.annotations.read_only_hint is True

        estop = tools["emergency_stop"]
        assert estop.annotations.destructive_hint is True
        assert estop.annotations.idempotent_hint is True


async def test_read_only_mode_is_visible_over_stdio():
    async with (
        stdio_client(_params(QIDI_READ_ONLY="1")) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "set_temperature", {"heater": "extruder", "target": 200, "confirm": True}
        )
        text = "".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        assert "read_only" in text or "read-only" in text.lower()


async def test_server_refuses_to_start_pointed_at_a_real_ip_in_test_mode():
    """The guard that keeps the test suite off the live printer, verified end to end."""
    import anyio

    params = _params()
    params.env = {**(params.env or {}), "QIDI_PRINTER_URL": "http://192.0.2.10:7125"}
    with pytest.raises((Exception, anyio.BrokenResourceError)):
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
