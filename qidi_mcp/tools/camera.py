"""Webcam access.

On stock QIDI firmware the camera is served by crowsnest, not Moonraker: the
Max4's ``crowsnest.conf`` puts a 1920x1080 ustreamer instance on port 8080, so a
snapshot is ``http://<printer>:8080/?action=snapshot`` and the MJPEG stream is
``?action=stream``. Moonraker's ``/server/webcams/list`` may or may not know
about it depending on configuration, so we try Moonraker first and fall back to
the configured URL.

The snapshot is returned as an MCP ``Image``, so a vision-capable agent can
actually *look* at the print — which is the single most useful thing an agent can
do about print quality.

And the single most dangerous thing an agent can do about print quality is
*believe* it looked. This machine turns its case light off whenever the
touchscreen sleeps (``smart_output_pin caselight`` syncs to ``SCREEN``), so a
snapshot taken of an idle printer is a valid, well-formed, entirely black JPEG.
On 2026-09-16 an agent was handed three of those, described a clean plate and
then a finished Benchy in detail, and reported a successful print. The bed was
empty: the job had extruded nothing at all. Nobody noticed for a day, because
the only "observation" in the loop was invented.

So a frame that cannot possibly show anything is now refused rather than
returned — see ``_jpeg_dimensions`` and ``FEATURELESS_BYTES_PER_PIXEL``. An
agent that never receives a black image cannot describe one.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

from ..errors import QidiError
from ..runtime import Runtime, guarded

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=False, open_world_hint=True)

#: Compressed bytes per pixel below which a JPEG carries essentially no detail.
#: Measured on the lab Max4 at 1920x1080: an unlit chamber gives 0.020 B/px
#: (41 KB) and the same view with the case light on gives 0.079-0.081 B/px
#: (164-167 KB). A featureless frame is 4x cheaper to encode than a real one, so
#: the threshold sits in a wide, empty gap rather than on a knife edge.
FEATURELESS_BYTES_PER_PIXEL = 0.030

# SOF0..SOF15 minus the marker IDs that are not frame headers (DHT/JPG/DAC).
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg_dimensions(blob: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a JPEG's frame header, or ``None`` if unparseable.

    Deliberately dependency-free: the server's install footprint is httpx plus the
    MCP SDK, and pulling in Pillow to measure one number would be a poor trade.
    Returning ``None`` means "cannot measure", and every caller treats that as
    permission to pass the frame through — a heuristic that blocks images it does
    not understand would be worse than the problem it fixes.
    """
    if blob[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(blob)
    while i + 9 < n:
        if blob[i] != 0xFF:
            i += 1
            continue
        marker = blob[i + 1]
        if marker == 0xFF:  # fill byte, legal before any marker
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # SOI, TEM, RSTn
            i += 2
            continue
        if marker == 0xDA:  # start of scan: no frame header before the data
            return None
        segment_length = int.from_bytes(blob[i + 2 : i + 4], "big")
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            height = int.from_bytes(blob[i + 5 : i + 7], "big")
            width = int.from_bytes(blob[i + 7 : i + 9], "big")
            return (width, height) if width and height else None
        i += 2 + segment_length
    return None


async def _case_light_on(rt: Runtime) -> bool | None:
    """Is the chamber lit? ``None`` when the printer will not say.

    Stock firmware exposes ``output_pin caselight``; QIDI's own build wraps it in a
    ``smart_output_pin`` that also tracks the touchscreen. Either answers the only
    question that matters here.
    """
    try:
        status = await rt.client.query(
            {"output_pin caselight": None, "smart_output_pin caselight": None}
        )
    except QidiError:
        return None
    for key in ("output_pin caselight", "smart_output_pin caselight"):
        section = status.get(key)
        if isinstance(section, dict) and section.get("value") is not None:
            try:
                return float(section["value"]) > 0.05
            except (TypeError, ValueError):
                continue
    return None


async def snapshot_candidates(rt: Runtime) -> list[str]:
    """Snapshot URLs to try, most specific first, deduplicated.

    A single advertised URL is not enough: Moonraker reports a *relative* path, and
    whether nginx proxies it or crowsnest answers directly varies by firmware. On
    the lab Max4 the relative path resolved against :80 is the one that works and
    :8080 is closed -- the reverse of what the stock config suggests -- so trying
    every candidate is what makes a working camera actually get found.

    Shared with a browser dashboard's camera proxy, so a person and an agent find
    the camera the same way.
    """
    candidates: list[str] = []
    try:
        listing = await rt.client.webcam_list()
        for cam in (listing or {}).get("webcams", []):
            snapshot = cam.get("snapshot_url") or cam.get("stream_url") or ""
            if not snapshot:
                continue
            if snapshot.startswith("http"):
                candidates.append(snapshot)
            else:
                path = snapshot if snapshot.startswith("/") else f"/{snapshot}"
                candidates.append(f"http://{rt.settings.host}{path}")
    except QidiError:
        pass
    if rt.settings.webcam_url:
        candidates.append(rt.settings.webcam_url)
    return list(dict.fromkeys(candidates))


def frame_is_featureless(blob: bytes) -> bool:
    """Whether a JPEG is too sparse to show anything (see FEATURELESS_BYTES_PER_PIXEL)."""
    dimensions = _jpeg_dimensions(blob)
    if not dimensions:
        return False
    width, height = dimensions
    return len(blob) / float(width * height) < FEATURELESS_BYTES_PER_PIXEL


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="get_webcam_snapshot",
        description=(
            "Grab a still image from the printer's camera and return it for inspection. Use "
            "this to check on a print: whether the first layer stuck, whether there is "
            "spaghetti, whether the part came loose. This printer switches its case light "
            "off when the touchscreen sleeps, so an idle machine usually returns a black "
            "frame: rather than hand you an image with nothing in it, the tool refuses with "
            "error 'frame_too_dark' and tells you to call set_case_light first. Pass "
            "allow_dark=true only if you want the raw bytes regardless — never describe the "
            "contents of a frame returned that way."
        ),
        annotations=READ,
    )
    @guarded
    async def get_webcam_snapshot(
        camera_url: str | None = None, allow_dark: bool = False
    ) -> Image | dict[str, Any]:
        candidates = [camera_url] if camera_url else await snapshot_candidates(rt)

        attempts: list[dict[str, str]] = []
        for candidate in candidates:
            try:
                blob = await rt.client.webcam_snapshot(candidate)
            except QidiError as exc:
                attempts.append({"url": candidate, "error": exc.code})
                continue
            if not blob:
                attempts.append({"url": candidate, "error": "empty_response"})
                continue
            fmt = "png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
            dimensions = _jpeg_dimensions(blob) if fmt == "jpeg" else None
            if dimensions and not allow_dark:
                width, height = dimensions
                bytes_per_pixel = len(blob) / float(width * height)
                if bytes_per_pixel < FEATURELESS_BYTES_PER_PIXEL:
                    light_on = await _case_light_on(rt)
                    return {
                        "ok": False,
                        "error": "frame_too_dark",
                        "message": (
                            "The camera returned a frame with no detail in it "
                            f"({bytes_per_pixel:.3f} compressed bytes/pixel at "
                            f"{width}x{height}; anything below "
                            f"{FEATURELESS_BYTES_PER_PIXEL:.3f} cannot show the bed). "
                            "Refusing to return it so it cannot be mistaken for a look at "
                            "the printer."
                        ),
                        "url": candidate,
                        "resolution": [width, height],
                        "bytes": len(blob),
                        "bytes_per_pixel": round(bytes_per_pixel, 4),
                        "case_light_on": light_on,
                        "hint": (
                            "Call set_case_light(on=true), wait about two seconds, then take "
                            "the snapshot again. This printer turns the light off when its "
                            "touchscreen sleeps, which is why an idle machine looks black. "
                            "If you truly want the frame anyway, pass allow_dark=true — but "
                            "do not report what is in it."
                        )
                        if light_on is not False
                        else (
                            "The case light is confirmed OFF. Call set_case_light(on=true), "
                            "wait about two seconds, then retake the snapshot."
                        ),
                    }
            return Image(data=blob, format=fmt)

        return {
            "ok": False,
            "error": "snapshot_unavailable",
            "message": "No camera URL returned an image.",
            "attempted": attempts or [{"url": "(none configured)", "error": "no_candidates"}],
            "hint": (
                "Check the camera is plugged in and crowsnest is running. You can pass an "
                "explicit camera_url, or set QIDI_WEBCAM_URL to the URL verified on this "
                f"firmware: http://{rt.settings.host}/webcam/?action=snapshot (the nginx "
                "proxy on :80 -- port 8080 is closed on the lab Max4)."
            ),
        }

    @server.tool(
        name="list_webcams",
        description=(
            "The cameras this printer exposes, with their stream and snapshot URLs. On stock "
            "QIDI firmware the camera is a crowsnest ustreamer instance on port 8080."
        ),
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    @guarded
    async def list_webcams() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "configured_snapshot_base": rt.settings.webcam_url,
            # MEASURED on the lab Max4, not read off crowsnest.conf: port 8080 is
            # CLOSED and the working snapshot is nginx's /webcam/ proxy on :80.
            # The docs-derived :8080 guess sent an earlier investigation down the
            # "the camera service must be dead" path when the camera was fine --
            # so the verified URL is listed first and labelled as verified.
            "verified_urls": {
                "snapshot": f"http://{rt.settings.host}/webcam/?action=snapshot",
                "stream_mjpeg": f"http://{rt.settings.host}/webcam/?action=stream",
                "note": (
                    "Confirmed against the lab machine: 1920x1080 JPEG via the nginx "
                    "/webcam/ proxy on port 80. Port 8080 is closed on this firmware, "
                    "despite what crowsnest.conf implies."
                ),
            },
        }
        try:
            listing = await rt.client.webcam_list()
            payload["moonraker_webcams"] = (listing or {}).get("webcams", [])
        except QidiError as exc:
            payload["moonraker_webcams"] = []
            payload["moonraker_note"] = (
                f"Moonraker has no webcam registry available ({exc.code}); the crowsnest URLs "
                "above still work."
            )
        return payload

    _ = (get_webcam_snapshot, list_webcams)
