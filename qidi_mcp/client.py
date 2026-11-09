"""Async Moonraker client: HTTP API plus one-shot JSON-RPC over WebSocket.

Scope notes
-----------
*   The HTTP API is the primary surface — every Moonraker build exposes it and
    it needs no session state.
*   ``webcam_snapshot`` bypasses Moonraker and hits crowsnest's MJPEG port
    directly (``:8080/?action=snapshot`` on stock QIDI firmware).
*   QIDI ship a *fork* of Moonraker with components removed (no ``sensor``,
    ``spoolman``, ``simplyprint``, ``analysis``, ``td1``) and, on stock config,
    no ``[update_manager]``. Nothing here may assume an endpoint exists:
    ``probe()`` in ``capabilities.py`` discovers what is really present, and
    404/501 are reported as ``NotSupported`` rather than as failures.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .config import Settings
from .errors import (
    ConnectionFailed,
    FileNotFound,
    InvalidRequest,
    KlippyNotReady,
    MoonrakerHTTPError,
    NotSupported,
)

#: Status objects requested for a "full" snapshot. Absent objects are simply
#: omitted by Klipper, so over-asking is safe and saves round trips.
DEFAULT_STATUS_OBJECTS: dict[str, list[str] | None] = {
    "webhooks": None,
    "print_stats": None,
    "display_status": None,
    "idle_timeout": None,
    "toolhead": None,
    "gcode_move": None,
    "extruder": None,
    "heater_bed": None,
    "virtual_sdcard": None,
    "motion_report": None,
    "fan": None,
    "filament_switch_sensor filament_switch_sensor": None,
    "heater_generic chamber": None,
    "temperature_sensor Chamber_Thermal_Protection_Sensor": None,
    "fan_generic cooling_fan": None,
    "fan_generic auxiliary_cooling_fan": None,
    "fan_generic auxiliary_cooling_fan2": None,
    "fan_generic chamber_circulation_fan": None,
    "heater_fan hotend_fan": None,
    "controller_fan chamber_fan": None,
    "controller_fan board_fan": None,
    "output_pin caselight": None,
    "output_pin beeper": None,
    "output_pin Polar_cooler": None,
    "bed_mesh": None,
    "probe": None,
    "exclude_object": None,
    "save_variables": None,
    "system_stats": None,
    "mcu": None,
}


class MoonrakerClient:
    """Thin, well-typed wrapper over Moonraker's HTTP + JSON-RPC surface."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.settings.api_key:
            h["X-Api-Key"] = self.settings.api_key
        if self.settings.bearer_token:
            h["Authorization"] = f"Bearer {self.settings.bearer_token}"
        return h

    async def client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.settings.base_url,
                    headers=self._headers(),
                    timeout=httpx.Timeout(self.settings.http_timeout),
                    follow_redirects=True,
                    # A printer on the LAN with a self-signed cert is common;
                    # only relevant when someone fronts Moonraker with https.
                    verify=False,
                    # Never honour *_proxy from the environment. The printer is
                    # on the LAN, so a proxy can only break the connection --
                    # and on a machine with a corporate/LLM proxy exported (as
                    # is the case here) httpx would otherwise POST printer
                    # control requests to that external host. A heater command
                    # must never leave the local network.
                    trust_env=False,
                )
            return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # -- raw HTTP -----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> Any:
        client = await self.client()
        try:
            resp = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                content=content,
                files=files,
                data=data,
                timeout=timeout or self.settings.http_timeout,
            )
        except httpx.ConnectError as exc:
            raise ConnectionFailed(
                f"Cannot reach Moonraker at {self.settings.base_url}: {exc}",
                hint=(
                    "Check the printer is powered on and on the network, and that "
                    "QIDI_PRINTER_URL points at it (Moonraker listens on port 7125)."
                ),
            ) from exc
        except httpx.TimeoutException as exc:
            raise ConnectionFailed(
                f"Timed out talking to Moonraker at {self.settings.base_url} ({exc}).",
                hint="The printer may be busy; retry, or raise QIDI_HTTP_TIMEOUT.",
            ) from exc

        return self._handle(resp, expect_json=expect_json)

    def _handle(self, resp: httpx.Response, *, expect_json: bool) -> Any:
        if resp.status_code in (404, 501):
            detail = _error_text(resp)
            raise NotSupported(
                f"This printer's Moonraker build does not provide {resp.request.url.path} "
                f"({resp.status_code}{': ' + detail if detail else ''}).",
                hint=(
                    "QIDI ship a trimmed Moonraker fork; call get_capabilities to see "
                    "what this machine actually supports."
                ),
            )
        if resp.status_code == 401:
            raise MoonrakerHTTPError(
                401,
                "Moonraker rejected the request as unauthorized.",
                hint=(
                    "Set QIDI_API_KEY (Moonraker → Settings → API key) or add this host "
                    "to Moonraker's trusted_clients."
                ),
            )
        if resp.status_code == 403:
            raise MoonrakerHTTPError(
                403,
                "Moonraker forbade the request.",
                hint="This client's IP is likely outside [authorization] trusted_clients.",
            )
        if resp.status_code >= 400:
            detail = _error_text(resp)
            # Klipper down / in shutdown surfaces as 503 from most endpoints.
            if resp.status_code == 503:
                raise KlippyNotReady(
                    f"Klipper is not ready: {detail or 'service unavailable'}.",
                    hint="Check get_printer_status; a shutdown needs restart_firmware.",
                )
            raise MoonrakerHTTPError(resp.status_code, detail or resp.reason_phrase)

        if not expect_json:
            return resp.content
        if not resp.content:
            return None
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            return resp.text
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    # -- convenience --------------------------------------------------------

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    # -- printer basics -----------------------------------------------------

    async def server_info(self) -> dict[str, Any]:
        return await self.get("/server/info")

    async def printer_info(self) -> dict[str, Any]:
        return await self.get("/printer/info")

    async def query(self, objects: dict[str, list[str] | None] | None = None) -> dict[str, Any]:
        """printer.objects.query — the workhorse status read."""
        objs = objects if objects is not None else DEFAULT_STATUS_OBJECTS
        # Moonraker's HTTP form: ?object=field1,field2&object2
        parts: list[str] = []
        for name, fields in objs.items():
            key = quote(name, safe=" ")
            parts.append(f"{key}={','.join(fields)}" if fields else key)
        result = await self.get(f"/printer/objects/query?{'&'.join(parts)}")
        if isinstance(result, dict):
            return result.get("status", result)
        return {}

    async def object_list(self) -> list[str]:
        result = await self.get("/printer/objects/list")
        if isinstance(result, dict):
            return list(result.get("objects", []))
        return []

    async def gcode(self, script: str, *, timeout: float | None = None) -> Any:
        """printer.gcode.script — run one or more G-code lines."""
        if not script.strip():
            raise InvalidRequest("Empty G-code script.")
        return await self.post(
            "/printer/gcode/script",
            params={"script": script},
            timeout=timeout or self.settings.long_timeout,
        )

    async def gcode_help(self) -> dict[str, str]:
        result = await self.get("/printer/gcode/help")
        return result if isinstance(result, dict) else {}

    async def emergency_stop(self) -> Any:
        return await self.post("/printer/emergency_stop")

    async def restart_host(self) -> Any:
        return await self.post("/printer/restart")

    async def restart_firmware(self) -> Any:
        return await self.post("/printer/firmware_restart")

    # -- QIDI fork extensions ----------------------------------------------
    #
    # QIDI's Moonraker fork adds these; they do not exist upstream, so each is
    # probed rather than assumed (a missing route yields NotSupported via the
    # 404/501 mapping). Verified against QIDI_MAX4's own
    # moonraker/components/klippy_apis.py.

    async def list_endpoints(self) -> Any:
        """QIDI-only: ask Klipper which endpoints it registered."""
        return await self.get("/printer/list_endpoints")

    async def route_exists(self, path: str) -> bool:
        """Is an HTTP route registered on Moonraker itself?

        ``/printer/list_endpoints`` enumerates what *Klipper* registered, which is
        a different namespace from the routes QIDI adds to *Moonraker*
        (``breakheater``, ``breakmacro``, ``power_off_print``). Those never appear
        in Klipper's list, so inferring them from it yields a false negative.

        Probe with a GET instead. These routes are POST-only, so Tornado answers:

        * ``405 Method Not Allowed`` -- the route exists (what we are testing for)
        * ``404 Not Found``          -- no such route

        A GET is side-effect free: 405 is refused before any handler runs, so this
        cannot abort a heater or a macro. Verified against a real Max4, where a
        known-bogus path returns 404 while ``breakheater`` returns 405.
        """
        client = await self.client()
        try:
            resp = await client.get(path)
        except httpx.HTTPError:
            return False
        # 200 covers a route that happens to allow GET (e.g. emergency_stop).
        return resp.status_code in (200, 405)

    async def break_heater(self) -> Any:
        """QIDI-only: abort an in-progress heater *wait* (M109/M190/M191).

        Calls Klipper's ``breakheater`` method. Unlike an emergency stop this
        leaves the machine running — it only stops the blocking wait.
        """
        return await self.post("/printer/breakheater")

    async def break_macro(self) -> Any:
        """QIDI-only: abort a running macro.

        Calls Klipper's ``breakmacro`` method. QIDI's own cancel path uses this
        together with breakheater, so a stuck macro can be broken without an
        emergency stop and its firmware restart.
        """
        return await self.post("/printer/breakmacro")

    async def power_loss_resume(self) -> Any:
        """QIDI-only: resume the print interrupted by a power loss (断电续打).

        Moonraker generates recovery G-code from its saved state and runs it.
        This *starts printing again* — it is not a power-off.
        """
        return await self.post("/printer/print/power_off_print")

    # -- print job ----------------------------------------------------------

    async def print_start(self, filename: str) -> Any:
        return await self.post("/printer/print/start", params={"filename": filename})

    async def print_pause(self) -> Any:
        return await self.post("/printer/print/pause")

    async def print_resume(self) -> Any:
        return await self.post("/printer/print/resume")

    async def print_cancel(self) -> Any:
        return await self.post("/printer/print/cancel")

    # -- files --------------------------------------------------------------

    async def file_list(self, root: str = "gcodes") -> list[dict[str, Any]]:
        result = await self.get("/server/files/list", params={"root": root})
        return result if isinstance(result, list) else []

    async def file_roots(self) -> list[dict[str, Any]]:
        result = await self.get("/server/files/roots")
        return result if isinstance(result, list) else []

    async def file_metadata(self, filename: str) -> dict[str, Any]:
        try:
            return await self.get("/server/files/metadata", params={"filename": filename})
        except NotSupported as exc:
            # The metadata route is core Moonraker and present on the Max4, so its
            # 404 means "no metadata for this file": the file is absent, or Moonraker
            # has not (or could not) parse it. It never means "route unsupported".
            raise FileNotFound(
                f"Moonraker has no metadata for {filename!r}: it is not on the printer "
                "(paths are relative to the gcodes root), or Moonraker has not finished "
                "parsing it.",
                hint=(
                    "Check the name with list_gcode_files or list_directory. Right after "
                    "an upload, retry in a moment."
                ),
            ) from exc

    async def directory_info(self, path: str = "gcodes", extended: bool = False) -> dict[str, Any]:
        return await self.get(
            "/server/files/directory",
            params={"path": path, "extended": "true" if extended else "false"},
        )

    async def file_download(self, root: str, path: str, *, max_bytes: int) -> bytes:
        client = await self.client()
        url = f"/server/files/{root}/{quote(path)}"
        chunks: list[bytes] = []
        total = 0
        try:
            async with client.stream("GET", url, timeout=self.settings.long_timeout) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._handle(resp, expect_json=False)
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        break
        except httpx.HTTPError as exc:
            raise ConnectionFailed(f"Download of {path} failed: {exc}") from exc
        return b"".join(chunks)[:max_bytes]

    async def file_upload(
        self,
        *,
        filename: str,
        content: bytes,
        root: str = "gcodes",
        path: str = "",
        start_print: bool = False,
    ) -> Any:
        files = {"file": (filename, content, "application/octet-stream")}
        data: dict[str, str] = {"root": root}
        if path:
            data["path"] = path
        if start_print:
            data["print"] = "true"
        return await self.post(
            "/server/files/upload",
            files=files,
            data=data,
            timeout=self.settings.long_timeout,
        )

    async def file_delete(self, root: str, path: str) -> Any:
        return await self.delete(f"/server/files/{root}/{quote(path)}")

    async def file_move(self, source: str, dest: str) -> Any:
        return await self.post("/server/files/move", params={"source": source, "dest": dest})

    async def file_copy(self, source: str, dest: str) -> Any:
        return await self.post("/server/files/copy", params={"source": source, "dest": dest})

    async def directory_create(self, path: str) -> Any:
        return await self.post("/server/files/directory", params={"path": path})

    async def directory_delete(self, path: str, force: bool = False) -> Any:
        return await self.delete(
            "/server/files/directory",
            params={"path": path, "force": "true" if force else "false"},
        )

    # -- history / queue ----------------------------------------------------

    async def history_list(self, limit: int = 20, start: int = 0) -> dict[str, Any]:
        return await self.get("/server/history/list", params={"limit": limit, "start": start})

    async def history_totals(self) -> dict[str, Any]:
        return await self.get("/server/history/totals")

    async def job_queue_status(self) -> dict[str, Any]:
        return await self.get("/server/job_queue/status")

    async def job_queue_post(self, filenames: Sequence[str], reset: bool = False) -> Any:
        return await self.post(
            "/server/job_queue/job",
            json_body={"filenames": list(filenames), "reset": reset},
        )

    async def job_queue_delete(self, job_ids: Sequence[str] | None = None, all_: bool = False) -> Any:
        params = {"all": "true"} if all_ else {"job_ids": ",".join(job_ids or [])}
        return await self.delete("/server/job_queue/job", params=params)

    async def job_queue_pause(self) -> Any:
        return await self.post("/server/job_queue/pause")

    async def job_queue_start(self) -> Any:
        return await self.post("/server/job_queue/start")

    # -- machine ------------------------------------------------------------

    async def system_info(self) -> dict[str, Any]:
        return await self.get("/machine/system_info")

    async def proc_stats(self) -> dict[str, Any]:
        return await self.get("/machine/proc_stats")

    async def machine_reboot(self) -> Any:
        return await self.post("/machine/reboot")

    async def machine_shutdown(self) -> Any:
        return await self.post("/machine/shutdown")

    async def service_action(self, action: str, service: str) -> Any:
        return await self.post(f"/machine/services/{action}", params={"service": service})

    # -- config / logs ------------------------------------------------------

    async def server_config(self) -> dict[str, Any]:
        return await self.get("/server/config")

    async def temperature_store(self, include_monitors: bool = False) -> dict[str, Any]:
        return await self.get(
            "/server/temperature_store",
            params={"include_monitors": "true" if include_monitors else "false"},
        )

    async def gcode_store(self, count: int = 100) -> dict[str, Any]:
        return await self.get("/server/gcode_store", params={"count": count})

    # -- webcam -------------------------------------------------------------

    async def webcam_list(self) -> dict[str, Any]:
        return await self.get("/server/webcams/list")

    async def webcam_snapshot(self, url: str | None = None, *, max_bytes: int = 8_000_000) -> bytes:
        """Fetch a JPEG snapshot from crowsnest (not via Moonraker)."""
        base = url or self.settings.webcam_url
        if not base:
            raise NotSupported(
                "No webcam URL configured.",
                hint="Set QIDI_WEBCAM_URL, e.g. http://<printer-ip>:8080",
            )
        target = base if "action=" in base else base.rstrip("/") + "/?action=snapshot"
        parsed = urlparse(target)
        if self.settings.test_mode and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise InvalidRequest("Test mode refuses a non-loopback webcam host.")
        try:
            # trust_env=False for the same reason as the main client: the camera
            # is on the LAN and must not be fetched through an external proxy.
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout, verify=False, trust_env=False
            ) as c:
                resp = await c.get(target)
                resp.raise_for_status()
                return resp.content[:max_bytes]
        except httpx.HTTPError as exc:
            raise ConnectionFailed(
                f"Could not fetch a snapshot from {target}: {exc}",
                hint="Confirm the camera is enabled and crowsnest is serving on that port.",
            ) from exc

    # -- JSON-RPC over WebSocket -------------------------------------------

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One-shot JSON-RPC call over the WebSocket.

        Only needed for methods with no HTTP equivalent. Opens, calls, closes —
        a persistent subscription is out of scope for a stdio MCP server whose
        tools are request/response.
        """
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise NotSupported("The websockets package is not installed.") from exc

        parsed = urlparse(self.settings.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{scheme}://{parsed.hostname}:{parsed.port or 7125}/websocket"
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
        try:
            async with websockets.connect(
                ws_url,
                open_timeout=self.settings.http_timeout,
                close_timeout=2,
                additional_headers=self._headers(),
            ) as ws:
                await ws.send(json.dumps(payload))
                deadline = asyncio.get_running_loop().time() + self.settings.http_timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise ConnectionFailed(f"No JSON-RPC reply to {method} in time.")
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    msg = json.loads(raw)
                    if msg.get("id") != 1:
                        continue  # async notification; ignore
                    if "error" in msg:
                        err = msg["error"]
                        raise MoonrakerHTTPError(
                            int(err.get("code", 500)) if str(err.get("code", "")).isdigit() else 500,
                            str(err.get("message", err)),
                        )
                    return msg.get("result")
        except (TimeoutError, OSError) as exc:
            raise ConnectionFailed(f"WebSocket call {method} failed: {exc}") from exc


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return (resp.text or "").strip()[:400]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return str(err)
        return str(body.get("message") or body)[:400]
    return str(body)[:400]
