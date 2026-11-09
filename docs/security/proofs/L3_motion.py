"""L3: motion bounds vs real axis ranges. Off-by-one / sign errors at the negative minimums."""
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "audit", "scripts"))
from _h import mk
from qidi_mcp.errors import QidiError
from qidi_mcp.limits import MAX4

# Ground truth from QIDITECH/QIDI_MAX4 config/printer.cfg at 47fc14c (fetch via bash scripts/fetch_upstream_sources.sh)
TRUTH = {"x": (-2.0, 392.0), "y": (-5.0, 410.0), "z": (-5.0, 342.0)}

print("--- profile vs printer.cfg ---")
for ax in "xyz":
    got = getattr(MAX4, ax)
    exp = TRUTH[ax]
    ok = (got.min, got.max) == exp
    print("  %s: profile=(%s, %s)  printer.cfg=%s  %s"
          % (ax.upper(), got.min, got.max, exp, "MATCH" if ok else "*** MISMATCH ***"))

p = mk()
print()
print("--- check_position boundaries (default min_jog_z=%s) ---" % p.settings.safety.min_jog_z)


def probe(ax, v):
    try:
        p.check_position(ax, v)
        return "allowed"
    except QidiError as e:
        return "refused"


for ax in "xyz":
    lo, hi = TRUTH[ax]
    for v, expect in [(lo - 0.001, "refused"), (lo, "allowed"), (lo + 0.001, "allowed"),
                      (0.0, "allowed"), (hi - 0.001, "allowed"), (hi, "allowed"),
                      (hi + 0.001, "refused")]:
        r = probe(ax, v)
        # Z has an extra floor from min_jog_z, so expectations differ below 0
        note = ""
        if ax == "z" and v < p.settings.safety.min_jog_z:
            expect = "refused"
            note = " (min_jog_z floor)"
        bad = "   <== UNEXPECTED (wanted %s)" % expect if r != expect else ""
        print("  check_position(%s, %-10s) -> %-8s%s%s" % (ax, v, r, note, bad))

print()
print("--- the negative minimums specifically: is the sign handled? ---")
for ax in "xy":
    lo = TRUTH[ax][0]
    for v in [lo, lo - 0.0001, lo - 1, abs(lo), -abs(lo)]:
        print("  check_position(%s, %-8s) -> %s" % (ax, v, probe(ax, v)))

print()
print("--- Z floor: can the nozzle be driven below the bed? ---")
for mz in ["0", "-5", "-4.999", "-1000"]:
    q = mk(QIDI_MIN_JOG_Z=mz)
    floor_effective = max(MAX4.z.min, q.settings.safety.min_jog_z)
    results = []
    for v in [-5.0, -4.0, -0.001, 0.0]:
        try:
            q.check_position("z", v)
            results.append("%s:allowed" % v)
        except QidiError:
            results.append("%s:refused" % v)
    print("  MIN_JOG_Z=%-8s -> effective floor=%-8s  %s" % (mz, floor_effective, "  ".join(results)))

print()
print("--- check_jog cap vs destination check (does a huge jog get caught?) ---")
q = mk(QIDI_MAX_JOG_DISTANCE="1e9")
print("  MAX_JOG_DISTANCE=1e9 -> cap=%s" % q.settings.safety.max_jog_distance)
for d in [1e9, -1e9, 100.0]:
    try:
        q.check_jog("x", d)
        r1 = "jog allowed"
    except QidiError:
        r1 = "jog refused"
    # move_relative then checks the destination from current position 100.0
    dest = 100.0 + d
    try:
        q.check_position("x", dest)
        r2 = "destination %s allowed" % dest
    except QidiError:
        r2 = "destination %s refused" % dest
    print("    d=%-12s -> %-12s ; %s" % (d, r1, r2))

print()
print("--- bed_mesh profile vs printer.cfg ---")
print("  profile bed_mesh_min=%s bed_mesh_max=%s" % (MAX4.bed_mesh_min, MAX4.bed_mesh_max))
