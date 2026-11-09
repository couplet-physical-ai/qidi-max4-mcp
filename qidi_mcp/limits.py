"""Hardware envelope for the QIDI Max4, and the safety envelope layered on top.

Two distinct concepts live here:

*   ``HARDWARE`` — what the machine physically is, transcribed from QIDI's own
    published ``printer.cfg`` for the Max4
    (https://github.com/QIDITECH/QIDI_MAX4 → ``config/printer.cfg``).
*   ``DEFAULT_SAFETY`` — the *narrower* envelope this MCP enforces by default.

The safety envelope is deliberately tighter than the hardware envelope. The
firmware's own ``heater_temp_limit`` macro caps extruder 370 °C / bed 120 °C /
chamber 65 °C; we default lower still and require explicit confirmation to go
near the top. Nothing here can *raise* a limit past the hardware maximum: the
policy layer clamps every configured value into the hardware range, so a
mis-set env var loosens nothing that the firmware would refuse anyway.

At runtime the real limits are re-derived from the connected printer's own
``configfile.settings`` (see ``capabilities.py``); these constants are the
fallback used before the first successful query and the hard ceiling that
runtime discovery is clamped against.
"""

from __future__ import annotations

import re
import shlex
import unicodedata
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class AxisLimits:
    """Per-axis travel in mm, from the printer.cfg stepper sections."""

    min: float
    max: float

    def clamp(self, value: float) -> float:
        return max(self.min, min(self.max, value))

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max


@dataclass(frozen=True)
class HeaterLimits:
    """Temperature envelope for one heater, in °C."""

    min: float
    max: float
    #: Above this, a temperature change is treated as high-risk and needs confirmation.
    confirm_above: float

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max


@dataclass(frozen=True)
class HardwareProfile:
    """Physical description of the machine. Never widened at runtime."""

    model: str

    # Build volume. QIDI advertise 385x405x342; printer.cfg position_max is
    # 392 / 410 / 342 (the extra travel is off-bed service area).
    x: AxisLimits
    y: AxisLimits
    z: AxisLimits

    # Absolute heater ceilings from printer.cfg [extruder]/[heater_bed]/[heater_generic chamber].
    extruder_max_temp: float
    bed_max_temp: float
    chamber_max_temp: float
    #: Klipper refuses extrusion below this ([extruder] min_extrude_temp).
    min_extrude_temp: float

    kinematics: str
    max_velocity: float
    max_accel: float
    max_z_velocity: float
    max_z_accel: float
    #: [printer] square_corner_velocity, mm/s.
    square_corner_velocity: float

    bed_mesh_min: tuple[float, float]
    bed_mesh_max: tuple[float, float]

    #: Fans addressable by SET_FAN_SPEED FAN=<name>, from printer.cfg.
    fans: tuple[str, ...]
    #: [output_pin] sections — SET_PIN PIN=<name>.
    output_pins: tuple[str, ...]
    #: Non-standard heaters: name -> Klipper status object key.
    generic_heaters: dict[str, str]
    #: [neopixel]/[led] sections.
    leds: tuple[str, ...]


#: The QIDI Max4, transcribed from QIDITECH/QIDI_MAX4 config/printer.cfg.
MAX4 = HardwareProfile(
    model="QIDI Max4",
    x=AxisLimits(-2.0, 392.0),
    y=AxisLimits(-5.0, 410.0),
    z=AxisLimits(-5.0, 342.0),
    extruder_max_temp=375.0,      # [extruder] max_temp
    bed_max_temp=125.0,           # [heater_bed] max_temp
    chamber_max_temp=68.0,        # [heater_generic chamber] max_temp
    min_extrude_temp=135.0,       # [extruder] min_extrude_temp
    kinematics="corexy",
    max_velocity=800.0,
    max_accel=30000.0,
    max_z_velocity=10.0,
    max_z_accel=500.0,
    square_corner_velocity=8.0,   # [printer] square_corner_velocity
    bed_mesh_min=(15.0, 15.0),
    bed_mesh_max=(375.0, 375.0),
    fans=(
        "cooling_fan",                # part cooling ([fan_generic])
        "auxiliary_cooling_fan",
        "auxiliary_cooling_fan2",
        "chamber_circulation_fan",
    ),
    output_pins=("caselight", "beeper", "Polar_cooler"),
    generic_heaters={"chamber": "heater_generic chamber"},
    leds=("RGB",),
)


@dataclass(frozen=True)
class SafetyEnvelope:
    """The limits this MCP enforces. Always <= the hardware profile."""

    extruder: HeaterLimits
    bed: HeaterLimits
    chamber: HeaterLimits

    #: Z below this is refused for jog moves — protects the nozzle/bed.
    min_jog_z: float
    #: Largest single relative jog, mm.
    max_jog_distance: float
    #: Largest manual extrude/retract in one call, mm.
    max_extrude_distance: float
    #: Feedrate ceiling for MCP-issued moves, mm/min.
    max_move_feedrate: float

    #: Ceilings for the kinematic limits raw G-code can change at runtime
    #: (M201/M203/M204/M205). Klipper lets those commands raise the toolhead's
    #: limits *above* what printer.cfg configured, so the envelope caps them. The
    #: defaults are the printer.cfg values themselves: an agent may lower the
    #: limits, never raise them past what the machine was set up for.
    #: Velocity ceiling, mm/s ([printer] max_velocity).
    max_velocity: float = 800.0
    #: Acceleration ceiling, mm/s² ([printer] max_accel).
    max_accel: float = 30000.0
    #: Square-corner-velocity ceiling, mm/s ([printer] square_corner_velocity).
    max_square_corner_velocity: float = 8.0

    def heater(self, name: str) -> HeaterLimits | None:
        return {
            "extruder": self.extruder,
            "heater_bed": self.bed,
            "bed": self.bed,
            "chamber": self.chamber,
        }.get(name)


