from _h import *
import qidi_mcp.policy as pol
D = Risk.DESTRUCTIVE
def go(p, tool, args, **kw):
    try:
        r = p.authorize(tool, D, args, **kw); return "CLEARED" if r is None else f"TICKET"
    except QidiError as e: return f"REFUSED({e.code})"
    except Exception as e: return f"CRASH {type(e).__name__}: {e}"

# clock control
now = [1000.0]
pol.time.monotonic = lambda: now[0]   # module-level 'time' is the real module; patch attr
p = mk(QIDI_CONFIRM_TTL="120")
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
now[0] += 119.999; print("t=119.999:", go(p,"reboot_printer",{},confirm_token=tok))
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
now[0] += 120.0; print("t=120.000 (boundary, uses >):", go(p,"reboot_printer",{},confirm_token=tok))
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
now[0] += 120.001; print("t=120.001:", go(p,"reboot_printer",{},confirm_token=tok))
# does an expired ticket linger in memory and get resurrected? (no sweep between)
tok = p.authorize("reboot_printer", D, {})["confirm_token"]
now[0] += 1000
print("expired ticket still in dict before any sweep:", tok in p.broker._tickets)
print("redeem after expiry, no intermediate sweep:", go(p,"reboot_printer",{},confirm_token=tok))
# clock going backwards? monotonic can't; skip.

# Degenerate TTL config values
for ttl in ["0", "-5", "inf", "nan", "1e308"]:
    p = mk(QIDI_CONFIRM_TTL=ttl)
    try:
        r = p.authorize("reboot_printer", D, {})
        tok = r["confirm_token"]; now[0] += 3600*24*365
        print(f"TTL={ttl}: issue ok expires_in={r['expires_in_seconds']}, redeem after 1 year:", go(p,"reboot_printer",{},confirm_token=tok))
    except Exception as e:
        print(f"TTL={ttl}: issue -> {type(e).__name__}: {e}; ticket count in broker: {len(p.broker._tickets)}; audit tail: {[e_['outcome'] for e_ in p.audit.tail()]}")
