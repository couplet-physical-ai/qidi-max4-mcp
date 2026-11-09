"""L4b: broad randomised SINGLE-LINE fuzz of our screen vs the real Klipper parser.

Single-line only (no \\n), because that is the contract both live call sites
honour, so any divergence found here is a genuine, reachable bypass.
"""
import itertools
import os
import random
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "audit", "scripts"))

from _realparser import real_command
from qidi_mcp.limits import DANGEROUS_GCODE_COMMANDS, klipper_command_name, screen_gcode

random.seed(1234)

CMDS = sorted(DANGEROUS_GCODE_COMMANDS) + ["M105", "G28", "G1", "M104", "BED_MESH_CALIBRATE"]

PREFIXES = ["", " ", "  ", "\t", "\t\t", " \t", "N1 ", "N1", "N 1 ", "N0 ", "N99999 ",
            "n1 ", "N1\t", "N-1 ", "N1.5 ", "N1;", "*", "*1 ", "\xa0", "\ufeff",
            "\u200b", "\x00", "\x1b", "M110 ", "N1 N2 ", ";", "; ", " ;", "(", ")"]
SUFFIXES = ["", " ", "\t", "\r", " S1", "S1", " EXTRA=1", "=1", ";c", " ;c", "*12",
            " *12", "*", " *", "\x00", "\xa0", "\u200b", ".1", ".0", " T0",
            " HEATER=extruder", "_X", "X", "1", " 1"]
CASES = [str, str.lower, str.upper, str.title,
         lambda s: "".join(random.choice([c.lower(), c.upper()]) for c in s)]
UNICODE_SUBS = [("I", "\u0131"), ("S", "\u017f"), ("K", "\u212a"), ("A", "\u0430"),
                ("E", "\u0435"), ("O", "\u043e"), ("C", "\u0441"), ("F", "\ufb00"),
                ("M", "\u216f"), ("D", "\u216e"), ("V", "\u2164"), ("X", "\u2169")]

divergences = []
bypasses = []
checked = 0


def check(line):
    global checked
    if "\n" in line:
        return
    checked += 1
    ours = klipper_command_name(line)
    real = real_command(line)
    scr = screen_gcode(line)
    real_d = real in DANGEROUS_GCODE_COMMANDS or real.split(".", 1)[0] in DANGEROUS_GCODE_COMMANDS
    if ours != real:
        divergences.append((line, ours, real, scr.is_dangerous, real_d))
    if real_d and not scr.is_dangerous:
        bypasses.append((line, ours, real))


# 1. exhaustive-ish structured sweep
for cmd in CMDS:
    for pre, suf, case in itertools.product(PREFIXES, SUFFIXES, CASES):
        check(pre + case(cmd) + suf)

# 2. unicode homoglyph / case-folding substitutions
for cmd in CMDS:
    for a, b in UNICODE_SUBS:
        if a in cmd:
            check(cmd.replace(a, b))
            check(cmd.replace(a, b, 1))
            check("N1 " + cmd.replace(a, b))
    for a, b in UNICODE_SUBS:
        check(cmd.lower().replace(a.lower(), b))

# 3. random mutation fuzz
ALPHABET = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789 \t;*=.-\r\x00") + \
           ["\xa0", "\u200b", "\ufeff", "\u0131", "\u017f", "\u212a", "\u0430"]
for _ in range(60000):
    cmd = random.choice(CMDS)
    s = list(cmd)
    for _ in range(random.randint(1, 4)):
        op = random.randint(0, 3)
        if op == 0:
            s.insert(random.randint(0, len(s)), random.choice(ALPHABET))
        elif op == 1 and len(s) > 1:
            del s[random.randrange(len(s))]
        elif op == 2 and s:
            s[random.randrange(len(s))] = random.choice(ALPHABET)
        else:
            s = list(random.choice(PREFIXES)) + s + list(random.choice(SUFFIXES))
    check("".join(s))

print("inputs compared against the real parser: %d" % checked)
print()
print("=" * 104)
print("NAME DIVERGENCES (ours != Klipper's dispatched command)")
print("=" * 104)
uniq = {}
for line, ours, real, ours_d, real_d in divergences:
    uniq.setdefault((ours, real, ours_d, real_d), []).append(line)
if not uniq:
    print("  none")
for (ours, real, ours_d, real_d), lines in sorted(uniq.items(), key=lambda kv: -len(kv[1]))[:40]:
    print("  ours=%-24r real=%-24r danger ours/real=%s/%s   n=%-5d e.g. %r"
          % (ours, real, ours_d, real_d, len(lines), lines[0]))
print()
print("total name divergences: %d (%d distinct shapes)" % (len(divergences), len(uniq)))
print()
print("=" * 104)
print("SAFETY BYPASSES (Klipper dispatches a deny-listed command; our screen said NOT dangerous)")
print("=" * 104)
if not bypasses:
    print("  NONE FOUND")
for line, ours, real in bypasses[:40]:
    print("  %r -> ours=%r  klipper=%r" % (line, ours, real))
print()
print("total bypasses: %d" % len(bypasses))
