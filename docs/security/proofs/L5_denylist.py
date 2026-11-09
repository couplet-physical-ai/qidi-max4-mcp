"""L5: completeness of DANGEROUS_GCODE_COMMANDS.

(a) Extract every command from the optionally fetched upstream G-Codes.md and report
    the ones in a dangerous category that are missing from the deny-list.
(b) Extract every optionally fetched QIDI vendor macro name and report
    which ones perform a dangerous operation in their body -- i.e. an innocuous
    name that reaches a restricted primitive.
    A macro counts as COVERED when it is on the deny-list, or when its parameters
    are range-checked by the shared raw-G-code screen (HEATER_COMMANDS /
    LIMIT_COMMANDS) -- e.g. M203 reaches SET_VELOCITY_LIMIT, but only with the
    caller's X/Y, which the screen bounds by the velocity ceiling.
(c) Confirm how the matcher actually matches (exact vs prefix).
(d) Informational: macros that reach a restricted primitive only THROUGH other
    macros. Not counted: the inner macro is what gets screened when called
    directly, and the outer one (e.g. PRINT_START) is a residual limit
    documented in docs/security/README.md.
"""
import glob
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from qidi_mcp.limits import (  # noqa: E402
    DANGEROUS_GCODE_COMMANDS,
    HEATER_COMMANDS,
    LIMIT_COMMANDS,
    screen_gcode,
)

GCODES_MD = ROOT / "research/upstream/G-Codes.md"
MACRO_GLOB = str(ROOT / "research/upstream/qidi-max4/config/klipper-macros-qd/*.cfg")
if not GCODES_MD.is_file() or not glob.glob(MACRO_GLOB):
    raise SystemExit("Optional GPLv3 sources absent; run bash scripts/fetch_upstream_sources.sh first")

# --- (a) documented commands ------------------------------------------------
text = GCODES_MD.read_text(encoding="utf-8")
# Klipper's G-Codes.md documents extended commands as `#### COMMAND_NAME` and
# traditional ones inside backticks like `M104`.
documented = set(re.findall(r"^#+\s*`?([A-Z][A-Z0-9_]{1,40})`?\s*$", text, re.M))
documented |= set(re.findall(r"`([A-Z][A-Z0-9_]{2,40})[ `]", text))
documented = {d for d in documented if not d.startswith("_")}

# Categories the brief asks about, matched by name.
CATEGORIES = {
    "writes persistent config": [
        "SAVE_CONFIG", "SAVE_VARIABLE", "SAVE_AT_END", "BED_MESH_PROFILE",
        "Z_OFFSET_APPLY_PROBE", "Z_OFFSET_APPLY_ENDSTOP", "SAVE_GCODE_STATE",
        "ENDSTOP_PHASE_CALIBRATE", "DELTA_CALIBRATE", "SAVE_SPEED",
    ],
    "changes motor currents": [
        "SET_TMC_CURRENT", "SET_TMC_FIELD", "INIT_TMC", "DUMP_TMC",
    ],
    "disables endstops / limits / steppers": [
        "SET_KINEMATIC_POSITION", "FORCE_MOVE", "SET_STEPPER_ENABLE",
        "STEPPER_BUZZ", "SET_VELOCITY_LIMIT", "M84", "M18", "M211", "M120", "M121",
    ],
    "starts unattended heating": [
        "PID_CALIBRATE", "MPC_CALIBRATE", "HEATER_INTERLOCK", "SET_HEATER_TEMPERATURE",
        "TEMPERATURE_WAIT", "M104", "M109", "M140", "M190", "M141", "M191", "M303",
    ],
    "long unattended motion / calibration": [
        "SHAPER_CALIBRATE", "RESONANCE_TEST", "TEST_RESONANCES", "ACCELEROMETER_MEASURE",
        "PROBE_CALIBRATE", "PROBE_ACCURACY", "MANUAL_PROBE", "BED_MESH_CALIBRATE",
        "Z_TILT_ADJUST", "QUAD_GANTRY_LEVEL", "SCREWS_TILT_CALCULATE", "TESTZ",
        "BED_SCREWS_ADJUST", "PROBE", "G29", "G32", "G34",
    ],
}


