"""A read-only live dashboard for the printer, served locally.

Why this exists: everything else in this package is for an agent to *call*. This
is for a human to *watch*. It answers the questions you actually have while a
print runs -- is it moving, how far in, how hot, is the failure net armed -- in
one glance, without opening QIDI's app or trusting the screen on the machine.

Two properties worth knowing:

*   **It cannot move your printer.** The dashboard builds the MCP server with
    ``read_only`` forced on, whatever the environment says, so every tier that
    mutates is refused before any network call. It is physically the same code
    path an agent uses, so what you see is exactly what an agent would see -- but
    the write half is bolted shut.
*   **It shows the envelope, not just the readings.** Each heater meter draws the
    current temperature, its target, *and* the ceiling this server enforces. The
    gap between the firmware's limit and ours is the entire point of the project,
    so it should be visible rather than buried in a config file.

Run it with ``qidi-dashboard`` (or ``python -m qidi_mcp.dashboard``). It opens a
browser at 127.0.0.1 and polls a few times a second. Bind address is loopback by
default on purpose: this exposes printer state and should not be served to a LAN
without thinking about it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import load_settings
from .dashboard_page import PAGE
from .server import build_server

log = logging.getLogger(__name__)

#: Read tools the dashboard aggregates. Capabilities and policy are effectively
#: static, so they are fetched once and cached; the rest are polled.
#:
#: ``get_printer_lease`` is polled rather than cached, and deliberately so: "who is
#: driving this machine" is the fastest-changing thing on the page, and a human
#: walking up to the printer is asking exactly that.
_LIVE_TOOLS = (
    "get_printer_status",
    "get_job_status",
    "get_ai_detection_status",
    # Two light queries on a printer with no Box; the page hides its card then.
    "get_box_status",
    "get_printer_lease",
)
_STATIC_TOOLS = ("get_capabilities", "get_safety_policy")


def _printer_answered(live: dict[str, Any]) -> bool:
    """Whether this poll reached the printer, i.e. whether static reads may be cached.

    ``get_capabilities`` returns ``ok: true`` even when Moonraker is unreachable --
    it degrades to "unknown" everywhere plus warnings. Caching that answer pinned
    the page to "unknown" for the life of the process: a printer that was off when
    the first visitor arrived stayed blank after it came back, until a restart.
    """
    status = live.get("get_printer_status")
    return isinstance(status, dict) and status.get("ok") is True


class _Bridge:
    """Runs the async MCP server on its own loop so a threaded HTTP server can use it."""

    def __init__(self) -> None:
        # Force read-only regardless of the caller's environment. The dashboard is
        # a window, not a control panel; nothing here should be able to commit the
        # machine even if someone runs it with QIDI_READ_ONLY=0 already exported.
        env = dict(os.environ)
        env["QIDI_READ_ONLY"] = "1"
        self.settings = load_settings(env)
        self.server, self.rt = build_server(self.settings)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="qidi-dash")
        self._thread.start()
        self._static: dict[str, Any] = {}

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, name: str) -> Any:
        return self._call_with_args(name, {})

    def _call_with_args(self, name: str, args: dict[str, Any]) -> Any:
        async def go() -> Any:
            result = await self.server.call_tool(name, args)
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                return structured
            if isinstance(result, tuple):
                return result[1]
            try:
                return json.loads(result.content[0].text)
            except (AttributeError, IndexError, ValueError):
                return {"ok": False, "error": "unparsable_result"}

        future = asyncio.run_coroutine_threadsafe(go(), self._loop)
        return future.result(timeout=30)

    def snapshot(self) -> dict[str, Any]:
        """One aggregated read. Individual failures are reported, not fatal."""
        out: dict[str, Any] = {}
        live: dict[str, Any] = {}
        for name in _LIVE_TOOLS:
            try:
                live[name] = self._call(name)
            except Exception as exc:  # noqa: BLE001
                live[name] = {"ok": False, "error": str(exc)}
        static = self._static
        if not static:
            static = {}
            for name in _STATIC_TOOLS:
                try:
                    static[name] = self._call(name)
                except Exception as exc:  # noqa: BLE001 - surface, never crash the page
                    log.warning("dashboard: %s failed: %s", name, exc)
                    static[name] = {"ok": False, "error": str(exc)}
            if _printer_answered(live):
                self._static = static
        out.update(static)
        out.update(live)
        out["printer_url"] = str(self.settings.base_url)
        out["read_only"] = True
        return out

    def close(self) -> None:
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.rt.aclose(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)


def _handler(bridge: _Bridge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "qidi-dashboard"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = (self.path or "/").split("?", 1)[0]
            if path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                body = json.dumps(bridge.snapshot(), default=str).encode("utf-8")
                self._send(200, body, "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def log_message(self, *_args: Any) -> None:
            """Silence per-request logging; a polling dashboard would flood the console."""

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    host = os.environ.get("QIDI_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("QIDI_DASHBOARD_PORT", "8770"))
    bridge = _Bridge()
    httpd = ThreadingHTTPServer((host, port), _handler(bridge))
    url = f"http://{host}:{port}/"
    print(f"QIDI dashboard on {url}  (read-only; printer {bridge.settings.base_url})")
    print("Ctrl+C to stop.")
    if os.environ.get("QIDI_DASHBOARD_NO_BROWSER") != "1":
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        bridge.close()


if __name__ == "__main__":
    main()