#: Conservative defaults. The firmware itself allows more (370/120/65 via
#: heater_temp_limit); these are the values an agent gets without opting in.
DEFAULT_SAFETY = SafetyEnvelope(
    # 300 covers PLA→PC/PA-CF; the 370 hardware ceiling needs an explicit raise.
    extruder=HeaterLimits(min=0.0, max=300.0, confirm_above=260.0),
    bed=HeaterLimits(min=0.0, max=120.0, confirm_above=100.0),
    chamber=HeaterLimits(min=0.0, max=65.0, confirm_above=55.0),
    min_jog_z=0.0,
    max_jog_distance=100.0,
    max_extrude_distance=100.0,
    max_move_feedrate=12000.0,
    # Exactly the printer.cfg values (MAX4 above): never more than configured.
    max_velocity=800.0,
    max_accel=30000.0,
    max_square_corner_velocity=8.0,
)


def clamp_envelope(env: SafetyEnvelope, hw: HardwareProfile) -> SafetyEnvelope:
    """Clamp a (possibly user-supplied) envelope into the hardware's range.

    Guarantees the policy layer can never be configured above what the
    firmware would accept, and that ``confirm_above`` stays <= ``max``.
    """

    def fix(h: HeaterLimits, hw_max: float) -> HeaterLimits:
        # max(0.0, ...) matters: clamping only the top could otherwise invert the
        # range (a negative configured ceiling giving min=0, max=-1). An inverted
        # range contains NO value, so `contains()` would refuse target=0 -- i.e. a
        # single mistyped env var would produce a server that cannot turn a hot
        # chamber heater OFF. The range may degenerate to "off only"; it must
        # never become empty.
        hi = max(0.0, min(h.max, hw_max))
        lo = max(0.0, min(h.min, hi))
        return HeaterLimits(min=lo, max=hi, confirm_above=max(0.0, min(h.confirm_above, hi)))

    return replace(
        env,
        extruder=fix(env.extruder, hw.extruder_max_temp),
        bed=fix(env.bed, hw.bed_max_temp),
        chamber=fix(env.chamber, hw.chamber_max_temp),
        max_jog_distance=max(0.0, env.max_jog_distance),
        max_extrude_distance=max(0.0, env.max_extrude_distance),
        max_move_feedrate=max(1.0, min(env.max_move_feedrate, hw.max_velocity * 60.0)),
        # Floors keep each range non-empty (a zero accel cap would refuse every
        # M204, including one that lowers the limit); ceilings are the hardware
        # profile, so no env var can raise them past printer.cfg.
        max_velocity=max(1.0, min(env.max_velocity, hw.max_velocity)),
        max_accel=max(1.0, min(env.max_accel, hw.max_accel)),
        max_square_corner_velocity=max(
            0.0, min(env.max_square_corner_velocity, hw.square_corner_velocity)
        ),
    )


# --- G-code screening -------------------------------------------------------