def is_denied(cmd):
    return cmd in DANGEROUS_GCODE_COMMANDS or cmd.split(".", 1)[0] in DANGEROUS_GCODE_COMMANDS


def is_value_checked(cmd):
    return cmd in HEATER_COMMANDS or cmd in LIMIT_COMMANDS


print("=" * 96)
print("(a) DANGEROUS CATEGORIES vs the deny-list")
print("=" * 96)
for cat, cmds in CATEGORIES.items():
    print("\n%s:" % cat.upper())
    for c in cmds:
        doc = "documented" if c in documented else "not-in-md"
        denied = is_denied(c)
        scr = screen_gcode(c)
        mark = "" if denied else "   <== MISSING FROM DENY-LIST"
        print("    %-26s %-11s denied=%-5s screen_dangerous=%-5s%s"
              % (c, doc, denied, scr.is_dangerous, mark))

# --- (b) vendor macros -----------------------------------------------------
print()
print("=" * 96)
print("(b) VENDOR MACROS whose body reaches a restricted primitive")
print("=" * 96)

DANGEROUS_BODY = [
    "SAVE_CONFIG", "SAVE_CONFIG_QD", "SAVE_VARIABLE", "SET_TMC_CURRENT", "SET_TMC_FIELD",
    "INIT_TMC", "FORCE_MOVE", "SET_KINEMATIC_POSITION", "SET_STEPPER_ENABLE",
    "PID_CALIBRATE", "SET_HEATER_SCALING", "RESET_HEATER_SCALING", "HEATER_TEMP_LIMIT",
    "DISABLE_ALL_SENSOR", "SET_VELOCITY_LIMIT",
]

macros = {}   # name -> (file, body)
for path in sorted(glob.glob(MACRO_GLOB)):
    src = open(path, encoding="utf-8").read()
    blocks = re.split(r"^\[", src, flags=re.M)
    for b in blocks:
        m = re.match(r"gcode_macro\s+([^\]]+)\]", b)
        if not m:
            continue
        macros[m.group(1).strip().upper()] = (os.path.basename(path), b)

print("vendor macros found: %d" % len(macros))
print()
hits = []
for name, (fname, body) in sorted(macros.items()):
    body_up = body.upper()
    # strip comments and the macro's own header line
    found = []
    for prim in DANGEROUS_BODY:
        for line in body_up.splitlines()[1:]:
            code = line.split("#")[0]
            if re.search(r"(?<![A-Z_])" + prim + r"(?![A-Z_])", code):
                found.append(prim)
                break
    if found:
        how = "denied" if is_denied(name) else "checked" if is_value_checked(name) else None
        hits.append((name, fname, sorted(set(found)), how))

for name, fname, found, how in hits:
    mark = "" if how else "   <== macro NOT covered"
    print("  %-30s %-24s covered=%-8s calls: %s%s"
          % (name, fname, how or "NO", ", ".join(found), mark))

print()
print("macros that reach a restricted primitive: %d (denied: %d, value-checked: %d)"
      % (len(hits), sum(1 for h in hits if h[3] == "denied"),
         sum(1 for h in hits if h[3] == "checked")))
print("macros that reach a restricted primitive but are NOT covered (denied or value-checked): %d"
      % sum(1 for h in hits if not h[3]))

# --- (c) matcher semantics -------------------------------------------------
print()
print("=" * 96)
print("(c) MATCHER SEMANTICS: is 'PREFIXES' actually a prefix match?")
print("=" * 96)
for probe in ["SAVE_CONFIG", "SAVE_CONFIG_QD", "SAVE_CONFIGX", "SET_TMC", "SET_TMC_CURRENT",
              "RESET_HEATER_SCALING", "SET_HEATER_SCALING", "RESTART", "FIRMWARE_RESTART",
              "M502", "M5021", "SAVE_VARIABLE"]:
    scr = screen_gcode(probe)
    print("  screen_gcode(%-22r).is_dangerous = %-5s  matched=%r"
          % (probe, scr.is_dangerous, scr.matched))

