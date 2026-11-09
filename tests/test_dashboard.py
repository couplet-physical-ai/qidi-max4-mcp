"""The dashboard is a window, not a control panel.

The property that matters is that it cannot move the printer even if the operator
launched it with writes already enabled in their environment.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request

import pytest
from aiohttp import web

from qidi_mcp.dashboard import _LIVE_TOOLS, _STATIC_TOOLS, _Bridge, _handler
from qidi_mcp.dashboard_page import PAGE


@pytest.fixture
def mock_url(printer):
    """Mock Moonraker on its OWN thread and loop.

    The dashboard bridge is deliberately synchronous -- a threaded HTTP server
    calls into it and blocks. If the mock shared the test's event loop, that block
    would stop the loop that has to answer the request, and the two would deadlock.
    So the mock gets an independent loop here.
    """
    from tests.mock_moonraker import build_app

    ready = threading.Event()
    box: dict[str, object] = {}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def start() -> None:
            runner = web.AppRunner(build_app(printer), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            box["port"] = runner.addresses[0][1]
            box["runner"] = runner
            ready.set()

        loop.run_until_complete(start())
        box["loop"] = loop
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True, name="mock-moonraker")
    thread.start()
    assert ready.wait(timeout=15), "mock Moonraker did not start"
    yield f"http://127.0.0.1:{box['port']}"
    loop = box["loop"]
    runner = box["runner"]
    assert isinstance(loop, asyncio.AbstractEventLoop)
    # Close connections before killing the loop, or aiohttp leaves half-finished
    # request tasks and warns "Task was destroyed but it is pending".
    assert isinstance(runner, web.AppRunner)
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=10)


@pytest.fixture
def dash(monkeypatch, mock_url):
    """A bridge pointed at the mock, launched with writes deliberately enabled."""
    monkeypatch.setenv("QIDI_PRINTER_URL", mock_url)
    monkeypatch.setenv("QIDI_TEST_MODE", "1")
    monkeypatch.setenv("QIDI_READ_ONLY", "0")  # the dashboard must override this
    bridge = _Bridge()
    yield bridge
    bridge.close()


def test_dashboard_forces_read_only_even_when_the_environment_enables_writes(dash):
    assert dash.settings.read_only is True
    assert dash.snapshot()["read_only"] is True


def test_a_mutating_tool_is_refused_through_the_dashboards_own_server(dash):
    """Same code path an agent uses, with the write half bolted shut."""
    result = dash._call_with_args("set_case_light", {"on": True})
    assert result["ok"] is False
    assert result["error"] == "read_only_mode"


def test_snapshot_carries_every_panel_the_page_reads(dash):
    snap = dash.snapshot()
    for name in _LIVE_TOOLS + _STATIC_TOOLS:
        assert name in snap, f"page reads {name} but snapshot omits it"
    assert snap["get_printer_status"]["ok"] is True
    assert snap["get_job_status"]["ok"] is True


def test_snapshot_reports_no_box_on_a_stock_printer(dash):
    """The page hides its Box card from this; it must be able to tell from the payload."""
    box = dash.snapshot()["get_box_status"]
    assert box["ok"] is True
    assert box["detected"] is False and box["problems"] == []
    assert box["slots"] == []


def test_snapshot_carries_a_box_when_one_is_fitted(dash, printer):
    printer.attach_box(1)
    box = dash.snapshot()["get_box_status"]
    assert box["detected"] is True and box["ready"] is True
    assert len(box["slots"]) == 4


def test_page_has_a_box_card_that_hides_itself_and_never_trusts_a_colour():
    assert 'id="box-card" hidden' in PAGE
    # Colours go into a style; only a plain #RRGGBB from the printer's own table may.
    assert "/^#[0-9a-fA-F]{6}$/" in PAGE


def test_http_serves_the_page_and_live_json(dash):
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(dash))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            body = r.read().decode()
            assert r.status == 200
            assert "QIDI Max 4" in body
        with urllib.request.urlopen(base + "/api/state", timeout=10) as r:
            data = json.loads(r.read())
            assert data["read_only"] is True
            assert data["get_printer_status"]["ok"] is True
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/nope", timeout=10)
        assert exc.value.code == 404
    finally:
        httpd.shutdown()


# -- page hygiene: cheap guards for the things that silently break a page -----
def test_page_defines_both_themes_and_never_depends_on_a_font_cdn():
    assert "prefers-color-scheme: dark" in PAGE
    assert '[data-theme="dark"]' in PAGE
    assert '[data-theme="light"]' in PAGE
    # A dashboard that falls back to Times because a CDN was blocked is worse
    # than one that always used the system stack.
    for bad in ("fonts.googleapis", "fonts.gstatic", "cdn.jsdelivr", "unpkg.com"):
        assert bad not in PAGE, f"page reaches out to {bad}"


def test_page_respects_reduced_motion_and_marks_up_progress_accessibly():
    assert "prefers-reduced-motion" in PAGE
    assert 'role="progressbar"' in PAGE
    assert "aria-valuenow" in PAGE


def test_page_reads_policy_keys_that_actually_exist(dash):
    """The ceilings silently vanished once because the JS guessed the wrong keys.

    The page read ``pol.limits`` and ``t.firmware_max_c``; the server actually sends
    ``temperature_limits_c`` and ``hardware_ceilings_c``. Nothing failed -- the
    meters just quietly lost the envelope, which is the one thing they exist to
    show. Pin the contract in both directions.
    """
    policy = dash.snapshot()["get_safety_policy"]
    for key in ("temperature_limits_c", "hardware_ceilings_c"):
        assert key in policy, f"server no longer sends {key}"
        assert key in PAGE, f"page stopped reading {key}"
    assert policy["temperature_limits_c"]["extruder"]["max"] > 0
    assert "pol.limits" not in PAGE, "page reverted to a key the server never sends"


def test_page_declares_no_duplicate_function_names():
    """A tick-label helper was once named `tick`, same as the async poll loop.

    The later declaration won, so every meter rendered '[object Promise]'. Cheap to
    catch, invisible in review.
    """
    import re

    names = re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", PAGE)
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate function declarations shadow each other: {dupes}"


def test_page_is_balanced_html():
    import re

    for tag in ("html", "head", "body", "style", "script", "footer", "section"):
        # \b so <header> is not counted as an unclosed <head>.
        opens = len(re.findall(rf"<{tag}\b", PAGE))
        closes = PAGE.count(f"</{tag}>")
        assert opens == closes, f"unbalanced <{tag}>: {opens} open, {closes} closed"


def test_page_shows_who_is_driving_the_shared_printer(dash):
    """On a shared machine, "may I touch this?" is the page's first job.

    The panel reads ``get_printer_lease``, whose payload is keyed by tool name like
    every other panel. Pin the contract in both directions -- the same mistake that
    once made the temperature ceilings vanish is available here.
    """
    snap = dash.snapshot()
    lease = snap["get_printer_lease"]
    assert lease["ok"] is True
    for key in ("coordination", "held", "you_hold_it"):
        assert key in lease, f"server no longer sends {key}"
        assert key in PAGE, f"page stopped reading {key}"
    assert "get_printer_lease" in PAGE
    # Elements the JS writes into must exist, or the panel fails silently.
    for element_id in ("lease", "lease-rows", "lease-note"):
        assert f'id="{element_id}"' in PAGE, f"page has no #{element_id} to fill"


def test_lease_panel_renders_a_holder_when_one_exists(dash, printer):
    """Populate the printer's database and check the aggregate actually reports it."""
    import time as _time

    printer.database["qidi_mcp"] = {
        "lease": {
            "holder_id": "abc123",
            "owner": "someone@bench",
            "reason": "a long print",
            "acquired_at": _time.time(),
            "expires_at": _time.time() + 900,
            "renewed_at": _time.time(),
        }
    }
    lease = dash.snapshot()["get_printer_lease"]
    assert lease["held"] is True
    assert lease["you_hold_it"] is False, "the dashboard must never claim the machine"
    assert lease["lease"]["owner"] == "someone@bench"
    assert lease["lease"]["minutes_remaining"] > 0


