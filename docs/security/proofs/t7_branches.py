from _h import *
def go(p, tool, risk, args, **kw):
    try:
        r = p.authorize(tool, risk, args, **kw); return "CLEARED" if r is None else "TICKET:"+r["confirm_token"]
    except QidiError as e: return f"REFUSED({e.code}:{(e.extra or {}).get('detail','')}{e.message[:40]})"

# strict_confirm: MOTION/THERMAL/PRINT need tickets; confirm=True alone must not clear
p = mk(QIDI_STRICT_CONFIRM="1")
for r in (Risk.LOW, Risk.MOTION, Risk.THERMAL, Risk.PRINT, Risk.DESTRUCTIVE):
    print(f"strict {r.value:11s} confirm=True no token ->", go(p, "t", r, {"a":1}, confirm=True))
t = p.authorize("home_axes", Risk.MOTION, {"axes":"xyz"})["confirm_token"]
print("strict MOTION redeem:", go(p, "home_axes", Risk.MOTION, {"axes":"xyz"}, confirm_token=t))
print("strict MOTION replay:", go(p, "home_axes", Risk.MOTION, {"axes":"xyz"}, confirm_token=t))

# non-strict: every tier with each combination
p = mk()
for r in (Risk.LOW, Risk.MOTION, Risk.THERMAL, Risk.PRINT, Risk.DESTRUCTIVE):
    print(f"{r.value:11s} nothing->{go(p,'t',r,{}):8s} confirm->{go(p,'t',r,{},confirm=True):8s} token=garbage->{go(p,'t',r,{},confirm_token='qidi-x')[:22]}")

# read-only: DESTRUCTIVE with a valid-looking token; EMERGENCY
p = mk(QIDI_READ_ONLY="1")
print("read_only DESTRUCTIVE token:", go(p,"reboot_printer",Risk.DESTRUCTIVE,{},confirm_token="qidi-x"), "| EMERGENCY:", go(p,"emergency_stop",Risk.EMERGENCY,{}))
print("read_only audit:", [(e['outcome'],e['detail']) for e in p.audit.tail()])

# interlocks vs ticket: is the ticket burned when an interlock refuses at redeem time?
p = mk()
t = p.authorize("delete_file", Risk.DESTRUCTIVE, {"path":"a"}, state=PrinterState(), interlocks=Interlocks(forbid_during_print=True))["confirm_token"]
print("redeem while printing:", go(p,"delete_file",Risk.DESTRUCTIVE,{"path":"a"},confirm_token=t,state=PrinterState(print_state="printing"),interlocks=Interlocks(forbid_during_print=True)))
print("then redeem after print ends (ticket preserved?):", go(p,"delete_file",Risk.DESTRUCTIVE,{"path":"a"},confirm_token=t,state=PrinterState(),interlocks=Interlocks(forbid_during_print=True)))

# ticket issued while printer was in state A: does redeem re-check state? (yes if tool passes interlocks) -- issue w/ homed, redeem unhomed
t = p.authorize("bed_mesh_calibrate", Risk.DESTRUCTIVE, {"profile":"default"}, state=PrinterState(homed_axes="xyz"), interlocks=Interlocks(require_homed=("x","y","z")))["confirm_token"]
print("issued homed, redeem unhomed:", go(p,"bed_mesh_calibrate",Risk.DESTRUCTIVE,{"profile":"default"},confirm_token=t,state=PrinterState(homed_axes=""),interlocks=Interlocks(require_homed=("x","y","z"))))

# Does a CLEARED MOTION/THERMAL call produce any audit line from authorize()?
p = mk()
p.authorize("home_axes", Risk.MOTION, {"axes":"xyz"}, confirm=True)
p.authorize("set_temperature", Risk.THERMAL, {"heater":"bed","target":100}, confirm=True)
print("audit lines after 2 CLEARED confirm=True calls:", p.audit.tail())
