from _h import *
import tracemalloc, time
p = mk(); tracemalloc.start()
t0=time.perf_counter()
for i in range(50_000): p.authorize("reboot_printer", Risk.DESTRUCTIVE, {"n":i})
cur,_ = tracemalloc.get_traced_memory()
print(f"50k unredeemed tickets: {len(p.broker._tickets)} live, {cur/1e6:.1f} MB, {time.perf_counter()-t0:.2f}s; audit tail len={len(p.audit.tail(1000))}")
t0=time.perf_counter(); p.authorize("reboot_printer", Risk.DESTRUCTIVE, {"n":-1}); print(f"next issue (sweep over 50k): {(time.perf_counter()-t0)*1000:.1f} ms")