#: Raw G-code that is refused even in the raw-gcode escape hatch unless the
#: caller passes allow_dangerous=True. These either bypass safety systems,
#: write persistent config, or can drive the machine into itself.
#: Commands refused on the raw G-code path unless allow_dangerous=True.
#:
#: Matched by EQUALITY, not by prefix, despite what the old name suggested: a real
#: prefix match would make "RESTART" swallow "RESTART_SERVICE" and "SET_TMC"
#: silently cover a family whose members differ in risk. So every spelling that
#: must be refused has to be listed explicitly -- including the vendor's own names.
DANGEROUS_GCODE_COMMANDS: frozenset[str] = frozenset(
    {
        "SAVE_CONFIG",              # rewrites printer.cfg and restarts Klipper
        "FORCE_MOVE",               # moves a stepper ignoring kinematic limits
        "SET_KINEMATIC_POSITION",   # lies to Klipper about where the toolhead is
        "SET_TMC_CURRENT",          # can overheat/stall a motor
        "SET_TMC_FIELD",
        "INIT_TMC",
        "PID_CALIBRATE",            # long unattended full-power heating
        "MCU_RESET",
        "RESTART",                  # bare RESTART / FIRMWARE_RESTART
        "FIRMWARE_RESTART",
        "SET_PRESSURE_ADVANCE",     # tuning writes; harmless but not agent-default
        "SET_EXTRUDER_ROTATION_DISTANCE",
        "SET_EXTRUDER_STEP_DISTANCE",
        "SYNC_EXTRUDER_MOTION",
        "SET_STEPPER_ENABLE",       # dropping a stepper mid-print loses position
        "SET_HEATER_SCALING",       # would widen the firmware's own temp caps
        "HEATER_TEMP_LIMIT",
        "M502",                     # reset settings to defaults
        "M301",                     # legacy PID set
        "M304",
        # -- QIDI vendor spellings ----------------------------------------
        # The deny-list is useless if it only knows upstream's names. QIDI's
        # macros reach the same primitives under their own names, and
        # SAVE_CONFIG_QD is the sharpest example: it is "save config without
        # restarting" (qd_macro.cfg:171, 保存配置不重启), so it rewrites
        # printer.cfg exactly as SAVE_CONFIG does but without the restart that
        # would make it obvious.
        "SAVE_CONFIG_QD",
        "SAVE_VARIABLE",            # writes persistent saved_variables.cfg
        # SET_HEATER_SCALING is denied above; its inverse re-widens the caps that
        # QIDI's own heater_temp_limit macro installed (370/120/65 back up to the
        # 375/125/68 MCU trip points), removing a whole safety layer silently.
        "RESET_HEATER_SCALING",
        "M4029",                    # QIDI bed levelling: restarts Klipper
        "M4031",                    # reaches SET_TMC_CURRENT / SET_STEPPER_ENABLE
        "M901",                     # input shaping: writes config
        # -- raw heater control -------------------------------------------
        # thermal.py deliberately goes through QIDI's M104/M140/M141 macros so the
        # firmware's scaling clamp applies. This is the un-clamped path.
        "SET_HEATER_TEMPERATURE",
        "M303",                     # PID autotune (legacy spelling)
        "MPC_CALIBRATE",
        # -- kinematic and stepper limits ---------------------------------
        "SET_VELOCITY_LIMIT",       # raises max_accel/max_velocity past the profile
        "STEPPER_BUZZ",             # moves a stepper directly, ignoring kinematics
        "M84",                      # drop steppers: loses position mid-print
        "M18",                      # same, traditional spelling
        # -- persistent calibration writes --------------------------------
        "Z_OFFSET_APPLY_PROBE",
        "Z_OFFSET_APPLY_ENDSTOP",
        "ENDSTOP_PHASE_CALIBRATE",
        # -- long unattended calibration ----------------------------------
        # Each has a dedicated tool that adds interlocks, progress reporting and a
        # timeout. Reaching them through raw G-code skips all of that, and they
        # move the machine for minutes with nobody watching.
        "PROBE_CALIBRATE",
        "MANUAL_PROBE",
        "TESTZ",
        "BED_MESH_CALIBRATE",
        "SHAPER_CALIBRATE",
        "TEST_RESONANCES",
        "Z_TILT_ADJUST",
        "QUAD_GANTRY_LEVEL",
        "SCREWS_TILT_CALCULATE",
        "DELTA_CALIBRATE",
        "PROBE_ACCURACY",
        "G29",                      # QIDI's wrapper: SAVE_CONFIG_QD + SAVE_VARIABLE
        "G32",
        "G34",
        # -- forging the AI failure detector ------------------------------
        # SET_AI_DETECT_RESULT is how QIDI's vision service *reports into* Klipper
        # that it saw spaghetti or a foreign object. It is an input, not a query,
        # and it cuts both ways: STATE=1 fakes a failure on a healthy print (which
        # the firmware may act on by pausing or aborting), while STATE=0 clears a
        # real detection and suppresses the safety net on a print that is failing.
        # Nothing legitimate needs an agent to impersonate the camera.
        "SET_AI_DETECT_RESULT",
        # -- macro state that safety-relevant macros read ------------------
        # SET_GCODE_VARIABLE rewrites any macro's variables. QIDI's heater macros
        # read their clamp from set_heater_scaling.scaled_heaters and
        # heater_temp_limit, and M204 reads its cap from m201.max_accel, so this
        # one command can undo the firmware's own temperature scaling without
        # touching any name above. We cannot enumerate which variables matter in
        # every macro, so the raw path refuses it outright.
        "SET_GCODE_VARIABLE",
        # -- stock Max4 vendor macros that reach a restricted primitive --------
        # Found by docs/security/proofs/L5_denylist.py, which reads every
        # [gcode_macro] body in QIDI's published config/klipper-macros-qd/ and reports those calling a
        # deny-listed primitive under an innocuous name. Macro bodies are Jinja
        # templates evaluated by Klipper, so the server cannot expand them; the
        # ones with no value check of their own (see HEATER_COMMANDS and
        # LIMIT_COMMANDS) are refused by name instead. Each has a dedicated tool
        # or is internal to QIDI's print sequence.
        # DISABLE_ALL_SENSOR wraps SET_FILAMENT_SENSOR ... ENABLE=0 (qd_macro.cfg),
        # which turns off the filament runout and Box motion sensors; the raw
        # primitive is argument-sensitive below (VALUE_SENSITIVE_COMMANDS).
        "DISABLE_ALL_SENSOR",
        "BEEP_ON",                  # SAVE_VARIABLE beep=1 (persistent write)
        "BEEP_OFF",                 # SAVE_VARIABLE beep=0
        "M603",                     # unload: DISABLE_ALL_SENSOR, M104 S<param>; use unload_filament
        "M604",                     # load: DISABLE_ALL_SENSOR, M104 S<param>; use load_filament
        "PAUSE",                    # DISABLE_ALL_SENSOR, SET_STEPPER_ENABLE; use pause_print
        "PRINT_END",                # DISABLE_ALL_SENSOR; part of the print sequence
        "TEMPERATURE_WAIT_SCALED",  # reads/derives heater scaling
        "_CHECK_HEATER_PARAMS",     # internal to SET_HEATER_SCALING
        "_KM_APPLY_PRINT_OFFSET",   # SAVE=1 writes z_offset via SAVE_VARIABLE
        "_PRINT_START_BOX_PREPAR",  # SAVE_VARIABLE; internal print-start phase
        "_PRINT_START_PHASE_INIT",  # DISABLE_ALL_SENSOR; internal print-start phase
        # -- found in the second review (parameter flow, see L5 section (e)) ----
        # PRINT_START stores every parameter in PRINT_START_SET's variables and the
        # later phases read HOTEND/BED/CHAMBER back from there, so
        # "PRINT_START_SET HOTEND=374" followed by a phase heated past the
        # envelope without either line naming a checked parameter. Only
        # PRINT_START itself (whose parameters are checked) may start a print.
        "PRINT_START_SET",
        "_PRINT_START_PHASE_PREHEAT",
        "_PRINT_START_PHASE_PROBING",
        "_PRINT_START_PHASE_EXTRUDER",
        "_PRINT_START_PHASE_CHECK",
        # heaters.cfg internals. _GCODE_WAIT_WRAPPER_INNER sends
        # "M{params.M}.6245197 S{target}": the caller picks which raw built-in
        # receives another heater's target. M109/M190 forward every parameter
        # here, which is why they accept only their documented letters
        # (HEATER_ALLOWED_PARAMS).
        "_GCODE_WAIT_WRAPPER",
        "_GCODE_WAIT_WRAPPER_INNER",
        # status_events.cfg: stores an arbitrary COMMAND and runs it later on a
        # status change, i.e. deferred G-code the screen never sees.
        "GCODE_ON_PRINT_STATUS",
        "_KM_PRINT_STATUS",
        "_PARK_INNER",              # PARK's unchecked inner move; call PARK
        "M0",                       # = PAUSE
        "M25",                      # = PAUSE
        "M601",                     # = PAUSE
        "M1002",                    # S=1 -> _KM_APPLY_PRINT_OFFSET SAVE=1 (SAVE_VARIABLE)
        "M900",                     # -> SET_PRESSURE_ADVANCE (deny-listed above)
        # QIDI closed-loop stepper controls (filament.cfg, kinematics.cfg): motor
        # current and homing mode, the same class as SET_TMC_CURRENT.
        "SET_OPERATING_CURRENT",
        "SET_HOMING_STATE",
        "SET_HOMING_MODE",
        "EXIT_ENFORCE_MODE",
        "REVERSE_HOMING",
        # -- starting a print outside start_print ---------------------------
        # start_print claims the printer's lease, binds the job to the file that
        # was checked and fitted, and runs the pre-flight interlocks. These start
        # (or select and start) a file from the raw path with none of that; use
        # start_print. M24 also resumes a paused print: use resume_print.
        "SDCARD_PRINT_FILE",
        "M23",                      # select SD file (then M24 starts it)
        "M24",                      # start/resume the selected file
        "M32",                      # select and start a file in one command
    }
)

