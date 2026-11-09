"""L2b: end-to-end — does a NaN argument actually put 'nan' on the wire?

Uses the project's own mock Moonraker (tests/mock_moonraker.py) and records
printer.commands, i.e. exactly the script bytes the server POSTed to
/printer/gcode/script.
"""
import asyncio
import os
import sys

os.environ["QIDI_TEST_MODE"] = "1"
os.environ["QIDI_PRINTER_URL"] = "http://127.0.0.1:7125"
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))

import pytest_asyncio  # noqa: F401  (ensure test deps present)
from aiohttp import web

from mock_moonraker import build_app, MockPrinter
from qidi_mcp.config import load_settings
from qidi_mcp.server import build_server

NAN = float("nan")
INF = float("inf")


async def main():
    printer = MockPrinter()
    app = build_app(printer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    settings = load_settings({
        "QIDI_PRINTER_URL": f"http://127.0.0.1:{port}",
        "QIDI_TEST_MODE": "1",
    })
    server, rt = build_server(settings)

    # Home + set a known position so jog destination checks are meaningful.
    printer.homed_axes = "xyz"
    printer.position = [100.0, 100.0, 50.0, 0.0]

    async def call(name, **kw):
        printer.commands.clear()
        result = await server.call_tool(name, kw)
        payload = getattr(result, "structuredContent", None)
        if payload is None and isinstance(result, tuple):
            payload = result[1]
        if payload is None:
            payload = result
        if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
            payload = payload["result"]
        return payload, list(printer.commands)

    print("=== extrude_filament(distance=nan) ===")
    # First call returns a ticket (DESTRUCTIVE-ish gate); confirm then redeem.
    res, cmds = await call("extrude_filament", distance=NAN, confirm=True)
    print("  result:", str(res)[:220])
    print("  WIRE  :", cmds)
    if cmds and any("nan" in c.lower() for c in cmds):
        print("  <== 'nan' REACHED THE WIRE")

    print()
    print("=== extrude_filament(distance=1e9) (control: should be refused) ===")
    res, cmds = await call("extrude_filament", distance=1e9, confirm=True)
    print("  result:", str(res)[:200])
    print("  WIRE  :", cmds)

    print()
    print("=== move_relative(dx=1, feedrate=nan) ===")
    res, cmds = await call("move_relative", dx=1.0, feedrate=NAN, confirm=True)
    print("  result:", str(res)[:220])
    print("  WIRE  :", cmds)
    if cmds and any("nan" in c.lower() for c in cmds):
        print("  <== 'nan' REACHED THE WIRE")

    print()
    print("=== move_relative(dx=nan) (position check should catch) ===")
    res, cmds = await call("move_relative", dx=NAN, confirm=True)
    print("  result:", str(res)[:220])
    print("  WIRE  :", cmds)

    print()
    print("=== move_absolute(x=100, feedrate=inf) ===")
    res, cmds = await call("move_absolute", x=100.0, feedrate=INF, confirm=True)
    print("  result:", str(res)[:200])
    print("  WIRE  :", cmds)

    print()
    print("=== set_temperature(extruder, nan) (control: must be refused) ===")
    res, cmds = await call("set_temperature", heater="extruder", target=NAN, confirm=True)
    print("  result:", str(res)[:200])
    print("  WIRE  :", cmds)

    await runner.cleanup()


asyncio.run(main())