def test_the_dashboard_never_takes_the_lease(dash):
    """It is a window, not a claim. Watching must not lock anybody out."""
    assert dash.snapshot()["get_printer_lease"]["held"] is False
    assert dash.snapshot()["get_printer_lease"]["you_hold_it"] is False


def test_lease_times_read_as_durations_not_decimal_minutes():
    """"126.9 min" was the header badge mid-print; the page formats seconds instead."""
    assert "minutes_remaining" not in PAGE
    assert PAGE.count("dur(ls.lease.seconds_remaining)") == 3
    assert "Cooperative only" not in PAGE, "touchscreen prints take the lease now"


def test_an_idle_printer_does_not_show_the_last_prints_layers():
    """print_stats.info outlives the job, so the card gates the per-job rows on state."""
    assert "const jobRows = live || ended;" in PAGE
    assert "jobRows && job.current_layer != null" in PAGE
    assert "live && job.estimated_remaining_s != null" in PAGE


def test_the_roster_stacks_into_cards_on_a_phone():
    """A four-column table scrolled sideways inside its card hid the Revoke button."""
    rule = PAGE[PAGE.index("#roster tr:first-child { display: none; }") - 400:]
    assert "@media (max-width: 620px)" in rule
    assert "#roster, #roster tbody, #roster tr, #roster td { display: block;" in PAGE