# --- (d) transitive reach (informational) -----------------------------------
print()
print("=" * 96)
print("(d) INFORMATIONAL: macros reaching a restricted primitive only via other macros")
print("=" * 96)
direct = {h[0] for h in hits}
calls = {}
for name, (fname, body) in macros.items():
    code = "\n".join(line.split("#")[0] for line in body.upper().splitlines()[1:])
    # Only lines that INVOKE a macro count (its name is the first token, possibly
    # after a Jinja block). A Jinja read like printer["gcode_macro _km_globals"]
    # is not a call.
    invoked = set()
    for line in code.splitlines():
        line = re.sub(r"\{%.*?%\}", " ", line).strip()
        tok = re.match(r"([A-Z_][A-Z0-9_.]*)", line)
        if tok:
            invoked.add(tok.group(1))
    calls[name] = {m for m in macros if m != name and m in invoked}
reach = set(direct)
changed = True
while changed:
    changed = False
    for name, callees in calls.items():
        if name not in reach and callees & reach:
            reach.add(name)
            changed = True
indirect = sorted(reach - direct)
for name in indirect:
    how = "denied" if is_denied(name) else "checked" if is_value_checked(name) else "-"
    print("  %-30s %-24s covered=%-8s via: %s"
          % (name, macros[name][0], how, ", ".join(sorted(calls[name] & reach))))
print("macros reaching a restricted primitive only transitively: %d" % len(indirect))

# --- (e) parameter flow ------------------------------------------------------
# Naming a macro is not the only way past the screen: a macro PARAMETER can carry
# a value into a heater, limit or motion primitive. (b) looks at bodies for
# restricted command names and so missed EXTRUSION_AND_FLUSH HOTEND=374, which
# reaches M109 S{params.HOTEND}. This section follows every parameter through
# Jinja assignments, aliases (params.__setitem__), calls to other macros (by
# key, by "for k in params" forwarding and {rawparams}), and macro state written
# from parameters and read back later (PRINT_START_SET -> print.HOTEND), until it
# reaches a primitive. A flow is COVERED when the macro that receives it is
# deny-listed, or the parameter is value-checked for that macro by the shared
# screen (HEATER_COMMANDS, LIMIT_COMMANDS, MOTION_MACROS, OFFSET_COMMANDS), or
# the screen refuses that parameter outright (HEATER_ALLOWED_PARAMS).
#
# The deny-list alone is not enough for a value flow: QIDI_ALLOW_DANGEROUS_GCODE=1
# lets deny-listed commands through. So a flow is counted separately when the
# deny-list is ALL that covers it, unless the command is one the operator switch
# cannot unlock (ALWAYS_DENIED_GCODE_COMMANDS). That count must be 0.
from qidi_mcp.limits import (  # noqa: E402
    ALWAYS_DENIED_GCODE_COMMANDS,
    HEATER_ALLOWED_PARAMS,
    MOTION_COMMANDS,
    MOTION_MACROS,
    OFFSET_COMMANDS,
    table_key,
)

print()
print("=" * 96)
print("(e) PARAMETER FLOW: stock-macro parameters reaching a heater/limit/motion primitive")
print("=" * 96)

