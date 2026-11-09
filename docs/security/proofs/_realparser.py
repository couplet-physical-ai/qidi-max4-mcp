"""Drive an optionally fetched Klipper GCodeDispatch as the command oracle.

Run bash scripts/fetch_upstream_sources.sh from the repository root first. The
pinned upstream parser lives outside the shipped Apache-2.0 source tree.
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_SRC = _ROOT / "research" / "upstream" / "klipper_gcode_parser.py"
if not _SRC.is_file():
    raise SystemExit("Optional Klipper GPLv3 parser absent; run bash scripts/fetch_upstream_sources.sh first")
_spec = importlib.util.spec_from_file_location("klipper_gcode", _SRC)
klipper_gcode = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(klipper_gcode)


class _Mutex:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Reactor:
    def mutex(self):
        return _Mutex()

    def register_callback(self, *a, **k):
        pass

    def register_fd(self, *a, **k):
        return None

    NOW = 0.0
    NEVER = float("inf")


class _StubPrinter:
    config_error = klipper_gcode.CommandError

    def __init__(self):
        self.objects = {}

    def register_event_handler(self, *a, **k):
        pass

    def get_start_args(self):
        return {}

    def get_reactor(self):
        return _Reactor()

    def load_object(self, *a, **k):
        return None

    def lookup_object(self, name, default=None):
        return default

    def add_object(self, name, obj):
        self.objects[name] = obj

    def send_event(self, *a, **k):
        return []

    def get_state_message(self):
        return ("ready", "ready")

    def is_shutdown(self):
        return False

    def invoke_shutdown(self, msg):
        raise AssertionError("shutdown invoked: %s" % (msg,))


def make_dispatch():
    printer = _StubPrinter()
    gd = klipper_gcode.GCodeDispatch(printer)
    gd.is_printer_ready = True
    gd.gcode_handlers = gd.ready_gcode_handlers
    return gd


def dispatched_commands(line, registered=()):
    """Return the list of command names the REAL parser dispatches for `line`.

    `registered` is a set of command names to pretend the firmware knows about;
    resolution of the command name happens before handler lookup, so this only
    affects whether the traditional/extended path is taken.
    """
    gd = make_dispatch()
    seen = []

    def record(gcmd):
        seen.append(gcmd.get_command())

    gd.cmd_default = record
    # GCodeDispatch pre-registers real handlers (M110, M112, M115, RESTART,
    # FIRMWARE_RESTART, ECHO, STATUS, HELP) that would actually try to act on a
    # printer we do not have. We only care which *name* was resolved, so replace
    # every handler with the recorder. Name resolution happens in
    # _process_commands before handler lookup, so this changes nothing we measure.
    for table in (gd.ready_gcode_handlers, gd.base_gcode_handlers):
        for cmd in list(table):
            table[cmd] = record
    gd.gcode_handlers = gd.ready_gcode_handlers
    for cmd in registered:
        try:
            gd.register_command(cmd, record, True)
        except Exception:
            pass
    # Klipper reads from the serial/api as data, splitting on newlines. Mirror
    # _process_data's split so an embedded newline behaves as the firmware does.
    gd._process_commands(str(line).split("\n"), need_ack=False)
    return seen


def real_command(line):
    """The single command name Klipper resolves, for single-line input."""
    cmds = dispatched_commands(line)
    return cmds[0] if cmds else ""
