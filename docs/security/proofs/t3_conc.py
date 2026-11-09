from _h import *
import asyncio, threading
p = mk(); D = Risk.DESTRUCTIVE
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
async def red():
    try: p.authorize("reboot_printer", D, {}, confirm_token=tok); return "CLEARED"
    except QidiError as e: return e.code
async def main(): return await asyncio.gather(*[red() for _ in range(4)])
print("asyncio 4x redeem:", asyncio.run(main()))
wins = []
for _ in range(300):
    tok = p.authorize("reboot_printer", D, {})["confirm_token"]
    res = []; b = threading.Barrier(8)
    def worker():
        b.wait()
        try: p.authorize("reboot_printer", D, {}, confirm_token=tok); res.append(1)
        except QidiError: res.append(0)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    wins.append(sum(res))
print("threaded 8-way redeem x300, max successes/token:", max(wins), "min:", min(wins))
