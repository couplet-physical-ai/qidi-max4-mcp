from _h import *
import asyncio, threading, time
p = mk()
D = Risk.DESTRUCTIVE
def go(tool, args, **kw):
    try:
        r = p.authorize(tool, D, args, **kw)
        return "CLEARED" if r is None else f"TICKET {r['confirm_token']}"
    except QidiError as e:
        return f"REFUSED({e.code}): {e.message[:60]}"

# 1. confirm=True alone on DESTRUCTIVE
print("confirm-only destructive:", go("delete_file", {"path":"a"}, confirm=True))
print("confirm-only + bogus token:", go("delete_file", {"path":"a"}, confirm=True, confirm_token="qidi-AAAAAAAAAAAA"))
print("empty-string token:", go("delete_file", {"path":"a"}, confirm_token=""))

# 2. replay
r = p.authorize("delete_file", D, {"path":"a"}); tok = r["confirm_token"]
print("first redeem:", go("delete_file", {"path":"a"}, confirm_token=tok))
print("replay:", go("delete_file", {"path":"a"}, confirm_token=tok))

# 3. cross-tool
tok = p.authorize("delete_file", D, {"path":"a"})["confirm_token"]
print("cross-tool (delete_file tok -> reboot_printer {}):", go("reboot_printer", {}, confirm_token=tok))
print("...and is the ticket burned after the mismatch?", go("delete_file", {"path":"a"}, confirm_token=tok))

# 4. same tool, different args
tok = p.authorize("delete_file", D, {"path":"a.gcode","root":"gcodes"})["confirm_token"]
print("diff root:", go("delete_file", {"path":"a.gcode","root":"config"}, confirm_token=tok))
tok = p.authorize("run_gcode", D, {"script":"G28","lines":1,"allow_dangerous":False})["confirm_token"]
print("run_gcode script swap:", go("run_gcode", {"script":"M112","lines":1,"allow_dangerous":False}, confirm_token=tok))
tok = p.authorize("set_temperature", D, {"heater":"extruder","target":260})["confirm_token"]
print("temp int->float 260 vs 260.0:", go("set_temperature", {"heater":"extruder","target":260.0}, confirm_token=tok))
tok = p.authorize("set_temperature", D, {"heater":"extruder","target":260})["confirm_token"]
print("temp 260 -> 350:", go("set_temperature", {"heater":"extruder","target":350}, confirm_token=tok))

# 5. token issued for {} tool used with confirm flag variants
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
print("reboot redeem w/ extra confirm kw in args:", go("reboot_printer", {"confirm":True,"confirm_token":tok}, confirm_token=tok))

# 6. lower-tier tool with a destructive token (strict_confirm off)
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
try:
    p.authorize("home_axes", Risk.MOTION, {}, confirm=True, confirm_token=tok); print("MOTION w/ foreign token: CLEARED (!)")
except QidiError as e: print("MOTION w/ foreign token:", e.code)

# 7. token guessing space
import secrets; print("token sample:", p.broker.issue("x",{}).token, "(9 bytes = 72 bits)")

# 8. Concurrency: asyncio
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
async def red():
    try: p.authorize("reboot_printer", D, {}, confirm_token=tok); return "CLEARED"
    except QidiError as e: return e.code
print("asyncio 2x redeem:", asyncio.run(asyncio.gather(*[red() for _ in range(2)])))
# threads, many tokens
wins = []
for _ in range(200):
    tok = p.authorize("reboot_printer", D, {})["confirm_token"]
    res = []
    def worker():
        try: p.authorize("reboot_printer", D, {}, confirm_token=tok); res.append(1)
        except QidiError: res.append(0)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    wins.append(sum(res))
print("threaded 8-way redeem x200 trials, max successes per token:", max(wins), "min:", min(wins))
