"""End-to-end behavioural validation against the in-process mock."""
import asyncio, json, logging, sys
sys.path.insert(0,'.')
logging.disable(logging.CRITICAL)
from aiohttp import web
from tests.mock_moonraker import MockPrinter, build_app
from qidi_mcp.config import load_settings
from qidi_mcp.server import build_server

async def session(**env):
    p = MockPrinter()
    runner = web.AppRunner(build_app(p), access_log=None); await runner.setup()
    site = web.TCPSite(runner,'127.0.0.1',0); await site.start()
    port = runner.addresses[0][1]
    st = load_settings({"QIDI_PRINTER_URL":f"http://127.0.0.1:{port}","QIDI_TEST_MODE":"1",**env})
    server, rt = build_server(st)
    return p, server, rt, runner

async def call(server, name, **kw):
    r = await server.call_tool(name, kw)
    d = getattr(r,'structuredContent',None) or json.loads(r.content[0].text)
    return d.get('result',d) if set(d)=={'result'} else d

async def main():
    print("A) READ-ONLY MODE")
    p, server, rt, runner = await session(QIDI_READ_ONLY="1")
    for tool,kw in [("get_printer_status",{}),("set_temperature",{"heater":"extruder","target":200,"confirm":True}),
                    ("home_axes",{"confirm":True}),("emergency_stop",{"confirm":True})]:
        r = await call(server, tool, **kw)
        print(f"   {tool:<22} ok={str(r.get('ok')):<5} err={r.get('error','-')}")
    print(f"   -> printer commands received: {p.commands}  estops={p.emergency_stops}")
    await rt.aclose(); await runner.cleanup()

    print("\nB) CONFIRMATION TIERS (read/write mode)")
    p, server, rt, runner = await session()
    r = await call(server,"get_printer_status");            print(f"   READ   get_printer_status        -> ok={r.get('ok')} (no confirm needed)")
    r = await call(server,"set_case_light",on=True);          print(f"   LOW    set_case_light            -> ok={r.get('ok')} err={r.get('error','-')}")
    r = await call(server,"home_axes");                       print(f"   MOTION home_axes (no confirm)    -> ok={r.get('ok')} err={r.get('error','-')}")
    r = await call(server,"home_axes",confirm=True);          print(f"   MOTION home_axes (confirm)       -> ok={r.get('ok')}")
    r = await call(server,"restart_klipper_host",confirm=True)
    tok = r.get('confirm_token')
    print(f"   DESTR  restart_klipper_host       -> ticket issued: {bool(tok)}")
    if tok:
        r2 = await call(server,"restart_klipper_host",confirm=True,confirm_token=tok)
        print(f"          redeem token              -> ok={r2.get('ok')}")
        r3 = await call(server,"restart_klipper_host",confirm=True,confirm_token=tok)
        print(f"          REPLAY same token         -> ok={r3.get('ok')} err={r3.get('error','-')}")
    await rt.aclose(); await runner.cleanup()

    print("\nC) SAFETY ENVELOPE")
    p, server, rt, runner = await session()
    for label,tool,kw in [("over-max temp","set_temperature",{"heater":"extruder","target":374,"confirm":True}),
                          ("raw G-code temp","run_gcode",{"script":"M104 S374","confirm":True}),
                          ("out-of-bounds move","run_gcode",{"script":"G1 X99999","confirm":True}),
                          ("vendor save-config","run_gcode",{"script":"SAVE_CONFIG_QD","confirm":True}),
                          ("line-num bypass","run_gcode",{"script":"N1 SAVE_CONFIG","confirm":True}),
                          ("NaN extrude","extrude_filament",{"distance":float('nan'),"confirm":True})]:
        r = await call(server,tool,**kw)
        print(f"   {label:<20} ok={str(r.get('ok')):<5} err={r.get('error','-')}")
    print(f"   -> nothing dangerous on wire: {not any(x in ' '.join(p.commands).upper() for x in ('S374','SAVE_CONFIG','99999','NAN'))}")
    await rt.aclose(); await runner.cleanup()

    print("\nD) CAPABILITY DISCOVERY")
    p, server, rt, runner = await session()
    caps = await call(server,"get_capabilities")
    th, dev, f = caps.get('thermal',{}), caps.get('devices',{}), caps.get('features',{})
    print(f"   kinematics={caps['printer']['kinematics']}  klipper={caps['printer']['klipper_version']}")
    print(f"   heaters={sorted(th.get('heaters',{}))}  heated_chamber={th.get('heated_chamber')}")
    print(f"   fans={len(dev.get('controllable_fans',[]))} pins={dev.get('output_pins')} leds={dev.get('leds')}")
    print(f"   bed_mesh={f.get('bed_mesh')} probe={f.get('probe')} z_tilt={f.get('z_tilt')} input_shaper={f.get('input_shaper')}")
    print(f"   soft_abort={f.get('soft_abort')}  macros={caps.get('macro_count')}  qidi_modules={len(caps.get('qidi_specific_klipper_modules',[]))}")
    print(f"   warnings={caps.get('warnings')}")
    await rt.aclose(); await runner.cleanup()
asyncio.run(main())
