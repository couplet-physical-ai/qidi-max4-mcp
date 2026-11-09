#!/usr/bin/env python
"""Run the test suite's in-process mock Moonraker as a standalone server.

Use this to try the whole stack -- the MCP server, the tools, the dashboard --
with **no printer, no Klipper, no Moonraker install**. It is the exact same
fake server (``tests/mock_moonraker.py``) the pytest suite runs against, just
started on a real loopback port instead of an ephemeral one inside a fixture.

    uv run python scripts/run_mock_printer.py
    uv run python scripts/run_mock_printer.py --port 7125

Then point the server at it:

    QIDI_PRINTER_URL=http://127.0.0.1:7125 QIDI_TEST_MODE=1 \\
        uv run python -m qidi_mcp.server

(``QIDI_TEST_MODE=1`` is only needed because it is also what makes the
config layer accept a loopback address with no further ceremony; it has no
other effect here.) Or run the dashboard against it:

    QIDI_PRINTER_URL=http://127.0.0.1:7125 QIDI_TEST_MODE=1 \\
        uv run python -m qidi_mcp.dashboard

Nothing here ever talks to a real printer: ``MockPrinter`` is pure Python
state and ``build_app`` is an ordinary aiohttp application bound to loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from tests.mock_moonraker import MockPrinter, build_app  # noqa: E402


async def _run(host: str, port: int, *, printing: bool) -> None:
    printer = MockPrinter()
    if printing:
        # A representative in-progress job, so get_job_status / the dashboard
        # have something to show immediately.
        printer.start_manual_print("benchy.gcode")
        printer.progress = 0.42
        printer.extruder_target = 210.0
        printer.extruder_temp = 209.6
        printer.bed_target = 60.0
        printer.bed_temp = 60.1

    app = build_app(printer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    bound_port = runner.addresses[0][1]
    print(f"Mock Moonraker running at http://{host}:{bound_port}  (Ctrl+C to stop)")
    print(f"  export QIDI_PRINTER_URL=http://{host}:{bound_port}")
    print("  export QIDI_TEST_MODE=1")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=7125, help="bind port (default 7125, Moonraker's own)")
    ap.add_argument(
        "--idle", action="store_true", help="start idle instead of mid-print (default: mid-print)"
    )
    args = ap.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.host, args.port, printing=not args.idle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
