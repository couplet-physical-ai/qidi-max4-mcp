#!/usr/bin/env python
"""Regenerate the dashboard screenshots used in the docs.

Hand-made screenshots rot: the UI changes, the image does not, and the docs start
lying. This drives the real dashboard against the in-process mock, so the images
are reproducible and a stale one is a command away from being fixed.

It also asserts the layout does not scroll sideways at three viewports. That is a
real check, not decoration -- an earlier "clipped on mobile" bug turned out to be
a screenshot artifact, because Chrome's ``--window-size`` does not set the CSS
viewport. Measuring ``scrollWidth`` against ``clientWidth`` under
``Emulation.setDeviceMetricsOverride`` is the only version of this that means
anything.

Usage:
    .venv/bin/python scripts/screenshots.py            # write docs/assets/
    .venv/bin/python scripts/screenshots.py --check    # verify layout only

Requires Google Chrome or Chromium; set ``QIDI_CHROME`` if it lives somewhere
unusual. Never touches a real printer: the mock binds loopback and the dashboard is
read-only by construction.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "docs" / "assets"

#: Where Chrome lives, most-specific first. The docs tell people to run this, so it
#: must not assume a macOS layout -- a hardcoded /Applications path fails on Linux
#: with a bare FileNotFoundError that says nothing useful.
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)


def find_chrome() -> str:
    """Locate a Chrome/Chromium binary, or explain what to install."""
    override = os.environ.get("QIDI_CHROME")
    if override:
        if not Path(override).exists():
            raise SystemExit(f"QIDI_CHROME={override} does not exist")
        return override
    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit(
        "no Chrome or Chromium found. Install one, or point QIDI_CHROME at it.\n"
        "  macOS:  brew install --cask google-chrome\n"
        "  Debian: apt-get install -y chromium"
    )
CDP_PORT = 9412
DASH_PORT = 8793

#: viewport width -> (asset stem, theme, mobile emulation)
SHOTS = (
    (1280, "dashboard-desktop-light", "light", False),
    (1280, "dashboard-desktop-dark", "dark", False),
    (414, "dashboard-phone", "light", True),
)


def _mock_printer() -> str:
    """Mock Moonraker on its own thread, in a realistic mid-print state."""
    import logging

    logging.disable(logging.CRITICAL)
    from aiohttp import web

    from tests.mock_moonraker import MockPrinter, build_app

    printer = MockPrinter()
    # Mirrors the first job actually printed through this server.
    printer.print_filename = "drone-sensor-mount-base-PLA.gcode"
    printer.print_state = "printing"
    printer.progress = 0.51
    printer.extruder_temp, printer.extruder_target = 209.8, 210.0
    printer.bed_temp, printer.bed_target = 45.0, 45.0
    printer.chamber_temp, printer.chamber_target = 31.4, 0.0
    printer.homed_axes = "xyz"
    printer.ai_detect["noodle"] = {"enable": 1, "sensitivity": "HIGH"}
    # A print is running, so somebody claimed the machine. A screenshot of an empty
    # "who is driving" panel would teach a reader nothing about what it is for.
    printer.database["qidi_mcp"] = {
        "lease": {
            "holder_id": "9f2c1ab4d0e37c55",
            "owner": "intern@bench-2",
            "reason": "drone sensor mount, ~27 min",
            "acquired_at": time.time() - 840,
            "expires_at": time.time() + 1_500,
            "renewed_at": time.time() - 840,
            "agent": "intern@bench-2",
        }
    }

    ready, box = threading.Event(), {}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start() -> None:
            runner = web.AppRunner(build_app(printer), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            box["port"] = runner.addresses[0][1]
            ready.set()

        loop.run_until_complete(start())
        loop.run_forever()

    threading.Thread(target=run, daemon=True, name="mock-moonraker").start()
    if not ready.wait(timeout=20):
        raise SystemExit("mock Moonraker did not start")
    return f"http://127.0.0.1:{box['port']}"


async def _drive(check_only: bool) -> int:
    import websockets

    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json"))
    page = next(t for t in tabs if t["type"] == "page")
    failures = 0
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        counter = [0]

        async def cmd(method: str, params: dict | None = None) -> dict:
            counter[0] += 1
            mid = counter[0]
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {})

        for width, stem, theme, mobile in SHOTS:
            await cmd(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": 1400, "deviceScaleFactor": 2, "mobile": mobile},
            )
            await cmd("Page.navigate", {"url": f"http://127.0.0.1:{DASH_PORT}/?theme={theme}"})
            await asyncio.sleep(3.0)
            probe = await cmd(
                "Runtime.evaluate",
                {
                    "returnByValue": True,
                    "expression": """(() => {
                      const de = document.documentElement, vw = de.clientWidth;
                      const bad = [];
                      document.querySelectorAll("*").forEach(el => {
                        if (el.getBoundingClientRect().right > vw + 0.5) bad.push(el.tagName);
                      });
                      const body = document.body.innerText;
                      return JSON.stringify({
                        vw, overflow: de.scrollWidth - vw, bad: bad.length,
                        caps: (body.match(/cap \\d+/g) || []).length,
                        lease: body.includes("intern@bench-2"),
                        broken: body.includes("[object") || body.includes("undefined") ||
                                body.includes("NaN"),
                      });
                    })()""",
                },
            )
            facts = json.loads(probe["result"]["value"])
            ok = facts["overflow"] <= 0 and facts["bad"] == 0 and facts["caps"] == 3
            # The lease panel is the point of the page for a shared machine: if it
            # renders blank, the screenshot silently documents the wrong thing.
            ok = ok and not facts["broken"] and facts["lease"]
            print(
                f"  {stem:26} vw={facts['vw']:<5} overflow={facts['overflow']:<4} "
                f"ceilings={facts['caps']}/3 lease={'yes' if facts['lease'] else 'MISSING'} "
                f"{'OK' if ok else 'FAIL'}"
            )
            if not ok:
                failures += 1
            if not check_only:
                shot = await cmd("Page.captureScreenshot", {"captureBeyondViewport": True})
                target = ASSETS / f"{stem}.png"
                target.write_bytes(base64.b64decode(shot["data"]))
                print(f"    wrote {target.relative_to(ROOT)}  ({target.stat().st_size // 1024} KB)")
    return failures


def main() -> None:
    check_only = "--check" in sys.argv
    ASSETS.mkdir(parents=True, exist_ok=True)
    import os

    base = _mock_printer()
    os.environ.update(
        {
            "QIDI_PRINTER_URL": base,
            "QIDI_TEST_MODE": "1",
            "QIDI_DASHBOARD_NO_BROWSER": "1",
        }
    )
    from qidi_mcp.dashboard import _Bridge, _handler

    bridge = _Bridge()
    httpd = ThreadingHTTPServer(("127.0.0.1", DASH_PORT), _handler(bridge))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"dashboard on 127.0.0.1:{DASH_PORT} against mock {base}")

    chrome_binary = find_chrome()
    print(f"using {chrome_binary}")
    chrome = subprocess.Popen(
        [
            chrome_binary, "--headless", "--disable-gpu", "--no-sandbox",
            f"--remote-debugging-port={CDP_PORT}", "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(4)
    try:
        failures = asyncio.run(_drive(check_only))
    finally:
        chrome.terminate()
        httpd.shutdown()
        bridge.close()
    if failures:
        raise SystemExit(f"{failures} viewport(s) failed layout checks")
    print("all viewports clean")


if __name__ == "__main__":
    main()