def test_the_role_table_uses_short_headers_on_a_phone():
    """Full-width role columns squeezed every capability onto three or four lines."""
    assert '["Maintainer", "Maint."]' in PAGE
    phone = PAGE[PAGE.index("@media (max-width: 620px) {\n    /* On a phone the roster"):]
    assert ".matrix .long { display: none; }" in phone[:1500]
    # The hide rule must come before the phone rule that shows the short names:
    # same specificity, so a later base rule silently wins and the headers vanish.
    assert PAGE.index(".matrix .short { display: none; }") < PAGE.index(".matrix .short { display: inline; }")


def test_row_buttons_name_their_file_or_person():
    """Repeated "Print…" / "Revoke" buttons are ambiguous to a screen reader."""
    assert 'b.setAttribute("aria-label", "Print " + f.path);' in PAGE
    assert 'btn.setAttribute("aria-label", `${text}: ${label}`);' in PAGE


def test_the_start_dialog_only_promises_a_chat_ping_when_chat_is_connected():
    assert "@mention these people in chat when it ends" not in PAGE
    assert 'fv.slack_enabled' in PAGE and "nobody is pinged when it ends" in PAGE


def test_stale_readings_are_dimmed_and_aged_not_shown_as_live():
    """A failed poll left old temperatures on screen looking live."""
    assert "const STALE_MS = 10000;" in PAGE
    assert "no reading for ${age < 60000" in PAGE
    assert "body.stale #heaters" in PAGE
    # An unreachable printer keeps the last good readings instead of blanking them,
    # which is what makes "showing the last one" true.
    assert "if (lastGood) { freshness(); return; }" in PAGE
    # The raw API address moved to the tooltip; it means nothing to most people.
    assert 'txt("host", d.printer_url' not in PAGE


def test_a_self_advancing_queue_is_a_warning_not_a_footnote():
    assert '$("queue-note").className = jobs.length && auto ? "warn-box" : "note";' in PAGE
    assert "on top of whatever is still on" in PAGE