#: The raw ways to start a print, deny-listed above; start_print is the tool.
PRINT_START_COMMANDS: frozenset[str] = frozenset({"SDCARD_PRINT_FILE", "M23", "M24", "M32"})

#: Deny-listed commands the operator switch (QIDI_ALLOW_DANGEROUS_GCODE=1) can
#: NOT unlock. The switch exists to reach deny-listed commands deliberately, but
#: the value checks below are meant to hold whatever it says, and each of these
#: defeats them: they rewrite the heater scaling the temperature check reads,
#: plant values in macro state that later macros send unchecked
#: (SET_GCODE_VARIABLE MACRO=print_start_set VARIABLE=print VALUE="{'HOTEND': 374}"),
#: store a command to be run later where the screen never sees it, or route one
#: heater's checked target to another heater's raw built-in.
ALWAYS_DENIED_GCODE_COMMANDS: frozenset[str] = frozenset(
    {
        "SET_GCODE_VARIABLE",
        "SET_HEATER_SCALING",
        "RESET_HEATER_SCALING",
        "HEATER_TEMP_LIMIT",
        "_CHECK_HEATER_PARAMS",
        "GCODE_ON_PRINT_STATUS",
        "_KM_PRINT_STATUS",
        "_GCODE_WAIT_WRAPPER",
        "_GCODE_WAIT_WRAPPER_INNER",
    }
)

#: Klipper built-ins that the stock macros shadow with ``rename_existing``.
#:
#: A ``[gcode_macro m104]`` with ``rename_existing: M104.6245197`` keeps the
#: original command reachable under the new name, *without* the vendor wrapper.
#: ``M104.6245197 S374`` is the raw extruder setter: no SET_HEATER_TEMPERATURE_SCALED,
#: so not even QIDI's 370 C clamp applies. ``_BED_MESH_CALIBRATE`` and
#: ``_KM_PAUSE_BASE`` are BED_MESH_CALIBRATE and PAUSE under names the deny-list
#: did not know. Every target is refused; the screen also adds any
#: ``rename_existing`` it finds in the connected printer's own config.
STOCK_RENAMED_BUILTINS: frozenset[str] = frozenset(
    {
        "M104.6245197",            # heaters.cfg
        "M109.6245197",
        "M140.6245197",
        "M190.6245197",
        "G28.6245197",             # kinematics.cfg
        "M24.6245197",             # pause_resume_cancel.cfg
        "M25.6245197",
        "_BED_MESH_CALIBRATE",     # bed_mesh.cfg
        "_KM_PAUSE_BASE",          # pause_resume_cancel.cfg
        "_KM_RESUME_BASE",
        "_KM_CANCEL_PRINT_BASE",
        "_KM_CLEAR_PAUSE",
        "_KM_SAVE_GCODE_STATE",    # state.cfg
        "_KM_RESTORE_GCODE_STATE",
    }
)
DANGEROUS_GCODE_COMMANDS = DANGEROUS_GCODE_COMMANDS | STOCK_RENAMED_BUILTINS

#: The decimal G/M codes the stock config defines as macros of their own
#: (heaters.cfg ``m109.0``/``m109.1``: wait-only; qd_macro.cfg ``G29.0``: mesh
#: clear). Any other decimal code is refused: on this firmware a decimal suffix
#: is how a renamed built-in is spelled, so an unknown one is far more likely
#: to be a raw command behind a vendor wrapper than anything else.
KNOWN_DECIMAL_GCODES: frozenset[str] = frozenset({"M109.0", "M109.1", "G29.0"})

#: Commands that only ever read. Used to fast-path the raw-gcode screen.
READONLY_GCODE_COMMANDS: frozenset[str] = frozenset(
    {
        "STATUS", "HELP", "M105", "M114", "M115", "M119",
        "QUERY_ENDSTOPS", "QUERY_PROBE", "QUERY_FILAMENT_SENSOR",
        "GET_POSITION", "GET_RETRACTION", "DUMP_TMC",
        "BED_MESH_OUTPUT", "BED_MESH_MAP", "PROBE_ACCURACY",
        "CHECK_KM_CONFIG", "LIST_MACROS", "LISTVARS", "KMVARS",
    }
)


