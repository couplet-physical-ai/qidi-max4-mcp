"""Through the real MCP server: does JSON int vs float for the same numeric value break/allow redemption?
And can run_macro params of different JSON types collide to the same fingerprint but a different script?"""
import sys, os, asyncio, json, contextlib, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
os.environ["QIDI_TEST_MODE"]="1"
from aiohttp import web
from mock_moonraker import MockPrinter, build_app
from qidi_mcp.config import load_settings
from qidi_mcp.server import build_server
from qidi_mcp.policy import ConfirmationBroker
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
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start(); port = runner.addresses[0][1]
    server, rt = build_server(load_settings({"QIDI_PRINTER_URL": f"http://127.0.0.1:{port}", "QIDI_TEST_MODE": "1"}, ))
    try:
        t = await call(server, "set_temperature", heater="extruder", target=300)
        print("issue target=300 (int):", t.get("status"), t.get("arguments_locked_to"))
        r = await call(server, "set_temperature", heater="extruder", target=300.0, confirm_token=t["confirm_token"])
        print("redeem target=300.0 (float):", r.get("ok"), r.get("error") or r.get("command_sent"))
        # run_macro params typing: {"S": 1} vs {"S": true} vs {"S": "1"}
        fp = ConfirmationBroker.fingerprint
        print("run_macro fp S=1 vs S=true collide?", fp("run_macro", {"macro":"M","params":{"S":1}}) == fp("run_macro", {"macro":"M","params":{"S":True}}))
        print("run_macro fp S=1 vs S='1' collide?", fp("run_macro", {"macro":"M","params":{"S":1}}) == fp("run_macro", {"macro":"M","params":{"S":"1"}}))
        t = await call(server, "run_macro", macro="SET_HEATER_TEMPERATURE", params={"HEATER":"extruder","TARGET":250})
        print("run_macro ticket will_send:", t.get("will_send"))
        r = await call(server, "run_macro", macro="SET_HEATER_TEMPERATURE", params={"HEATER":"extruder","TARGET":"250"}, confirm_token=t["confirm_token"])
        print("redeem with TARGET as string '250' (same script):", r.get("ok"), r.get("error"))
    finally:
        await rt.aclose(); await runner.cleanup()
asyncio.run(main())
