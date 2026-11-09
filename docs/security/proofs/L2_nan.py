"""L2: NaN / inf as a *tool argument* (not an env var) against every numeric guard."""
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "audit", "scripts"))
from _h import mk
from qidi_mcp.errors import QidiError

p = mk()
NAN = float("nan")
INF = float("inf")

print("envelope: extruder max=%s  bed max=%s  chamber max=%s  jog=%s  extrude=%s  feedrate=%s"
      % (p.settings.safety.extruder.max, p.settings.safety.bed.max,
         p.settings.safety.chamber.max, p.settings.safety.max_jog_distance,
         p.settings.safety.max_extrude_distance, p.settings.safety.max_move_feedrate))
print()


def probe(label, fn, *a):
    try:
        r = fn(*a)
        return "PASSED THE GUARD" + ("" if r is None else " -> returned %r" % (r,))
    except QidiError as e:
        return "refused (%s)" % e.code
    except Exception as e:
        return "raised %s: %s" % (type(e).__name__, e)


print("--- check_temperature ---")
for h in ["extruder", "heater_bed", "chamber"]:
    for v in [NAN, INF, -INF, 1e9, 400.0]:
        r = probe("", p.check_temperature, h, v)
        flag = "   <== BYPASS" if "PASSED" in r else ""
        print("  check_temperature(%-10s, %-6s) -> %s%s" % (h, v, r, flag))

print()
print("--- check_position (x/y/z) ---")
for ax in ["x", "y", "z"]:
    for v in [NAN, INF, -INF, 1e9, -1e9]:
        r = probe("", p.check_position, ax, v)
        flag = "   <== BYPASS" if "PASSED" in r else ""
        print("  check_position(%s, %-8s) -> %s%s" % (ax, v, r, flag))

print()
print("--- check_jog / check_extrude / check_feedrate / check_fan_speed ---")
for v in [NAN, INF, -INF, 1e9]:
    r = probe("", p.check_jog, "x", v)
    print("  check_jog(x, %-8s)      -> %s%s" % (v, r, "   <== BYPASS" if "PASSED" in r else ""))
for v in [NAN, INF, -INF, 1e9]:
    r = probe("", p.check_extrude, v)
    print("  check_extrude(%-8s)      -> %s%s" % (v, r, "   <== BYPASS" if "PASSED" in r else ""))
for v in [NAN, INF, -INF, 0.0, -5.0, 1e9]:
    r = probe("", p.check_feedrate, v)
    print("  check_feedrate(%-8s)     -> %s%s" % (v, r, "   <== BYPASS" if "PASSED" in r else ""))
for v in [NAN, INF, -INF, 2.0, -1.0]:
    r = probe("", p.check_fan_speed, v)
    print("  check_fan_speed(%-8s)    -> %s%s" % (v, r, "   <== BYPASS" if "PASSED" in r else ""))

print()
print("--- what does a NaN look like once formatted into G-code? ---")
print("  'G1 X%s F%s' %% (nan, nan) -> %r" % ("nan", "nan", "G1 X%s F%s" % (NAN, NAN)))
print("  'M104 S%.1f' %% nan        -> %r" % (NAN, "M104 S%.1f" % NAN))
print("  min(nan, 12000) = %r   min(12000, nan) = %r" % (min(NAN, 12000.0), min(12000.0, NAN)))
print()
print("--- temperature_needs_confirm with NaN (does a NaN dodge the ticket?) ---")
for h in ["extruder", "bed", "chamber"]:
    print("  temperature_needs_confirm(%-8s, nan) -> %s"
          % (h, p.temperature_needs_confirm(h, NAN)))