def test_a_camera_frame_that_stops_updating_is_labelled_frozen_not_live():
    assert "const CAM_STALE_MS = 8000;" in PAGE
    assert 'badge($("cam-badge"), "warn", "frozen")' in PAGE
    assert "} catch (e) { camFreshness(); return; }" in PAGE
    assert ".cam img.frozen" in PAGE


def test_the_picker_says_when_the_directory_is_not_connected():
    """Without a directory connector key every search returned "No one found", which is wrong."""
    assert "directoryDown = out.ok === false;" in PAGE
    assert "Name search is not connected yet." in PAGE


def test_a_multi_step_action_stays_on_the_printer_it_began_on():
    """Upload -> prepare -> start, and cancel -> confirm, must not follow a printer switch."""
    assert "pending = { filename, token: t.confirm_token, leaseAcquired: lease, printer: target };" in PAGE
    assert '{ filename: pending.filename, confirm_token: pending.token, followers }, pending.printer);' in PAGE
    assert '{ tool: "release_printer_lease", args: {} }, p.printer);' in PAGE
    assert "if (target !== PRINTER) {" in PAGE            # upload finished after a switch
    assert 'act("cancel_print", { confirm_token: t.confirm_token }, "Cancelled.", target);' in PAGE
    assert "if (pending || $(\"confirm\").open) abandon();" in PAGE   # switching closes the dialog


def test_answers_about_another_printer_are_dropped_not_drawn():
    for route in ("/api/state", "/api/camera.jpg", "/api/files", "/api/followers", "/api/queue"):
        assert f'P("{route}", asked)' in PAGE, route
    assert PAGE.count("if (asked !== PRINTER") >= 5


def test_the_camera_can_be_enlarged_and_zoomed_in_the_dashboard():
    """Clicking the picture (or Expand) opens it full size; click, wheel and pinch zoom."""
    assert '<dialog class="viewer" id="cam-view"' in PAGE
    assert '$("cam-img").addEventListener("click", openViewer);' in PAGE
    assert '$("cam-open").addEventListener("click", openViewer);' in PAGE
    # Zoom keeps the point under the pointer still, and never pans off the picture.
    assert "zx = px - (px - zx) * (next / zs);" in PAGE
    # Panning stops at the picture, not in the letterbox bars around it.
    assert "return Math.min(-pad * zs, Math.max(stageLen - (pad + picLen) * zs, t));" in PAGE
    assert "zx = clampAxis(zx, w, fit ? img.naturalWidth * fit : w);" in PAGE
    assert "const ZOOM_MAX = 6, ZOOM_CLICK = 2.5, ZOOM_BUTTON = 1.5;" in PAGE
    assert 'stage.addEventListener("wheel"' in PAGE and "{ passive: false }" in PAGE
    assert "if (ptrs.size === 2) {" in PAGE                       # pinch
    assert "Math.hypot(px - downAt[0], py - downAt[1]) > 4" in PAGE   # a wobble is still a click


def test_the_enlarged_camera_is_the_same_live_frame_with_the_same_verdict():
    # One fetch feeds both pictures, swapped before the old object URL is revoked.
    assert '$("cv-img").src = url;' in PAGE
    assert PAGE.index('$("cv-img").src = url;') < PAGE.index("URL.revokeObjectURL(camUrl)")
    assert "async function refreshCamera() { await fetchCamera(); camMirror(); }" in PAGE
    # Frozen and dark are labelled in the viewer too, not only on the card.
    assert '$("cv-badge").className = $("cam-badge").className;' in PAGE
    assert 'vi.classList.toggle("frozen", img.classList.contains("frozen"));' in PAGE
    assert ".stage img.frozen" in PAGE


def test_the_enlarged_camera_never_shows_another_printer():
    assert '$("cv-img").removeAttribute("src");' in PAGE
    assert 'if ($("cam-view").open) $("cam-view").close();' in PAGE
    # It also closes when the camera goes away rather than freezing full screen.
    assert 'if ($("cam-view").open && ($("cam-card").hidden || !shown)) $("cam-view").close();' in PAGE
