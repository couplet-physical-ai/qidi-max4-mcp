"""Test fixtures — plus the hard guarantee that tests cannot touch a real printer.

The live machine is at 192.0.2.10 and must never be contacted by the test
suite. Two independent mechanisms enforce that:

1.  ``QIDI_TEST_MODE=1`` is set for the whole session, and ``config.py`` refuses
    any non-loopback ``QIDI_PRINTER_URL`` in that mode.
2.  A session-wide monkeypatch of ``socket.socket.connect`` / ``connect_ex``
    raises on any address that is not 127.0.0.1/::1. This catches anything that
    bypasses the config layer entirely — a hardcoded URL, a stray httpx call, a
    typo in a fixture.

The second guard is the important one: it fails the test rather than the printer.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from aiohttp import web

os.environ["QIDI_TEST_MODE"] = "1"
os.environ.setdefault("QIDI_PRINTER_URL", "http://127.0.0.1:7125")
# Never let a developer's real credentials or overrides leak into a test run.
for _leaky in ("QIDI_API_KEY", "QIDI_READ_ONLY", "QIDI_STRICT_CONFIRM", "QIDI_AUDIT_LOG"):
    os.environ.pop(_leaky, None)

LOOPBACK_ONLY_MESSAGE = (
    "BLOCKED: the test suite attempted a network connection to {addr}. Tests must "
    "only talk to the in-process mock on loopback — never a real printer."
)


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # unix sockets, netlink, etc. — not our concern
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(scope="session", autouse=True)
def block_external_network() -> Iterator[None]:
    """Fail loudly on any connection attempt that leaves loopback."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: object) -> None:  # type: ignore[override]
        if not _is_loopback(address):
            raise AssertionError(LOOPBACK_ONLY_MESSAGE.format(addr=address))
        return real_connect(self, address)  # type: ignore[arg-type]

    def guarded_connect_ex(self: socket.socket, address: object) -> int:  # type: ignore[override]
        if not _is_loopback(address):
            raise AssertionError(LOOPBACK_ONLY_MESSAGE.format(addr=address))
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]


@pytest.fixture
def printer():
    """Fresh mock printer state per test."""
    from tests.mock_moonraker import MockPrinter

    return MockPrinter()


@pytest_asyncio.fixture
async def moonraker(printer) -> AsyncIterator[tuple[str, object]]:
    """Run the mock Moonraker on an ephemeral loopback port; yield (base_url, printer)."""
    from tests.mock_moonraker import build_app

    app = build_app(printer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}", printer
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def rt(moonraker) -> AsyncIterator[object]:
    """A Runtime wired to the mock, with default (safe) settings."""
    from qidi_mcp.config import load_settings
    from qidi_mcp.runtime import Runtime

    base_url, _ = moonraker
    settings = load_settings({"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1"})
    runtime = Runtime(settings)
    try:
        yield runtime
    finally:
        await runtime.aclose()


@pytest_asyncio.fixture
async def server_rt(moonraker) -> AsyncIterator[tuple[object, object]]:
    """A fully built MCPServer plus its Runtime, wired to the mock."""
    from qidi_mcp.config import load_settings
    from qidi_mcp.server import build_server

    base_url, _ = moonraker
    settings = load_settings({"QIDI_PRINTER_URL": base_url, "QIDI_TEST_MODE": "1"})
    server, runtime = build_server(settings)
    try:
        yield server, runtime
    finally:
        await runtime.aclose()


@pytest.fixture
def allow_dangerous_gcode(server_rt):
    """Flip the operator-side QIDI_ALLOW_DANGEROUS_GCODE switch on for one test.

    The switch is read from the environment at startup; tests set it on the live
    settings instead so the rest of the configuration stays the fixture default.
    """
    from dataclasses import replace

    _, runtime = server_rt
    runtime.policy.settings = replace(runtime.policy.settings, allow_dangerous_gcode=True)
    runtime.settings = runtime.policy.settings
    return runtime


@pytest.fixture
def call_tool(server_rt):
    """Call a tool by name the way a client would, returning the parsed payload."""
    import json

    server, _ = server_rt

    async def _call(_tool_name: str, /, **kwargs):
        result = await server.call_tool(_tool_name, kwargs)

        # MCPServer may return a CallToolResult, a (content, structured) tuple,
        # or a bare value depending on the tool's return annotation.
        payload = None
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            payload = structured
        elif isinstance(result, tuple):
            payload = result[1]
        elif getattr(result, "content", None):
            block = result.content[0]
            text = getattr(block, "text", None)
            payload = text if text is not None else block
        else:
            payload = result

        if isinstance(payload, str):
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                payload = json.loads(payload)
        if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
            payload = payload["result"]
        return payload

    _call.raw = lambda _tool_name, /, **kw: server.call_tool(_tool_name, kw)  # type: ignore[attr-defined]
    return _call


@pytest.fixture
def claim_printer(server_rt):
    """Take the printer lease, as an agent must before starting a print.

    start_print and releasing the job queue require an explicit claim so other
    agents can see the machine is in use (QIDI_REQUIRE_LEASE_FOR_PRINT). Tests
    that exercise printing therefore have to do what a real caller does; this
    keeps that one line from being copied into every one of them.
    """

    async def _claim(reason: str = "test") -> None:
        _, runtime = server_rt
        await runtime.lease.acquire(reason, minutes=30)

    return _claim