@dataclass(frozen=True)
class GcodeScreen:
    """Result of screening one raw G-code line."""

    command: str
    is_readonly: bool
    is_dangerous: bool
    matched: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


#: Klipper's own argument splitter, transcribed from klippy/gcode.py:
#:     args_r = re.compile('([A-Z_]+|[A-Z*])')
#: We must extract the command name *exactly* as Klipper does, or a line can be
#: screened as one command and executed as another.
_KLIPPER_ARGS_RE = re.compile("([A-Z_]+|[A-Z*])")


def klipper_command_name(line: str) -> str:
    """Return the command Klipper will actually run for ``line``.

    A faithful reimplementation of the parsing half of Klipper's
    ``GCodeDispatch._process_commands``. Two details matter and neither is
    obvious:

    * The command is *not* the first whitespace-delimited token. Klipper splits
      on runs of ``[A-Z_]`` and rejoins the first three fragments, so
      ``M109S200`` (no space) is the command ``M109``.
    * A leading ``N<number>`` line number is **skipped**. So ``N1 SAVE_CONFIG``
      runs ``SAVE_CONFIG``. Screening the first token would see ``N1``, call it
      unrecognised, and wave the line through — a complete bypass of the
      deny-list using a syntax every G-code sender supports.

    Screening therefore has to agree with the executor, character for
    character, which is why this mirrors upstream rather than approximating it.
    """
    line = line.strip()
    cpos = line.find(";")
    if cpos >= 0:
        line = line[:cpos]
    parts = _KLIPPER_ARGS_RE.split(line.upper())
    if "".join(parts[:2]) == "N":
        # Line-numbered: skip the N and its digits, take the real command.
        return "".join(parts[3:5]).strip()
    return "".join(parts[:3]).strip()


def klipper_params(line: str) -> dict[str, str]:
    """Extract a line's parameters the way Klipper's dispatcher does.

    Mirrors the ``params`` dict built in ``_process_commands``::

        params = {parts[i]: parts[i+1].strip() for i in range(1, len(parts), 2)}

    Needed because screening command *names* alone is not enough: the name
    ``SET_HEATER_TEMPERATURE`` is perfectly ordinary, while
    ``TARGET=374`` on the same line is not. To range-check the value we have to
    read it exactly as the firmware will.

    Values are returned exactly as Klipper keeps them, separators included.
    ``G27 P1 Z=20`` gives ``Z="=20"``, not ``"20"``: Klipper's native handlers
    reject that value, but the G27/M600 macros forward it to PARK as
    ``Z="=20"``, where ``|float`` reads it as 0.0. Stripping the ``=`` here once
    let the screen approve Z=20 while the toolhead went to Z=0, so the screen now
    refuses such values instead (see :func:`classic_value_problem`).
    """
    text = line.strip()
    cpos = text.find(";")
    if cpos >= 0:
        text = text[:cpos]
    parts = _KLIPPER_ARGS_RE.split(text.upper())
    params: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            params[parts[i]] = parts[i + 1].strip()
    return params


#: Classic commands whose handlers read the raw line as text (display and
#: console messages), so an ``=`` in them is message text, not a parameter.
RAW_TEXT_GCODES = frozenset({"M117", "M118"})

#: What a classic (letter + number) command's parameter value may contain, as
#: :func:`klipper_params` splits it: a number. Klipper's own handlers call
#: ``float()``/``int()`` on it and fail on anything else; the stock macros instead
#: pass it through ``|float``/``|int``, which turn anything unreadable into 0.
CLASSIC_VALUE_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")


def classic_value_problem(command: str, params: dict[str, str]) -> str | None:
    """Why a classic command's parameters cannot be screened, or None if they can.

    A value Klipper keeps but no handler can read is where the screen and the
    printer disagree. The screen would have to guess, and the macros' guess is 0:
    ``G27 P1 Z=20`` and ``M600 Z=20`` reach PARK as Z=0.0, below any raised
    QIDI_MIN_JOG_Z floor. So any value that contains ``=``, a quote or whitespace
    is refused. :data:`RAW_TEXT_GCODES` are exempt, because their text is a message.
    """
    stem = command.split(".", 1)[0]
    if command in RAW_TEXT_GCODES or stem in RAW_TEXT_GCODES:
        return None
    for name, value in params.items():
        if "=" in value or any(ch in value for ch in "\"' \t"):
            return f"{name}{value}"
    return None


def is_traditional_gcode(command: str) -> bool:
    """Klipper's test for a "traditional" command: a letter then a number.

    Mirrors ``GCodeDispatch.is_traditional_gcode``. It decides how Klipper parses
    the parameters: ``M104 S200`` by letter, ``SET_HEATER_TEMPERATURE
    HEATER=heater_bed TARGET=60`` as shell-quoted ``KEY=VALUE`` pairs.
    """
    try:
        cmd = command.upper().split()[0]
        float(cmd[1:])
        return cmd[0].isupper() and cmd[1].isdigit()
    except (IndexError, ValueError):
        return False


#: A parameter name Klipper's extended parser and the stock macros agree on.
EXTENDED_KEY_RE = re.compile(r"[A-Z0-9_]+")


