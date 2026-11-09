"""L4: differential test — our screen vs a fetched pinned Klipper parser.

Run bash scripts/fetch_upstream_sources.sh before using the oracle, instantiated and driven
(see _realparser.py). We compare the command name our screen resolves against the
command name Klipper actually dispatches, over many spellings of the same command.

A divergence matters only in one direction for safety: if Klipper runs a
dangerous command that our screen did NOT name as dangerous, that is a bypass.
"""
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(__file__))

from _realparser import real_command, dispatched_commands
from qidi_mcp.limits import DANGEROUS_GCODE_COMMANDS, klipper_command_name, screen_gcode

# Spellings applied to a dangerous target command.
TARGETS = ["SAVE_CONFIG", "FORCE_MOVE", "M502", "SET_KINEMATIC_POSITION", "PID_CALIBRATE"]


def variants(cmd):
    yield cmd
    yield cmd.lower()
    yield cmd.title()
    yield "  " + cmd
    yield cmd + "  "
    yield "\t" + cmd
    yield cmd + "\t"
    yield "\r" + cmd
    yield cmd + "\r"
    yield cmd + "\n"
    yield "\n" + cmd
    yield " \t " + cmd + " \t "
    yield "N1 " + cmd
    yield "N1" + cmd
    yield "N 1 " + cmd
    yield "N12345 " + cmd
    yield "n1 " + cmd
    yield "N1\t" + cmd
    yield "N1  " + cmd
    yield "N-1 " + cmd
    yield "N1.5 " + cmd
    yield cmd + " ; comment"
    yield cmd + ";comment"
    yield ";" + cmd
    yield "; " + cmd
    yield cmd + " *123"
    yield cmd + "*123"
    yield "N1 " + cmd + "*123"
    yield "*" + cmd
    yield cmd + " EXTRA=1"
    yield cmd + "S1"
    yield "M105\n" + cmd            # multi-line: 2nd line dangerous
    yield cmd + "\nM105"
    yield "M105\r" + cmd            # CR-separated
    yield "M105\r\n" + cmd
    yield cmd + "\x00"
    yield "\x00" + cmd
    yield cmd + "\x0b"              # vertical tab: str.splitlines splits, Klipper does not
    yield cmd + "\x0c"              # form feed: ditto
    yield cmd + "\x85"              # NEL: ditto
    yield cmd + "\u2028"            # LINE SEPARATOR: ditto
    yield cmd + "\u2029"
    yield cmd + "\xa0"              # NBSP
    yield "\xa0" + cmd
    # non-ASCII that changes under .upper()
    yield cmd.replace("A", "\u0430") if "A" in cmd else cmd   # Cyrillic a
    yield cmd.lower().replace("i", "\u0131")                  # dotless i -> I under upper()
    yield cmd.lower().replace("s", "\u017f")                  # long s -> S under upper()
    yield cmd.lower().replace("k", "\u212a")                  # Kelvin sign -> K
    yield "\ufeff" + cmd                                       # BOM
    yield cmd + "\u200b"                                       # zero-width space
    yield "\u0131" + cmd
    yield "ff" + cmd                                           # ligature -> FF
    yield cmd.replace("O", "\u0131") if "O" in cmd else cmd


rows = []
for target in TARGETS:
    for v in variants(target):
        ours_name = klipper_command_name(v)
        # Match run_gcode's production behavior: it splits and screens every
        # nonempty line before sending any of them. Screening only the first
        # command here previously mislabeled multi-line input as a bypass.
        effective_dangerous = any(
            screen_gcode(line.strip()).is_dangerous for line in v.splitlines() if line.strip()
        )
        # Klipper may dispatch MULTIPLE commands for one input (embedded newline).
        real_list = dispatched_commands(v)
        real = real_list[0] if real_list else ""
        # Does Klipper run anything dangerous?
        real_dangerous = any(
            (c in DANGEROUS_GCODE_COMMANDS or c.split(".", 1)[0] in DANGEROUS_GCODE_COMMANDS)
            for c in real_list
        )
        agree_name = ours_name == real
        if agree_name and effective_dangerous == real_dangerous:
            continue
        rows.append((v, ours_name, real_list, effective_dangerous, real_dangerous, agree_name))

print("=" * 100)
print("DIVERGENCES (our resolved name != Klipper's, or danger classification differs)")
print("=" * 100)
print("%-30s %-22s %-28s %s" % ("INPUT", "OURS", "KLIPPER DISPATCHES", "DANGER ours/real"))
print("-" * 100)
bypasses = []
for v, ours, real_list, ours_d, real_d, agree in rows:
    mark = ""
    if real_d and not ours_d:
        mark = "  <== BYPASS"
        bypasses.append((v, ours, real_list))
    print("%-30r %-22r %-28r %s/%s%s" % (v, ours, real_list, ours_d, real_d, mark))

print()
print("total divergences: %d" % len(rows))
print("SAFETY BYPASSES (Klipper runs something dangerous, run_gcode screening said safe): %d" % len(bypasses))
for v, ours, real_list in bypasses:
    print("  %r -> ours=%r  klipper=%r" % (v, ours, real_list))
