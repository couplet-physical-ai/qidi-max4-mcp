"""L1: can any env var RAISE a limit? Zero/negative/huge/non-numeric/at-ceiling."""
import os
import sys

sys.path.insert(0, os.getcwd())
from qidi_mcp.config import load_settings
from qidi_mcp.errors import InvalidRequest
from qidi_mcp.limits import MAX4, DEFAULT_SAFETY

BASE = {"QIDI_PRINTER_URL": "http://127.0.0.1:7125", "QIDI_TEST_MODE": "1"}


def s(**env):
    e = dict(BASE)
    e.update(env)
    return load_settings(e)


print("hardware ceilings: extruder=%.0f bed=%.0f chamber=%.0f"
      % (MAX4.extruder_max_temp, MAX4.bed_max_temp, MAX4.chamber_max_temp))
print("vendor macro caps : extruder=370 bed=120 chamber=65  (heaters.cfg)")
print("defaults          : extruder=%.0f bed=%.0f chamber=%.0f"
      % (DEFAULT_SAFETY.extruder.max, DEFAULT_SAFETY.bed.max, DEFAULT_SAFETY.chamber.max))
print()

print("--- attempts to RAISE the extruder ceiling ---")
for raw in ["1000", "375", "374.9", "1e9", "0", "-50", "300.0000001", "0x1", " 350 ",
            "3_0_0", "inf", "-inf", "nan", "NaN", "abc", "", "1e400"]:
    try:
        st = s(QIDI_MAX_EXTRUDER_TEMP=raw)
        e = st.safety.extruder
        flag = ""
        if e.max > MAX4.extruder_max_temp:
            flag = "  <== ABOVE HARDWARE CEILING"
        elif e.max > 370:
            flag = "  <== above vendor macro cap 370"
        if e.min > e.max:
            flag += "  [min>max invariant broken]"
        print("  %-16r -> min=%-6s max=%-8s confirm_above=%-8s%s"
              % (raw, e.min, e.max, e.confirm_above, flag))
    except InvalidRequest as exc:
        print("  %-16r -> REFUSED: %s" % (raw, exc.message))

print()
print("--- bed / chamber at and above ceiling ---")
for var, key, hwmax, vendor in [("QIDI_MAX_BED_TEMP", "bed", MAX4.bed_max_temp, 120),
                                ("QIDI_MAX_CHAMBER_TEMP", "chamber", MAX4.chamber_max_temp, 65)]:
    for raw in ["9999", str(hwmax), str(vendor)]:
        st = s(**{var: raw})
        h = getattr(st.safety, key)
        flag = "  <== ABOVE HW" if h.max > hwmax else ("  <== above vendor cap %d" % vendor if h.max > vendor else "")
        print("  %s=%-6s -> max=%-6s%s" % (var, raw, h.max, flag))

print()
print("--- confirm_above: can confirmation be disabled? ---")
for raw in ["1000", "375", "0", "-1"]:
    st = s(QIDI_MAX_EXTRUDER_TEMP="375", QIDI_CONFIRM_EXTRUDER_TEMP=raw)
    e = st.safety.extruder
    # temperature_needs_confirm uses target > confirm_above
    at_max_needs_confirm = 375.0 > e.confirm_above
    print("  CONFIRM_EXTRUDER_TEMP=%-6r -> confirm_above=%-6s ; target=375 needs ticket? %s%s"
          % (raw, e.confirm_above, at_max_needs_confirm,
             "" if at_max_needs_confirm else "  <== NO TICKET AT HW MAX"))

print()
print("--- other envelope fields ---")
for var, raw in [("QIDI_MAX_JOG_DISTANCE", "1e9"), ("QIDI_MAX_JOG_DISTANCE", "-5"),
                 ("QIDI_MAX_EXTRUDE_DISTANCE", "1e9"), ("QIDI_MAX_FEEDRATE", "1e9"),
                 ("QIDI_MAX_FEEDRATE", "0"), ("QIDI_MIN_JOG_Z", "-1000"),
                 ("QIDI_MIN_JOG_Z", "1e9")]:
    st = s(**{var: raw})
    sa = st.safety
    print("  %-26s=%-8s -> jog=%-12s extrude=%-10s feedrate=%-10s min_jog_z=%s"
          % (var, raw, sa.max_jog_distance, sa.max_extrude_distance,
             sa.max_move_feedrate, sa.min_jog_z))
print("  (hardware max_velocity*60 = %.0f mm/min)" % (MAX4.max_velocity * 60.0))