_XYZ = {"X", "Y", "Z"}
SINKS = {
    "SET_HEATER_TEMPERATURE": {"TARGET": "heater"},
    # Raw built-ins behind QIDI's wrappers (rename_existing).
    "M104.6245197": {"S": "heater"},
    "M109.6245197": {"S": "heater"},
    "M140.6245197": {"S": "heater"},
    "M190.6245197": {"S": "heater"},
    # Box firmware commands: bodies unpublished, so their temperatures are sinks.
    "BOX_PRINT_START": {"HOTENDTEMP": "heater"},
    "E_LOAD": {"S": "heater"},
    "SET_VELOCITY_LIMIT": {k: "limit" for k in (
        "VELOCITY", "ACCEL", "ACCEL_TO_DECEL", "SQUARE_CORNER_VELOCITY",
        "MINIMUM_CRUISE_RATIO")},
    "M204": {"S": "limit", "P": "limit", "T": "limit"},
    "SET_GCODE_OFFSET": {k: "offset" for k in (
        "X", "Y", "Z", "X_ADJUST", "Y_ADJUST", "Z_ADJUST")},
    **{g: {a: "motion" for a in _XYZ} for g in ("G0", "G1", "G2", "G3")},
}


def _balanced(text, start):
    """Index just past the '}' closing the '{' at text[start]."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _gcode_body(block):
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*gcode\s*:", line):
            return "\n".join([line.split(":", 1)[1]] + lines[i + 1:])
    return ""


def _strip_comments(text):
    out, depth = [], 0
    for line in text.split("\n"):
        keep = []
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
            elif ch == "#" and depth == 0:
                break
            keep.append(ch)
        out.append("".join(keep))
    return "\n".join(out)


def _join_braces(text):
    """Put each multi-line {...} / {%...%} on one line."""
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        out.append(" " if ch == "\n" and depth else ch)
    return "".join(out)


STATE_RE = re.compile(r"""^printer\[["']gcode_macro (\w+)["']\]\.(\w+)$""")


class Macro:
    def __init__(self, name, block):
        self.name = name
        body = _join_braces(_strip_comments(_gcode_body(block)))
        self.local = {}      # var -> set of (owner, param)
        self.handles = {}    # var -> store macro (state handle)
        self.alias = {}      # param -> set of (owner, param)
        self.state_taint = set()
        self.store_all = bool(re.search(r"__setitem__\([^,]+,\s*params\[", body))
        stmts = re.findall(r"\{%-?\s*(.*?)\s*-?%\}", body)
        for _ in range(3):   # assignments may refer forward through loops
            for st in stmts:
                m = re.match(r"set\s+(\w+)\s*=\s*(.*)$", st, re.S)
                if not m:
                    continue
                var, expr = m.group(1), m.group(2).strip()
                sm = STATE_RE.match(expr)
                if sm:
                    self.handles[var] = sm.group(1).upper()
                    continue
                taint = self.taint(expr)
                for am in re.finditer(r"params\.__setitem__\(\s*['\"](\w+)['\"]\s*,(.*)\)", expr):
                    self.alias.setdefault(am.group(1).upper(), set()).update(
                        self.taint(am.group(2)))
                if re.search(r"\.(append|__setitem__)\(", expr) and "params.__setitem__" not in expr:
                    self.state_taint |= taint
                elif var != "dummy":
                    self.local[var] = self.local.get(var, set()) | taint
        # Forwarding loops and {rawparams} become a marker; other statements go.
        def fwd(m):
            flt = m.group(1) or ""
            sel = re.search(r"select\(\s*['\"]in['\"]\s*,\s*['\"](\w+)['\"]", flt)
            rej = re.search(r"reject\(\s*['\"]in['\"]\s*,\s*['\"](\w+)['\"]", flt)
            return " @FWD[%s|%s]@ " % (sel.group(1) if sel else "",
                                        rej.group(1) if rej else "")
        body = re.sub(r"\{%-?\s*for\s+\w+\s+in\s+params\s*(\|[^%]*)?-?%\}.*?\{%-?\s*endfor\s*-?%\}",
                      fwd, body, flags=re.S)
        body = body.replace("{rawparams}", " @FWD[|]@ ")
        body = re.sub(r"\{%.*?%\}", "", body, flags=re.S)
        self.lines = [ln.strip() for ln in body.split("\n") if ln.strip()]

    def taint(self, expr):
        out = set()
        for p in re.findall(r"params\.(\w+)", expr):
            if p != "__setitem__":
                out.add((self.name, p.upper()))
        for p in re.findall(r"params\[\s*['\"](\w+)['\"]\s*\]", expr):
            out.add((self.name, p.upper()))
        for var, attr in re.findall(r"(?<![.\w])(\w+)\.(\w+)", expr):
            if var in self.handles:
                out.add((self.handles[var], attr.upper()))
        for tok in re.findall(r"(?<![.\w'\"])([A-Za-z_]\w*)", expr):
            if tok in self.local:
                out |= self.local[tok]
        for owner, p in list(out):
            if owner == self.name and p in self.alias:
                out |= self.alias[p]
        return out

    def calls(self):
        """Yield (callee, {key: taint}, [forward filters]) per command line."""
        for line in self.lines:
            if line.startswith("@FWD"):
                continue
            m = re.match(r"(\S+)", line)
            name, rest = m.group(1), line[m.end():]
            if "{" in name:
                at = line.index("{")
                inner = line[at:_balanced(line, at)]
                if inner.lstrip("{ ").startswith("action"):
                    continue
                taint = self.taint(inner) or self.state_taint
                yield ("<dynamic command>", {"*": taint}, [])
                continue
            keyed, i = {}, 0
            while i < len(rest):
                if rest[i] == "{":
                    end = _balanced(rest, i)
                    before = rest[:i]
                    km = (re.search(r"(\w+)=[\"']?$", before)
                          or re.search(r"(?<![A-Za-z_])([A-Za-z])$", before))
                    if km:
                        keyed.setdefault(km.group(1).upper(), set()).update(
                            self.taint(rest[i:end]))
                    i = end
                else:
                    i += 1
            fwds = re.findall(r"@FWD\[(\w*)\|(\w*)\]@", rest)
            yield (name.upper(), keyed, fwds)


parsed = {name: Macro(name, body) for name, (_, body) in macros.items()}

# flows[(owner, param)] = {kind: path}; owner is the macro the agent would call.
flows = {}


def add(owner, param, kind, path):
    if kind not in flows.setdefault((owner, param), {}):
        flows[(owner, param)][kind] = path
        return True
    return False


changed = True
while changed:
    changed = False
    for mac in parsed.values():
        for callee, keyed, fwds in mac.calls():
            if callee == "<dynamic command>":
                for owner, p in keyed["*"]:
                    changed |= add(owner, p, "command", f"{mac.name}: command built from it")
                continue
            sink = SINKS.get(callee) if callee not in parsed else None
            for key, taint in keyed.items():
                kinds = {}
                if sink and key in sink:
                    kinds[sink[key]] = f"{mac.name} -> {callee} {key}"
                if callee in parsed:
                    for kind, path in flows.get((callee, key), {}).items():
                        kinds[kind] = f"{mac.name} -> {path}"
                for owner, p in taint:
                    for kind, path in kinds.items():
                        if owner != mac.name:   # value stored by owner, read back here
                            path = f"{owner} state {p} read by {path}"
                        changed |= add(owner, p, kind, path)
            for sel, rej in fwds:
                if callee not in parsed:
                    continue
                for (owner, p), kinds in list(flows.items()):
                    if owner != callee or (sel and p not in sel) or (rej and p in rej):
                        continue
                    for kind, path in list(kinds.items()):
                        changed |= add(mac.name, p, kind, f"{mac.name} =forward=> {path}")
                        for o2, p2 in mac.alias.get(p, ()):
                            changed |= add(o2, p2, kind, f"{mac.name} =forward=> {path}")


def value_cover(owner, param, kind):
    key = table_key(owner)
    allowed = HEATER_ALLOWED_PARAMS.get(key)
    if allowed is not None and (param not in allowed or param == "M"):
        return "param refused" if param != "M" else "M pinned to own number"
    # HEATER / T only choose WHICH heater; the screen reads them to pick the
    # ceiling it checks the temperature against (unknown heaters are refused).
    if kind == "heater" and param in ("HEATER", "T") and key in HEATER_COMMANDS:
        return "selects checked heater"
    checked = {
        "heater": HEATER_COMMANDS.get(key, {}),
        "limit": LIMIT_COMMANDS.get(key, {}),
        "motion": MOTION_MACROS.get(key, ("X", "Y", "Z") if key in MOTION_COMMANDS else ()),
        "offset": OFFSET_COMMANDS.get(key, ()),
    }.get(kind, ())
    return "checked" if param in checked else None


def flow_cover(owner, param, kind):
    how = value_cover(owner, param, kind)
    if how is not None:
        return how + ("+denied" if is_denied(owner) else "")
    if table_key(owner) in ALWAYS_DENIED_GCODE_COMMANDS:
        return "always denied"
    if is_denied(owner):
        return "denied"
    return None


uncovered = 0
deny_alone = 0
for (owner, param), kinds in sorted(flows.items()):
    if owner not in parsed:
        continue
    for kind, path in sorted(kinds.items()):
        how = flow_cover(owner, param, kind)
        uncovered += how is None
        deny_alone += how == "denied"
        print("  %-36s %-8s covered=%-24s via %s%s"
              % (f"{owner}.{param}", kind, how or "NO", path,
                 "   <== NOT covered" if how is None else
                 "   <== deny-list alone (operator switch reopens)" if how == "denied" else ""))
print()
print("parameter flows found: %d" % sum(len(k) for (o, _), k in flows.items() if o in parsed))
print("parameter flows NOT covered: %d" % uncovered)
print("parameter flows covered by the deny-list alone: %d" % deny_alone)

# --- (e2) forwarded parameter NAMES ---------------------------------------------
# A forwarding loop re-renders each parameter as "%s=..." (or '~k~'=...): the
# values are quoted, the NAMES are not. Klipper keeps a quoted name verbatim, so
# PRINT_START HOTEND=200 " HOTEND"=374 reached _print_start_phase_init as the key
# " HOTEND" (which no table names) and was re-rendered as HOTEND="374", the last
# HOTEND winning. Every such loop is safe only if the screen refuses any name
# that is not [A-Z0-9_]+; that is probed here against the shared parser.
print()
print("=" * 96)
print("(e2) FORWARDED PARAMETER NAMES (re-rendered unescaped by 'for k in params')")
print("=" * 96)
from qidi_mcp.limits import klipper_extended_params  # noqa: E402

PROBES = ['PRINT_START HOTEND=200 " HOTEND"=374', 'PRINT_START "\tHOTEND"=374',
          "PRINT_START 'HOTEND '=374", 'PRINT_START "HOT END"=374']
validated = all(klipper_extended_params(p) is None for p in PROBES)
loops = []
for name, (fname, body) in sorted(macros.items()):
    for m in re.finditer(r"for\s+(\w+)\s+in\s+params", _gcode_body(body)):
        loops.append((name, fname, m.group(1)))
for name, fname, var in loops:
    print("  %-30s %-24s renders key %r: validated=%s" % (name, fname, var, validated))
print("forwarding loops: %d" % len(loops))
print("forwarded parameter names NOT validated: %d" % (0 if validated else len(loops)))

# --- (f) rename_existing targets ----------------------------------------------
print()
print("=" * 96)
print("(f) rename_existing TARGETS (raw built-ins behind a vendor wrapper)")
print("=" * 96)
targets = set()
for path in sorted(glob.glob(MACRO_GLOB)):
    for line in open(path, encoding="utf-8"):
        m = re.match(r"\s*rename_existing\s*:\s*(\S+)", line)
        if m:
            targets.add(m.group(1).upper())
for t in sorted(targets):
    print("  %-26s denied=%s" % (t, is_denied(t)))
print("rename_existing targets: %d, NOT denied: %d"
      % (len(targets), sum(1 for t in targets if not is_denied(t))))