def klipper_extended_params(line: str) -> dict[str, str] | None:
    """Parameters of an extended command, parsed as Klipper's dispatcher does.

    Mirrors ``get_raw_command_parameters`` + ``_get_extended_params``: the text
    after the command name, split shell-style with ``#``/``;`` as comment
    characters, each word split on the first ``=``, keys upper-cased. Values keep
    their case and lose their quotes. Returns ``None`` where Klipper would raise
    "Malformed command", so the caller can refuse a line it cannot read.

    The letter-splitting :func:`klipper_params` is wrong for these commands:
    ``HEATER=heater_bed`` upper-cases into fragments, so the heater name was lost
    and every SET_HEATER_TEMPERATURE line was checked against the extruder's
    ceiling whatever heater it named.
    """
    command = klipper_command_name(line)
    origline = line.strip()
    if not command:
        return {}
    start = origline.upper().find(command)
    if start < 0:
        return None
    raw = origline[start + len(command):]
    end = raw.rfind("*")
    if end >= 0 and raw[end + 1 :].isdigit():
        raw = raw[:end]
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;"
    try:
        pairs = [word.split("=", 1) for word in lexer]
        params = {k.upper(): v for k, v in pairs}
    except ValueError:
        return None
    if not all(EXTENDED_KEY_RE.fullmatch(k) for k in params):
        # Klipper keeps a quoted key verbatim, so ``" HOTEND"=374`` arrives as the
        # key " HOTEND". The screen looks up HOTEND and misses it, while a macro
        # that re-renders its parameters ("for k in params" into "%s=...") turns
        # it back into HOTEND=374 on the next line. Such a line is refused.
        return None
    return params


def command_params(line: str) -> dict[str, str] | None:
    """Parameters of ``line`` read by whichever parser Klipper will use for it."""
    command = klipper_command_name(line)
    if is_traditional_gcode(command):
        return klipper_params(line)
    return klipper_extended_params(line)


#: Commands that set a heater target: command -> {parameter holding a temperature:
#: the heater it sets, or None when the line's own HEATER= parameter names it}.
#: Screening these by name alone is useless — the danger is entirely in the value —
#: so the raw G-code path (and run_macro, which shares it) range-checks every listed
#: parameter through the same envelope the dedicated thermal tools use.
#:
#: On the stock Max4 every entry ends in QIDI's SET_HEATER_TEMPERATURE_SCALED
#: (heaters.cfg), which sends ``min(max((T + bump) * scale, minimum), maximum)``.
#: Stock scaling is scale=1, bump=0, minimum=0, so the temperature reaching the
#: heater is never above the checked value; the commands that could change the
#: scaling (SET_HEATER_SCALING, RESET_HEATER_SCALING, HEATER_TEMP_LIMIT,
#: SET_GCODE_VARIABLE) are deny-listed, and the raw path additionally reads the
#: live scaling and checks the scaled value when it is not stock.
_PRINT_START_TEMPS: dict[str, str | None] = {
    "HOTEND": "extruder",
    "HOTEND_TEMP": "extruder",
    "BED": "bed",
    "BED_TEMP": "bed",
    "CHAMBER": "chamber",
}

HEATER_COMMANDS: dict[str, dict[str, str | None]] = {
    "M104": {"S": "extruder"},                 # extruder, no wait
    # QIDI's wait wrapper takes S, falling back to R (heaters.cfg:77).
    "M109": {"S": "extruder", "R": "extruder"},
    "M140": {"S": "bed"},
    "M190": {"S": "bed", "R": "bed"},
    "M141": {"S": "chamber"},
    "M191": {"S": "chamber", "R": "chamber"},
    "SET_HEATER_TEMPERATURE": {"TARGET": None},
    # QIDI's scaled setter (heaters.cfg:17): the TARGET is pre-scaling.
    "SET_HEATER_TEMPERATURE_SCALED": {"TARGET": None},
    # Internal wait wrapper behind M109/M190 (heaters.cfg:75): S, else R.
    "_GCODE_WAIT_WRAPPER": {"S": None, "R": None},
    # Stock vendor macros that pass a temperature parameter straight to
    # M104/M140/M141 (start_end.cfg, qd_macro.cfg, park.cfg).
    "PRINT_START": _PRINT_START_TEMPS,
    # PRINT_START's first phase (same parameters, same aliases).
    "_PRINT_START_PHASE_INIT": _PRINT_START_TEMPS,
    # PRINT_START_SET stores its parameters as macro state; the later phases read
    # HOTEND (or HOTEND_TEMP), BED and CHAMBER back and heat to them
    # (start_end.cfg: M109 S{print.HOTEND}). It is deny-listed, but the
    # operator switch unlocks that, so the values it plants are checked too.
    "PRINT_START_SET": _PRINT_START_TEMPS,
    # qd_macro.cfg: "M140 S{params.S}" (deny-listed: it also runs SAVE_CONFIG).
    "M4029": {"S": "bed"},
    # filament.cfg unload/load: "M104 S{params.S}" (deny-listed: DISABLE_ALL_SENSOR).
    "M603": {"S": "extruder"},
    "M604": {"S": "extruder"},
    # Box firmware: "E_LOAD SLOT=<n> S=<temp>" heats the extruder for the feed
    # (filament.cfg calls it with M604's temperature).
    "E_LOAD": {"S": "extruder"},
    "CLEAR_NOZZLE": {"HOTEND": "extruder"},
    "CLEAR_NOZZLE_PLR": {"HOTEND": "extruder"},
    # pause_resume_cancel.cfg: "{% if 'R' in params %}M109 S{params.R}{% endif %}".
    "M600": {"R": "extruder"},
    # filament.cfg: "M109 S{params.HOTEND|int}".
    "EXTRUSION_AND_FLUSH": {"HOTEND": "extruder"},
    # Box firmware command (start_end.cfg passes it the print's HOTEND). Its body
    # is not published, so the temperature it is handed is bounded like any other.
    "BOX_PRINT_START": {"HOTENDTEMP": "extruder"},
}

#: Heater commands whose stock macro forwards *every* parameter it is given to an
#: inner macro (heaters.cfg: "for k in params" into _GCODE_WAIT_WRAPPER, which
#: hands them to _GCODE_WAIT_WRAPPER_INNER). There, ``M=`` picks the raw built-in
#: that receives the heater's target: ``M109 S200 R1 M140`` set the extruder to a
#: checked 200 C and then sent ``M140.6245197 S200`` to the bed. Only the
#: documented parameters are accepted.
HEATER_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "M109": frozenset({"S", "R", "T"}),
    "M190": frozenset({"S", "R"}),
}

#: The largest G-code offset (mm) the raw path accepts, the same cap set_z_offset
#: applies. SET_GCODE_OFFSET ... MOVE=1 moves the toolhead by the change, and any
#: offset shifts every later coordinate, so it is checked like a coordinate.
MAX_GCODE_OFFSET_MM = 5.0

#: Commands that set a G-code offset: command -> parameters holding millimetres.
OFFSET_COMMANDS: dict[str, frozenset[str]] = {
    "SET_GCODE_OFFSET": frozenset({"X", "Y", "Z", "X_ADJUST", "Y_ADJUST", "Z_ADJUST"}),
    # qd_macro.cfg: "SET_GCODE_OFFSET Z_ADJUST={params.Z}".
    "M290": frozenset({"Z"}),
}

#: Commands that change the toolhead's kinematic limits at runtime: command ->
#: {parameter: which envelope ceiling bounds it}. Klipper does not cap these at the
#: printer.cfg values, so ``M203 X99999`` would raise max_velocity a hundredfold.
#: SET_VELOCITY_LIMIT itself stays on the deny-list.
LIMIT_COMMANDS: dict[str, dict[str, str]] = {
    # QIDI velocity.cfg: M201 stores min(X, Y) as the accel cap M204 honours.
    "M201": {"X": "accel", "Y": "accel"},
    # M203 -> SET_VELOCITY_LIMIT VELOCITY=min(X, Y).
    "M203": {"X": "velocity", "Y": "velocity"},
    # Klipper's native M204: S, else min(P, T), becomes max_accel.
    "M204": {"S": "accel", "P": "accel", "T": "accel"},
    # M205 -> SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=min(X, Y).
    "M205": {"X": "square_corner_velocity", "Y": "square_corner_velocity"},
}

#: Commands that command absolute or relative motion, and the axis letters whose
#: values are coordinates.
MOTION_COMMANDS: frozenset[str] = frozenset({"G0", "G1", "G2", "G3"})
MOTION_AXES: tuple[str, ...] = ("X", "Y", "Z")

#: Stock macros whose X/Y/Z parameters become absolute toolhead coordinates
#: (park.cfg: PARK -> _PARK_INNER -> G0; G27 and M600 forward X/Y/Z to PARK).
#: PARK's own _CHECK_KINEMATIC_LIMITS only bounds them by the firmware's travel;
#: the screen applies the envelope as it does to G0/G1.
#: Each saves the G-code state and switches to G90 itself (park.cfg), so its
#: coordinates are absolute whatever mode the script left the printer in, and
#: the envelope (including the Z floor) applies even after a G91.
MOTION_MACROS: dict[str, tuple[str, ...]] = {
    "PARK": MOTION_AXES,
    "G27": MOTION_AXES,
    "M600": MOTION_AXES,
    # Deny-listed, but the operator switch unlocks that: PAUSE forwards X/Y/Z to
    # PARK, and _PARK_INNER is PARK's inner move.
    "PAUSE": MOTION_AXES,
    "_PARK_INNER": MOTION_AXES,
}

#: Box firmware commands (the closed ``box_extras`` / ``multi_color_controller``
#: module). Their parameters are not published: the stock macros only show
#: ``E_LOAD SLOT=<n> S=<temp>`` and ``BOX_PRINT_START HOTENDTEMP=<temp>``, and the
#: one real Box seen registered ``MULTI_COLOR_*`` names instead (box-first-contact).
#: So any parameter of one of these whose name could carry a temperature is
#: checked: against the extruder for the load/unload/print-start family, which
#: heat the nozzle to feed, and refused outright for the rest (the Box dryer and
#: heater have no envelope here).
BOX_FIRMWARE_COMMANDS: frozenset[str] = frozenset(
    {
        "E_LOAD",
        "E_UNLOAD",
        "EXTRUDER_LOAD",
        "EXTRUDER_UNLOAD",
        "BOX_PRINT_START",
        "INIT_BOX_STATE",
        "INIT_RFID_READ",
        "CLEAR_TOOLCHANGE_STATE",
        "TRY_RESUME_PRINT",
        "DISABLE_BOX_HEATER",
    }
)
BOX_FIRMWARE_PREFIXES: tuple[str, ...] = ("MULTI_COLOR_",)
#: A parameter name that may hold a temperature: E_LOAD's S, a wait's R, or any
#: name mentioning a temperature, a heater or a target.
_TEMPERATURE_PARAM_RE = re.compile(r"^(?:S|R)$|TEMP|HOT|HEAT|TARGET|DEGREE|CELSIUS")


def is_box_firmware_command(command: str) -> bool:
    return command in BOX_FIRMWARE_COMMANDS or command.startswith(BOX_FIRMWARE_PREFIXES)


def box_temperature_params(command: str, params: dict[str, str]) -> dict[str, str | None]:
    """Temperature-like parameters of a Box firmware command: name -> heater.

    ``None`` means there is no envelope to check it against, so the screen
    refuses it. Parameters already listed in :data:`HEATER_COMMANDS` are left to
    that table.
    """
    if not is_box_firmware_command(command):
        return {}
    known = HEATER_COMMANDS.get(command, {})
    heater = "extruder" if ("LOAD" in command or command == "BOX_PRINT_START") else None
    return {
        name: heater
        for name in params
        if name not in known and _TEMPERATURE_PARAM_RE.search(name)
    }


def is_decimal_gcode(command: str) -> bool:
    """True for a traditional G/M code with a decimal suffix, e.g. ``M104.6245197``."""
    return "." in command and is_traditional_gcode(command)


def table_key(command: str) -> str:
    """The name to look a command up by in the value-check tables.

    A decimal G/M code is checked as its stem: ``M104.6245197`` sets the
    extruder exactly as ``M104`` does, only without QIDI's wrapper, so it must
    never escape the M104 temperature check by being spelled differently.
    """
    if is_decimal_gcode(command):
        return command.split(".", 1)[0]
    return command


#: Commands whose danger lives in an ARGUMENT, not the name. Mapped to the
#: parameters that make them persistent/destructive.
#:
#: ``BED_MESH_PROFILE LOAD=default`` is routine; ``SAVE=`` and ``REMOVE=`` rewrite
#: the stored profile and require a SAVE_CONFIG to persist. Denying the whole
#: command would block the harmless form and push agents toward the escape hatch,
#: which is the opposite of what a deny-list is for.
ARGUMENT_SENSITIVE_COMMANDS: dict[str, frozenset[str]] = {
    "BED_MESH_PROFILE": frozenset({"SAVE", "REMOVE"}),
}

#: Commands that are dangerous for some VALUES of a parameter: command ->
#: {parameter: the only integer value accepted}. ``SET_FILAMENT_SENSOR ...
#: ENABLE=0`` is the primitive behind DISABLE_ALL_SENSOR: it switches off the
#: runout and Box motion sensors. ENABLE=1 (what ENABLE_ALL_SENSOR sends) is fine.
VALUE_SENSITIVE_COMMANDS: dict[str, dict[str, int]] = {
    "SET_FILAMENT_SENSOR": {"ENABLE": 1},
}


def screen_gcode(line: str, *, extra_denied: frozenset[str] = frozenset()) -> GcodeScreen:
    """Classify a single raw G-code line.

    The command name comes from :func:`klipper_command_name`, so the screen sees
    what the firmware will run rather than what the text superficially looks
    like. ``extra_denied`` adds names to the deny-list for this call (the
    ``rename_existing`` targets found in the connected printer's config).
    """
    token = klipper_command_name(line)
    if not token:
        return GcodeScreen(command="", is_readonly=True, is_dangerous=False)

    # Numbered G/M codes may carry a decimal (M109.1); compare on the stem too.
    stem = token.split(".", 1)[0]

    is_readonly = token in READONLY_GCODE_COMMANDS or stem in READONLY_GCODE_COMMANDS
    matched = None
    for bad in DANGEROUS_GCODE_COMMANDS | extra_denied:
        if token == bad or stem == bad:
            matched = bad
            break
    if matched is None and is_decimal_gcode(token) and token not in KNOWN_DECIMAL_GCODES:
        # A decimal suffix is how this firmware spells a renamed built-in.
        matched = token
        is_readonly = False

    # Some commands are safe or dangerous depending on their arguments.
    if matched is None:
        sensitive = ARGUMENT_SENSITIVE_COMMANDS.get(token) or ARGUMENT_SENSITIVE_COMMANDS.get(
            stem
        )
        if sensitive:
            present = sensitive & set(klipper_params(line))
            if present:
                matched = f"{token} {'/'.join(sorted(present))}"

    if matched is None and token in VALUE_SENSITIVE_COMMANDS:
        params = klipper_extended_params(line)
        for name, only in VALUE_SENSITIVE_COMMANDS[token].items():
            if params is None:
                matched = f"{token} (unreadable parameters)"
                break
            if name not in params:
                continue
            try:
                ok = int(params[name].strip()) == only
            except ValueError:
                ok = False
            if not ok:
                matched = f"{token} {name}={params[name]}"
                break

    notes: tuple[str, ...] = ()
    if line.strip().upper() != token:
        # Surface the resolution when the raw text and the resolved command
        # differ (line numbers, missing spaces, comments), so an audit reader can
        # see why a line was classified the way it was.
        notes = (f"resolved to Klipper command {token!r}",)

    return GcodeScreen(
        command=token,
        is_readonly=is_readonly,
        is_dangerous=matched is not None,
        matched=matched,
        notes=notes,
    )


def is_illegal_script_char(ch: str) -> bool:
    """Characters refused anywhere in a raw script or macro parameter.

    Klipper splits a script on ``\\n`` and nothing else, and its parameter parser
    keeps the *last* value of a repeated letter. Python's ``str.splitlines()``
    also breaks on ``\\r``, ``\\x0b``, ``\\x0c``, ``\\x1c``-``\\x1e``, ``\\x85``,
    ``\\u2028`` and ``\\u2029``, so screening by ``splitlines()`` checked
    ``M104 S100\\rS374`` as two harmless lines while Klipper ran one line with
    S=374. Any control character other than tab, and any Unicode line or
    paragraph separator, is therefore refused rather than interpreted.
    """
    if ch == "\t":
        return False
    return unicodedata.category(ch) in {"Cc", "Zl", "Zp"}


def check_script_text(text: str, what: str = "script") -> None:
    """Refuse text containing a separator Klipper and the screen could disagree on.

    ``\\n`` is the only line break; a single ``\\r`` directly before it (or at the
    very end) is tolerated so CRLF scripts still work.
    """
    for lineno, line in enumerate(text.split("\n"), 1):
        body = line[:-1] if line.endswith("\r") else line
        for ch in body:
            if is_illegal_script_char(ch):
                raise ValueError(
                    f"{what} line {lineno} contains the control character "
                    f"U+{ord(ch):04X}. Klipper separates commands with newlines only, "
                    "so a script containing other control or line-separator characters "
                    "is refused rather than guessed at."
                )


def split_gcode_script(text: str) -> list[str]:
    """Split a script into the lines Klipper will execute, after checking it.

    Mirrors Klipper's ``run_script`` (``script.split('\\n')``); blank lines are
    dropped. Raises :class:`ValueError` from :func:`check_script_text`.
    """
    check_script_text(text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]
