"""An in-process fake Moonraker that behaves like a QIDI Max4.

The object model, heater limits, macro names and QIDI-specific modules are
modeled from QIDI's published config (the fixture itself is synthetic), so a test that passes
here is exercising the same shapes the real machine returns.

It is a real aiohttp server on a loopback port — not a monkeypatched client — so
the HTTP layer, query encoding, envelope unwrapping and error mapping are all
genuinely tested.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

from aiohttp import web

# The Max4's Klipper is closest to upstream v0.13.0 with QIDI patches; the exact
# upstream commit is not published.
KLIPPER_VERSION = "v0.13.0-qidi-284-g5d1b0f3f"
MOONRAKER_VERSION = "v0.8.0-266-gd5b0e7f"

def dark_jpeg(width: int = 1920, height: int = 1080, payload: int = 2048) -> bytes:
    """A valid-enough JPEG declaring ``width``x``height`` with almost no data in it.

    Reproduces the real failure mode: the unlit Max4 chamber returns a perfectly
    well-formed 1920x1080 JPEG that happens to be nearly all black, so it is cheap
    to compress and carries no information about the bed.
    """
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")  # segment length
        + b"\x08"  # 8-bit precision
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"  # 3 components
        + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + sof + b"\xff\xda" + b"\x00" * payload + b"\xff\xd9"


def _header_metadata(content: bytes) -> dict[str, Any]:
    """The slicer keys Moonraker would parse out of an uploaded file's comments.

    Only the handful the master queue compares: a test writes ``; filament_type =
    PETG`` into a file and the mock's metadata route then says ``PETG``, exactly as
    the real printer's would after parsing the upload.
    """
    out: dict[str, Any] = {}
    for raw in content[:65536].decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith(";") or "=" not in line:
            continue
        key, _, value = line[1:].partition("=")
        key, value = key.strip().lower(), value.strip()
        if key == "filament_type" and value:
            out["filament_type"] = value
        elif key in {"filament_colour", "filament_color"} and value:
            out["filament_colors"] = [c.strip() for c in value.split(";") if c.strip()]
        elif key == "estimated printing time (normal mode)" and value:
            h = m = s = 0
            for part in value.split():
                if part.endswith("h"):
                    h = int(part[:-1])
                elif part.endswith("m"):
                    m = int(part[:-1])
                elif part.endswith("s"):
                    s = int(part[:-1])
            out["estimated_time"] = float(h * 3600 + m * 60 + s)
    return out


#: Mirrors QIDITECH/QIDI_MAX4 config/printer.cfg, including QIDI's private modules.
OBJECT_LIST = [
    "webhooks", "configfile", "mcu", "mcu THR", "gcode_move", "print_stats",
    "virtual_sdcard", "pause_resume", "display_status", "idle_timeout", "toolhead",
    "extruder", "heater_bed", "heaters", "fan", "motion_report", "query_endstops",
    "system_stats", "manual_probe", "save_variables", "exclude_object", "bed_mesh",
    "bed_screws", "z_tilt", "force_move", "respond", "gcode_arcs",
    "heater_generic chamber", "verify_heater chamber", "verify_heater extruder",
    "verify_heater heater_bed",
    "temperature_sensor Chamber_Thermal_Protection_Sensor",
    "fan_generic cooling_fan", "fan_generic auxiliary_cooling_fan",
    "fan_generic auxiliary_cooling_fan2", "fan_generic chamber_circulation_fan",
    "heater_fan hotend_fan", "controller_fan chamber_fan", "controller_fan board_fan",
    "output_pin caselight", "output_pin beeper", "output_pin Polar_cooler",
    "neopixel RGB", "filament_switch_sensor filament_switch_sensor", "ai_func_manager",
    "tmc2209 extruder", "tmc2209 stepper_z", "tmc2209 stepper_z1",
    "stepper_x", "stepper_y", "stepper_z", "stepper_z1",
    "lis2dw", "resonance_tester", "input_shaper",
    # QIDI-only modules:
    "probe_air", "cl_interface", "closed_loop x", "closed_loop y",
    "multi_color_controller", "print_stats_manager", "d_bus_service_manager",
    # Macros (subset of the real ~90):
    "gcode_macro PRINT_START", "gcode_macro PRINT_END", "gcode_macro CANCEL_PRINT",
    "gcode_macro PAUSE", "gcode_macro RESUME", "gcode_macro M600", "gcode_macro M603",
    "gcode_macro M604", "gcode_macro CLEAR_NOZZLE", "gcode_macro M4029",
    "gcode_macro M4031", "gcode_macro M1001", "gcode_macro M1002", "gcode_macro M1004",
    "gcode_macro M290", "gcode_macro M300", "gcode_macro M901", "gcode_macro G32",
    "gcode_macro LED_ON", "gcode_macro LED_OFF", "gcode_macro MOVE_TO_TRASH",
    "gcode_macro HEATER_TEMP_LIMIT", "gcode_macro SET_HEATER_SCALING",
    "gcode_macro BED_MESH_CALIBRATE", "gcode_macro G28", "gcode_macro M104",
    "gcode_macro M140", "gcode_macro M141", "gcode_macro M109", "gcode_macro M190",
    "gcode_macro M191",
]

#: Synthetic miniature of the filament-table *format*, not copied QIDI config.
#: IDs and display names are sample facts; operating temperatures are invented
#: and must never be used for real printing (live APIs read the printer's table).
FILAMENT_TABLE = """\
[fila1]
filament = PLA Rapido
type = PLA
min_temp = 201
max_temp = 231

[fila2]
filament = PLA Matte
type = PLA
min_temp = 204
max_temp = 234

[colordict]
1 = #FAFAFA
2 = #060606

[vendor_list]
0 = Generic
1 = QIDI
"""

#: The real config values from printer.cfg that the MCP derives limits from.
CONFIG_SETTINGS: dict[str, Any] = {
    "printer": {
        "kinematics": "corexy",
        "max_velocity": 800.0,
        "max_accel": 30000.0,
        "max_z_velocity": 10.0,
        "max_z_accel": 500.0,
        "square_corner_velocity": 8.0,
    },
    "extruder": {
        "min_temp": 0.0, "max_temp": 375.0, "min_extrude_temp": 135.0,
        "nozzle_diameter": 0.4, "filament_diameter": 1.75,
        "pressure_advance": 0.032, "rotation_distance": 53.7,
    },
    "heater_bed": {"min_temp": -100.0, "max_temp": 125.0, "max_power": 0.7},
    "heater_generic chamber": {"min_temp": -100.0, "max_temp": 68.0, "max_power": 1.0},
    "temperature_sensor Chamber_Thermal_Protection_Sensor": {
        "min_temp": -100.0, "max_temp": 150.0
    },
    "stepper_x": {"position_min": -2.0, "position_max": 392.0, "position_endstop": 392.0},
    "stepper_y": {"position_min": -5.0, "position_max": 410.0, "position_endstop": -1.0},
    "stepper_z": {"position_min": -5.0, "position_max": 342.0},
    "bed_mesh": {
        "mesh_min": [15.0, 15.0], "mesh_max": [375.0, 375.0],
        "probe_count": [9, 9], "algorithm": "bicubic",
    },
    "z_tilt": {"retries": 4, "retry_tolerance": 0.05},
    "input_shaper": {
        "shaper_type_x": "ei", "shaper_freq_x": 54.4,
        "shaper_type_y": "mzv", "shaper_freq_y": 36.2,
    },
    "resonance_tester": {"accel_per_hz": 150.0, "max_smoothing": 0.25},
    "idle_timeout": {"timeout": 43200.0},
    "probe_air": {"z_offset": -0.05, "samples": 2},
    "fan_generic cooling_fan": {"max_power": 1.0},
    "output_pin caselight": {"pwm": False, "value": 1.0},
}

GCODE_HELP = {
    "ENABLE_AI_DETECT": "Enable/disable AI detection",
    "SET_AI_SENSITIVITY": "Set AI sensitivity: TYPE=noodle/foreign VALUE=LOW/MEDIUM/HIGH",
    "SET_AI_DETECT_RESULT": "AI reports a detection result",
    "G28": "Auto home the printer",
    "G1": "Linear move",
    "TURN_OFF_HEATERS": "Turn off all heaters",
    "M104": "Set extruder temperature",
    "M105": "Get temperature",
    "M106": "Set fan speed",
    "M107": "Fan off",
    "M109": "Set extruder temperature and wait",
    "M112": "Emergency stop",
    "M117": "Display message",
    "M118": "Write to console",
    "M140": "Set bed temperature",
    "M141": "Set chamber temperature",
    "M190": "Set bed temperature and wait",
    "M191": "Set chamber temperature and wait",
    "M220": "Set speed factor",
    "M221": "Set extrude factor",
    "M300": "Beep",
    "M400": "Wait for moves to finish",
    "M603": "filament unlode",
    "M604": "filament load",
    "M4029": "QIDI bed levelling calibration",
    "M1001": "auto resonance",
    "M1002": "manage z offset",
    "M1004": "enable polar cooler",
    "M290": "Babystep Z",
    "M901": "input shaping",
    "BED_MESH_CALIBRATE": "Perform Mesh Bed Leveling",
    "BED_MESH_PROFILE": "Bed Mesh Persistent Storage management",
    "BED_MESH_CLEAR": "Clear the Mesh so no z-adjustment is made",
    "BED_MESH_OUTPUT": "Retrieve interpolated grid of probed z-points",
    "Z_TILT_ADJUST": "Adjust the Z tilt",
    "PROBE": "Probe Z-height at current XY position",
    "PROBE_ACCURACY": "Probe Z-height accuracy at current XY position",
    "QUERY_PROBE": "Return the status of the z-probe",
    "QUERY_ENDSTOPS": "Report on the status of each endstop",
    "SCREWS_TILT_CALCULATE": "Tool to help adjust bed leveling screws",
    "SET_FAN_SPEED": "Sets the speed of a fan",
    "SET_PIN": "Set the value of an output pin",
    "SET_LED": "Set the color of an LED",
    "SET_GCODE_OFFSET": "Set a virtual offset to g-code positions",
    "SET_HEATER_TEMPERATURE": "Sets a heater temperature",
    "SET_FILAMENT_SENSOR": "Sets the filament sensor on/off",
    "SET_INPUT_SHAPER": "Set cancel vibration parameters",
    "SHAPER_CALIBRATE": "Similar to TEST_RESONANCES but suggest input shaper config",
    "TEST_RESONANCES": "Runs the resonance test for a specifed axis",
    "SAVE_CONFIG": "Overwrite config file and restart",
    "FORCE_MOVE": "Manually move a stepper; invalidates kinematics",
    "SET_KINEMATIC_POSITION": "Force a low-level kinematic position",
    "SET_TMC_CURRENT": "Set the current of a TMC driver",
    "PID_CALIBRATE": "Run PID calibration test",
    "RESTART": "Reload config file and restart host software",
    "FIRMWARE_RESTART": "Restart firmware, host, and reload config",
    "CLEAR_NOZZLE": "Clean the nozzle on the purge chute",
    "SAVE_VARIABLE": "Save arbitrary variables to disk",
    "PAUSE": "Pauses the current print",
    "RESUME": "Resumes the print from a pause",
    "CANCEL_PRINT": "Cancel the current print",
    "DUMP_TMC": "Read and display TMC stepper driver registers",
    "STATUS": "Report the printer status",
    "HELP": "Report the list of available extended G-Code commands",
}


class MockPrinter:
    """Mutable printer state the endpoints read and write."""

    def start_manual_print(self, filename: str = "benchy.gcode") -> dict[str, Any]:
        """A print started on the touchscreen: Klipper and history see it, no lease does."""
        self.print_filename, self.print_state, self.progress = filename, "printing", 0.05
        job = {
            "job_id": f"{len(self.history) + 1:06X}", "filename": filename,
            "status": "in_progress", "start_time": time.time(), "end_time": None,
            "print_duration": 0.0, "total_duration": 0.0, "filament_used": 0.0,
            "exists": True,
        }
        self.history.append(job)
        return job

    def finish_print(self, status: str = "completed", *, duration: float = 3600.0) -> dict[str, Any]:
        """End the running job the way Moonraker records it, and return the record."""
        job = next(j for j in reversed(self.history) if j["status"] == "in_progress")
        job.update(status=status, end_time=time.time(), print_duration=duration,
                   total_duration=duration + 60, filament_used=4200.0)
        self.print_state = "complete" if status == "completed" else status
        return job

    def __init__(self) -> None:
        self.klippy_state = "ready"
        self.klippy_message = ""
        self.print_state = "standby"
        self.print_filename = ""
        self.homed_axes = ""
        self.position = [0.0, 0.0, 0.0, 0.0]
        self.extruder_temp = 24.5
        self.extruder_target = 0.0
        self.bed_temp = 23.8
        self.bed_target = 0.0
        self.chamber_temp = 25.1
        self.chamber_target = 0.0
        self.fan_speed = 0.0
        self.aux_fan_speed = 0.0
        self.circulation_fan_speed = 0.0
        self.caselight = 1.0
        self.beeper = 0.0
        self.polar_cooler = 0.0
        # AI failure detection. Defaults copied from a real Max4: both detectors
        # ship DISABLED, with these sensitivities already set.
        self.ai_detect = {
            "noodle": {"enable": 0, "sensitivity": "HIGH"},
            "foreign": {"enable": 0, "sensitivity": "MEDIUM"},
        }
        self.ai_error_type = ""
        self.ai_counts = 0
        self.z_offset = 0.0
        self.speed_factor = 1.0
        self.extrude_factor = 1.0
        self.filament_detected = True
        self.filament_sensor_enabled = True
        # Moonraker's job queue. Measured on a real Max4: an untouched machine
        # reports queue_state "paused", NOT "ready" -- which matters, because a
        # live queue turns "enqueue" into "start printing now".
        self.queue_state = "paused"
        self.queued_jobs: list[dict[str, Any]] = []
        #: [job_queue] automatic_transition in moonraker.conf; None = not set (default off).
        self.queue_automatic_transition: bool | None = None
        #: Moonraker's job history. A started print appends an ``in_progress``
        #: job; ``finish_print`` gives it a terminal status, as Moonraker does.
        self.history: list[dict[str, Any]] = [
            {
                "job_id": "000001", "filename": "benchy.gcode", "status": "completed",
                "start_time": 1_756_000_000.0, "end_time": 1_756_005_820.0,
                "print_duration": 5820.0, "total_duration": 5900.0,
                "filament_used": 3821.4, "exists": True,
            },
            {
                "job_id": "000002", "filename": "projects/bracket_abs.gcode",
                "status": "cancelled", "start_time": 1_756_100_000.0,
                "end_time": 1_756_101_200.0, "print_duration": 1200.0,
                "total_duration": 1250.0, "filament_used": 812.0, "exists": True,
            },
        ]
        self._next_job_id = 1
        # Moonraker's namespaced key-value database, where the cross-agent lease
        # lives. Third-party namespaces are created on first write and dropped
        # when their last key goes, exactly as the real one behaves.
        self.database: dict[str, dict[str, Any]] = {
            "webcams": {},
            "moonraker": {},
            "gcode_metadata": {},
            "fluidd": {},
            "announcements": {},
        }
        self.progress = 0.0
        self.gcode_log: list[dict[str, Any]] = []
        self.commands: list[str] = []          # every script received, in order
        self.emergency_stops = 0
        #: When True, Klipper-backed endpoints answer 503 even though
        #: /server/info still reports the true klippy_state.
        self.fail_queries_with_503 = False
        self.break_heaters = 0
        self.break_macros = 0
        self.power_loss_resumes = 0
        self.firmware_restarts = 0
        self.host_restarts = 0
        self.reboots = 0
        self.shutdowns = 0
        self.uploads: list[dict[str, Any]] = []
        #: Files that exist but that Moonraker has no metadata for (yet): its
        #: metadata route 404s for them exactly as it does for a missing file.
        self.unparsed: set[str] = set()
        #: Uploads answer 500, as a full disk or a mid-upload reboot would.
        self.fail_uploads = False
        #: Keys merged over the metadata derived from an uploaded file's header, so
        #: a test can make the printer "see" a different filament than the file says.
        self.upload_meta_override: dict[str, Any] | None = None
        #: Moonraker's object processing rewrites the file after upload, so its
        #: size and mtime differ from what was sent. On by request only.
        self.process_objects = False
        self.deleted: list[str] = []
        self.service_actions: list[tuple[str, str]] = []
        self.files: dict[str, dict[str, Any]] = {
            "benchy.gcode": {
                "path": "benchy.gcode", "size": 2_483_910, "modified": 1_756_000_000.0,
                "estimated_time": 5820.0, "filament_total": 3821.4,
                "filament_weight_total": 11.4, "filament_type": "PLA",
                "layer_height": 0.2, "first_layer_height": 0.25, "object_height": 48.0,
                "nozzle_diameter": 0.4, "first_layer_bed_temp": 60.0,
                "first_layer_extr_temp": 220.0, "chamber_temp": 0.0,
                "slicer": "QIDISlicer", "slicer_version": "1.7.0", "layer_count": 240,
            },
            "projects/bracket_abs.gcode": {
                "path": "projects/bracket_abs.gcode", "size": 8_100_224,
                "modified": 1_756_400_000.0, "estimated_time": 24_300.0,
                "filament_total": 19_842.0, "filament_weight_total": 59.2,
                "filament_type": "ABS", "layer_height": 0.2, "object_height": 120.0,
                "nozzle_diameter": 0.4, "first_layer_bed_temp": 100.0,
                "first_layer_extr_temp": 255.0, "chamber_temp": 50.0,
                "slicer": "QIDISlicer", "slicer_version": "1.7.0",
            },
        }
        self.saved_variables: dict[str, Any] = {
            "auto_reload_detect": 1, "auto_read_rfid": 1, "auto_init_detect": 1,
            "box_count": 0, "enable_box": 1, "filepath": "", "last_file": "",
            "last_load_slot": "slot16", "power_resume_z": 0.0, "slot_sync": "slot-1",
            "was_interrupted": False, "z_offset": 0.0, "power_resume_file": "",
            **{f"slot{i}": 0 for i in range(16)},
            **{f"filament_slot{i}": 1 for i in range(17)},
            **{f"color_slot{i}": 1 for i in range(17)},
            **{f"vendor_slot{i}": 1 for i in range(17)},
        }
        self.bed_mesh_profiles: dict[str, Any] = {"default": {}, "kamp": {}}
        self.active_mesh = "default"
        # Box support. A stock Max4 has NO Box: no box_extras object, no E_LOAD, and
        # box_count 0. attach_box() builds the other machine. The extra objects and
        # help lines are per-instance so one test's Box cannot leak into the next.
        self.box_heater_disables = 0
        self.extra_objects: list[str] = []
        self.extra_help: dict[str, str] = {}
        #: Extra status objects merged into objects/query, e.g. a macro's variables.
        self.extra_status: dict[str, Any] = {}
        self.box_extras: dict[str, Any] | None = None
        self.box_motion_sensor: dict[str, Any] | None = None
        #: Served as config/officiall_filas_list.cfg; None -> 404.
        self.filament_table: str | None = FILAMENT_TABLE

    def attach_box(self, units: int = 1, *, register_count: bool = True) -> None:
        """Turn this mock into a Max4 with a Box.

        The object and command names come from QIDI's published macros, nothing else:
        ``box_extras`` and ``filament_motion_sensor box_motion_sensor`` are read by
        ``pause_resume_cancel.cfg`` / ``qd_macro.cfg``, and ``E_LOAD`` / ``E_UNLOAD`` /
        ``INIT_BOX_STATE`` / ``INIT_RFID_READ`` are called by ``filament.cfg`` /
        ``globals.cfg``. The *contents* of box_extras are invented (QIDI do not publish
        them), which is why tests only assert it is passed through verbatim.

        ``register_count=False`` builds the half-configured machine where Klipper has
        the Box object but save_variables.box_count is still 0.
        """
        self.extra_objects += [
            "box_extras",
            "filament_motion_sensor box_motion_sensor",
            "gcode_macro E_LOAD",
            "gcode_macro E_UNLOAD",
        ]
        self.extra_help.update(
            {
                "INIT_BOX_STATE": "Re-read the slot sensors of every Box",
                "INIT_RFID_READ": "Read the RFID tag of every slot",
                "BOX_PRINT_START": "Prepare the Box for a print",
                "DISABLE_BOX_HEATER": "Turn the Box heater off",
            }
        )
        self.box_extras = {"is_tool_change": 0}
        self.box_motion_sensor = {"enabled": True, "filament_detected": False}
        if register_count:
            self.saved_variables["box_count"] = units
        # A real Box has spools in it: mark the first slot occupied by a known filament
        # (id 2 = the second table entry, colour 2 = #060606) so name decoding is testable.
        self.saved_variables["slot0"] = 1
        self.saved_variables["filament_slot0"] = 2
        self.saved_variables["color_slot0"] = 2

    # -- state assembly -----------------------------------------------------

    def log(self, message: str, *, kind: str = "response") -> None:
        self.gcode_log.append({"message": message, "time": 1_756_500_000.0, "type": kind})

    def status(self) -> dict[str, Any]:
        return {
            "webhooks": {"state": self.klippy_state, "state_message": self.klippy_message},
            "print_stats": {
                "filename": self.print_filename,
                "total_duration": 120.0 if self.print_state != "standby" else 0.0,
                "print_duration": 100.0 if self.print_state != "standby" else 0.0,
                "filament_used": 240.5 if self.print_state != "standby" else 0.0,
                "state": self.print_state,
                "message": "",
                "info": {"total_layer": 240, "current_layer": 12},
            },
            "display_status": {"progress": self.progress, "message": None},
            "idle_timeout": {
                "state": "Printing" if self.print_state == "printing" else "Idle",
                "printing_time": 100.0,
            },
            "toolhead": {
                "homed_axes": self.homed_axes,
                "position": list(self.position),
                "axis_minimum": [-2.0, -5.0, -5.0, 0.0],
                "axis_maximum": [392.0, 410.0, 342.0, 0.0],
                "extruder": "extruder",
                "max_velocity": 800.0,
                "max_accel": 30000.0,
                "square_corner_velocity": 8.0,
                "print_time": 0.0,
                "estimated_print_time": 0.0,
                "stalls": 0,
            },
            "gcode_move": {
                "speed_factor": self.speed_factor,
                "extrude_factor": self.extrude_factor,
                "absolute_coordinates": True,
                "absolute_extrude": False,
                "homing_origin": [0.0, 0.0, self.z_offset, 0.0],
                "position": list(self.position),
                "gcode_position": list(self.position),
                "speed": 3000.0,
            },
            "extruder": {
                "temperature": self.extruder_temp, "target": self.extruder_target,
                "power": 0.0, "can_extrude": self.extruder_temp >= 135.0,
                "pressure_advance": 0.032, "smooth_time": 0.03,
            },
            "heater_bed": {
                "temperature": self.bed_temp, "target": self.bed_target, "power": 0.0
            },
            "heater_generic chamber": {
                "temperature": self.chamber_temp, "target": self.chamber_target, "power": 0.0
            },
            "temperature_sensor Chamber_Thermal_Protection_Sensor": {
                "temperature": self.chamber_temp + 0.4,
                "measured_min_temp": 21.0, "measured_max_temp": 58.2,
            },
            "fan": {"speed": self.fan_speed, "rpm": None},
            "fan_generic cooling_fan": {"speed": self.fan_speed, "rpm": 3200},
            "fan_generic auxiliary_cooling_fan": {"speed": self.aux_fan_speed, "rpm": 0},
            "fan_generic auxiliary_cooling_fan2": {"speed": self.aux_fan_speed, "rpm": 0},
            "fan_generic chamber_circulation_fan": {
                "speed": self.circulation_fan_speed, "rpm": 0
            },
            "heater_fan hotend_fan": {"speed": 1.0, "rpm": 6100},
            "controller_fan chamber_fan": {"speed": 0.0, "rpm": 0},
            "controller_fan board_fan": {"speed": 0.6, "rpm": 2400},
            "output_pin caselight": {"value": self.caselight},
            "output_pin beeper": {"value": self.beeper},
            "output_pin Polar_cooler": {"value": self.polar_cooler},
            "ai_func_manager": {
                "enable_noodle": dict(self.ai_detect["noodle"]),
                "enable_foreign": dict(self.ai_detect["foreign"]),
                "ai_state": {"error_type": self.ai_error_type, "counts": self.ai_counts},
            },
            "filament_switch_sensor filament_switch_sensor": {
                "filament_detected": self.filament_detected,
                "enabled": self.filament_sensor_enabled,
            },
            "virtual_sdcard": {
                "file_path": self.print_filename,
                "progress": self.progress,
                "is_active": self.print_state == "printing",
                "file_position": int(self.progress * 2_483_910),
                "file_size": 2_483_910,
            },
            "motion_report": {
                "live_position": list(self.position),
                "live_velocity": 0.0,
                "live_extruder_velocity": 0.0,
            },
            "bed_mesh": {
                "profile_name": self.active_mesh,
                "mesh_min": [15.0, 15.0],
                "mesh_max": [375.0, 375.0],
                "probed_matrix": [
                    [-0.46, -0.48, -0.50], [-0.33, -0.32, -0.34], [0.63, 0.64, 0.61]
                ],
                "mesh_matrix": [[-0.46, -0.48], [-0.33, -0.32]],
                "profiles": self.bed_mesh_profiles,
            },
            "probe": {
                "last_query": False, "last_z_result": -0.05,
                "name": "probe_air", "z_offset": -0.05,
            },
            "exclude_object": {"objects": [], "excluded_objects": [], "current_object": None},
            "save_variables": {"variables": self.saved_variables},
            **(
                {"box_extras": dict(self.box_extras)}
                if self.box_extras is not None
                else {}
            ),
            **(
                {"filament_motion_sensor box_motion_sensor": dict(self.box_motion_sensor)}
                if self.box_motion_sensor is not None
                else {}
            ),
            "system_stats": {"sysload": 0.42, "cputime": 1200.0, "memavail": 1_400_000},
            "mcu": {
                "mcu_version": KLIPPER_VERSION,
                "mcu_build_versions": "gcc: 9.2.1 binutils: 2.34",
                "last_stats": {
                    "mcu_awake": 0.4, "mcu_task_avg": 0.000004,
                    "mcu_task_stddev": 0.000002, "bytes_write": 100_000,
                    "bytes_read": 200_000, "bytes_retransmit": 0, "freq": 400_000_000,
                },
            },
            "configfile": {"settings": CONFIG_SETTINGS, "save_config_pending": False},
            "z_tilt": {"applied": True},
            "screws_tilt_adjust": {"error": False, "results": {}},
            "manual_probe": {"is_active": False, "z_position": None},
            "idle_timeout ": {},
        }


def build_app(printer: MockPrinter, *, qidi_fork: bool = True) -> web.Application:
    """Build the mock app.

    ``qidi_fork=False`` emulates *upstream* Moonraker, which lacks QIDI's
    breakheater/breakmacro/power_off_print/list_endpoints routes. Tools that
    depend on them must degrade to a clear "not supported" rather than
    appearing to work.
    """
    app = web.Application()

    def result(data: Any, status: int = 200) -> web.Response:
        return web.json_response({"result": data}, status=status)

    def error(code: int, message: str) -> web.Response:
        return web.json_response({"error": {"code": code, "message": message}}, status=code)

    def klippy_guard() -> web.Response | None:
        # fail_queries_with_503 models the genuinely ambiguous case: Moonraker
        # answers 503 "Klippy Host not connected" because ITS socket to Klipper is
        # gone (Moonraker restarted, or a proxy hiccuped) while Klipper carries on
        # printing. /server/info keeps reporting the real klippy_state.
        if printer.fail_queries_with_503 or printer.klippy_state != "ready":
            return error(503, "Klippy Host not connected")
        return None

    # -- server / printer info ---------------------------------------------

    async def server_info(_: web.Request) -> web.Response:
        return result(
            {
                "klippy_connected": printer.klippy_state == "ready",
                "klippy_state": printer.klippy_state,
                "moonraker_version": MOONRAKER_VERSION,
                # The embedded Max4 Moonraker reports API version 1.4.0.
                "api_version": [1, 4, 0],
                "api_version_string": "1.4.0",
                # QIDI's fork: no sensor/spoolman/simplyprint/analysis/update_manager.
                "components": [
                    "secrets", "database", "file_manager", "klippy_apis", "machine",
                    "shell_command", "data_store", "proc_stats", "job_state",
                    "job_queue", "history", "octoprint_compat", "authorization",
                    "extensions", "announcements", "webcam", "timelapse",
                ],
                "failed_components": [],
                "warnings": [],
                "registered_directories": ["gcodes", "config", "logs", "timelapse"],
            }
        )

    async def printer_info(_: web.Request) -> web.Response:
        return result(
            {
                "state": printer.klippy_state,
                "state_message": printer.klippy_message or "Printer is ready",
                "hostname": "qidi-max4",
                "software_version": KLIPPER_VERSION,
                "cpu_info": "4 core ARM Cortex-A53",
                "klipper_path": "/home/qidi/klipper",
                "python_version": "3.9.2",
            }
        )

    async def objects_list(_: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        return result({"objects": OBJECT_LIST + printer.extra_objects})

    async def objects_query(request: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        full = {**printer.status(), **printer.extra_status}
        requested: dict[str, list[str] | None] = {}
        if request.method == "POST":
            body = await request.json()
            requested = body.get("objects", {}) or {}
        else:
            for key, value in request.query.items():
                requested[key] = value.split(",") if value else None
        out: dict[str, Any] = {}
        for name, fields in requested.items():
            if name not in full:
                continue
            obj = full[name]
            out[name] = (
                {f: obj[f] for f in fields if f in obj}
                if isinstance(fields, list) and fields
                else obj
            )
        return result({"eventtime": 1_756_500_000.0, "status": out})

    async def gcode_script(request: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        script = request.query.get("script") or ""
        if not script and request.can_read_body:
            try:
                script = (await request.json()).get("script", "")
            except (json.JSONDecodeError, ValueError):
                script = ""
        printer.commands.append(script)
        for raw in script.splitlines():
            line = raw.strip()
            if line:
                printer.log(line, kind="command")
                _apply(printer, line)
        return result("ok")

    async def gcode_help(_: web.Request) -> web.Response:
        return result({**GCODE_HELP, **printer.extra_help})

    async def emergency_stop(_: web.Request) -> web.Response:
        printer.emergency_stops += 1
        printer.klippy_state = "shutdown"
        printer.klippy_message = "Shutdown due to emergency stop"
        printer.extruder_target = printer.bed_target = printer.chamber_target = 0.0
        printer.print_state = "error" if printer.print_state == "printing" else printer.print_state
        return result("ok")

    async def firmware_restart(_: web.Request) -> web.Response:
        printer.firmware_restarts += 1
        printer.klippy_state = "ready"
        printer.klippy_message = ""
        printer.homed_axes = ""
        return result("ok")

    async def host_restart(_: web.Request) -> web.Response:
        printer.host_restarts += 1
        printer.klippy_state = "ready"
        return result("ok")

    async def list_endpoints(_: web.Request) -> web.Response:
        """QIDI fork only."""
        return result({"endpoints": ["gcode/script", "objects/query", "emergency_stop",
                                     "breakheater", "breakmacro"]})

    async def break_heater(_: web.Request) -> web.Response:
        """QIDI fork only: release a blocking heater wait."""
        if (guard := klippy_guard()) is not None:
            return guard
        printer.break_heaters += 1
        return result("ok")

    async def break_macro(_: web.Request) -> web.Response:
        """QIDI fork only: abort a running macro."""
        if (guard := klippy_guard()) is not None:
            return guard
        printer.break_macros += 1
        return result("ok")

    async def power_loss_resume(_: web.Request) -> web.Response:
        """QIDI fork only: resume the print interrupted by a power cut."""
        if (guard := klippy_guard()) is not None:
            return guard
        printer.power_loss_resumes += 1
        printer.print_state = "printing"
        return result("ok")

    async def query_endstops(_: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        return result({"x": "open", "y": "open", "z": "TRIGGERED"})

    # -- print -------------------------------------------------------------

    async def print_start(request: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        filename = request.query.get("filename", "")
        if filename not in printer.files:
            return error(404, f"File {filename} not found")
        printer.print_filename = filename
        printer.print_state = "printing"
        printer.progress = 0.01
        printer.history.append({
            "job_id": f"{len(printer.history) + 1:06X}", "filename": filename,
            "status": "in_progress", "start_time": time.time(), "end_time": None,
            "print_duration": 0.0, "total_duration": 0.0, "filament_used": 0.0,
            "exists": True,
        })
        return result("ok")

    async def print_pause(_: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        printer.print_state = "paused"
        return result("ok")

    async def print_resume(_: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        printer.print_state = "printing"
        return result("ok")

    async def print_cancel(_: web.Request) -> web.Response:
        if (guard := klippy_guard()) is not None:
            return guard
        printer.print_state = "cancelled"
        printer.progress = 0.0
        return result("ok")

    # -- files -------------------------------------------------------------

    async def files_list(request: web.Request) -> web.Response:
        root = request.query.get("root", "gcodes")
        if root != "gcodes":
            return result([])
        return result(list(printer.files.values()))

    async def files_roots(_: web.Request) -> web.Response:
        return result(
            [
                {"name": "gcodes", "path": "/home/qidi/printer_data/gcodes", "permissions": "rw"},
                {"name": "config", "path": "/home/qidi/printer_data/config", "permissions": "rw"},
                {"name": "logs", "path": "/home/qidi/printer_data/logs", "permissions": "r"},
                {"name": "timelapse", "path": "/home/qidi/printer_data/timelapse", "permissions": "rw"},
            ]
        )

    async def files_metadata(request: web.Request) -> web.Response:
        filename = request.query.get("filename", "")
        meta = printer.files.get(filename)
        if meta is None or filename in printer.unparsed:
            return error(404, f"Metadata not available for <{filename}>")
        return result({**meta, "filename": filename})

    async def files_directory(request: web.Request) -> web.Response:
        if request.method == "GET":
            path = request.query.get("path", "gcodes").strip("/")
            root, _, rel = path.partition("/")
            if root != "gcodes":
                return result({"dirs": [], "files": [], "root_info": {"name": root}})
            # A directory lists its *immediate* files, and asking for one that does
            # not exist is an error -- upstream Moonraker raises it with its default
            # status, 400 -- which a file-existence check built on listings must
            # read as "absent", not as a failure.
            known_dirs = {"", "projects"} | {
                "/".join(n.split("/")[:i]) for n in printer.files for i in range(1, n.count("/") + 1)
            }
            if rel not in known_dirs:
                return error(400, f"Directory does not exist ({path})")
            return result(
                {
                    "dirs": [
                        {
                            "modified": 1_756_400_000.0, "size": 4096,
                            "permissions": "rw", "dirname": "projects",
                        }
                    ] if path == "gcodes" else [],
                    "files": [
                        {
                            "modified": m["modified"], "size": m["size"],
                            "permissions": "rw", "filename": name.split("/")[-1],
                        }
                        for name, m in printer.files.items()
                        if name.rpartition("/")[0] == rel
                    ],
                    "disk_usage": {"total": 29_000_000_000, "used": 4_000_000_000, "free": 25_000_000_000},
                    "root_info": {"name": "gcodes", "permissions": "rw"},
                }
            )
        body = await request.json() if request.can_read_body else {}
        path = body.get("path") or request.query.get("path", "")
        return result({"item": {"path": path, "root": "gcodes"}, "action": "create_dir"})

    async def files_directory_delete(request: web.Request) -> web.Response:
        path = request.query.get("path", "")
        printer.deleted.append(path)
        return result({"item": {"path": path, "root": "gcodes"}, "action": "delete_dir"})

    async def files_upload(request: web.Request) -> web.Response:
        reader = await request.multipart()
        filename = "unknown.gcode"
        content = b""
        fields: dict[str, str] = {}
        async for part in reader:
            if part.name == "file":
                filename = part.filename or filename
                content = await part.read(decode=False)
            else:
                fields[part.name or "?"] = (await part.read()).decode()
        path = fields.get("path", "")
        full = f"{path}/{filename}" if path else filename
        if printer.fail_uploads:
            return error(500, "Internal server error while writing the file")
        printer.uploads.append({"filename": full, "size": len(content), "fields": fields})
        # Moonraker parses the slicer header on upload; derive the same keys from
        # the comment lines a slicer writes, so a queued file's metadata on the
        # "printer" reflects what was actually uploaded. Object processing, when a
        # test asks for it, rewrites the file (so size and mtime change) as the
        # real server does when exclude_object is enabled.
        size = len(content) + (64 if printer.process_objects else 0)
        modified = 1_756_500_000.0 + (1.0 if printer.process_objects else 0.0)
        printer.files[full] = {
            "path": full, "size": size, "modified": modified,
            "estimated_time": 600.0, "filament_total": 500.0, "slicer": "unknown",
            **_header_metadata(content),
            **(printer.upload_meta_override or {}),
        }
        started = fields.get("print") == "true"
        if started:
            printer.print_filename = full
            printer.print_state = "printing"
        # Documented: 201, and NOT wrapped in "result".
        return web.json_response(
            {
                "item": {"path": full, "root": fields.get("root", "gcodes"),
                         "modified": modified, "size": size, "permissions": "rw"},
                "print_started": started,
                "print_queued": False,
                "action": "create_file",
            },
            status=201,
        )

    async def files_move(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        src = body.get("source") or request.query.get("source", "")
        dst = body.get("dest") or request.query.get("dest", "")
        return result({"item": {"path": dst, "root": "gcodes"}, "source_item": {"path": src}, "action": "move_file"})

    async def files_copy(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        dst = body.get("dest") or request.query.get("dest", "")
        return result({"item": {"path": dst, "root": "gcodes"}, "action": "create_file"})

    async def file_get_or_delete(request: web.Request) -> web.Response:
        root = request.match_info["root"]
        path = request.match_info["path"]
        if request.method == "DELETE":
            printer.deleted.append(path)
            printer.files.pop(path, None)
            return result({"item": {"path": path, "root": root}, "action": "delete_file"})
        if root == "config" and path == "drying.conf":
            return web.Response(
                body=(
                    b";type bed chamber minutes\n"
                    b"ABS             90      60          720\n"
                    b"PLA             70      55          720\n"
                    b"PETG            90      60          720\n"
                    b";end\n"
                ),
                content_type="text/plain",
            )
        if root == "config" and path == "officiall_filas_list.cfg":
            if printer.filament_table is None:
                return error(404, f"File {path} not found")
            return web.Response(body=printer.filament_table.encode(), content_type="text/plain")
        if root == "config" and path == "printer.cfg":
            return web.Response(body=b"# MAX4 printer config\n[printer]\nkinematics:corexy\n")
        if path in printer.files:
            return web.Response(body=b"G28\nG1 X10 Y10 F3000\n" * 100)
        return error(404, f"File {path} not found")

    async def log_download(request: web.Request) -> web.Response:
        which = request.path.rsplit("/", 1)[-1]
        return web.Response(body=(f"{which} line\n" * 500).encode())

    # -- history / queue ---------------------------------------------------

    async def history_list(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 50))
        # Newest first, as Moonraker orders it by default.
        jobs = sorted(printer.history, key=lambda j: j["start_time"], reverse=True)
        return result({"count": len(jobs), "jobs": jobs[:limit]})

    async def history_totals(_: web.Request) -> web.Response:
        return result(
            {
                "job_totals": {
                    "total_jobs": 42, "total_time": 384_000.0, "total_print_time": 372_000.0,
                    "total_filament_used": 284_000.0, "longest_job": 86_400.0,
                    "longest_print": 84_000.0,
                }
            }
        )

    def queue_payload() -> dict[str, Any]:
        return {"queued_jobs": list(printer.queued_jobs), "queue_state": printer.queue_state}

    async def queue_status(_: web.Request) -> web.Response:
        return result(queue_payload())

    async def queue_post(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        names = body.get("filenames") or []
        for name in names:
            printer.queued_jobs.append(
                {
                    "filename": name,
                    "job_id": f"{printer._next_job_id:08X}",
                    "time_added": 1_756_500_000.0 + printer._next_job_id,
                    "time_in_queue": 0.0,
                }
            )
            printer._next_job_id += 1
        return result(queue_payload())

    async def queue_delete(request: web.Request) -> web.Response:
        if request.query.get("all", "").lower() in ("true", "1"):
            printer.queued_jobs.clear()
        else:
            wanted = {j for j in request.query.get("job_ids", "").split(",") if j}
            printer.queued_jobs = [
                j for j in printer.queued_jobs if j["job_id"] not in wanted
            ]
        return result(queue_payload())

    async def queue_start(_: web.Request) -> web.Response:
        printer.queue_state = "ready"
        return result(queue_payload())

    async def queue_pause(_: web.Request) -> web.Response:
        printer.queue_state = "paused"
        return result(queue_payload())

    # -- database (where the cross-agent lease lives) ----------------------

    async def database_list(_: web.Request) -> web.Response:
        return result(
            {"namespaces": sorted(printer.database), "backups": []}
        )

    async def database_item_get(request: web.Request) -> web.Response:
        ns = request.query.get("namespace", "")
        key = request.query.get("key", "")
        bucket = printer.database.get(ns, {})
        if key not in bucket:
            # Measured on a real Max4: 404 for a missing key, whether or not the
            # namespace exists. The lease code depends on telling "nobody holds
            # it" apart from a real failure, so the mock must not invent a 200.
            return error(404, f"Key '{key}' in namespace '{ns}' not found")
        return result({"namespace": ns, "key": key, "value": bucket[key]})

    async def database_item_post(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        ns = str(body.get("namespace", ""))
        key = str(body.get("key", ""))
        if not ns or not key:
            return error(400, "namespace and key are required")
        printer.database.setdefault(ns, {})[key] = body.get("value")
        return result({"namespace": ns, "key": key, "value": body.get("value")})

    async def database_item_delete(request: web.Request) -> web.Response:
        ns = request.query.get("namespace", "")
        key = request.query.get("key", "")
        bucket = printer.database.get(ns, {})
        if key not in bucket:
            return error(404, f"Key '{key}' in namespace '{ns}' not found")
        value = bucket.pop(key)
        # A real Moonraker drops an empty third-party namespace.
        if not bucket and ns not in ("webcams", "moonraker", "gcode_metadata", "fluidd", "announcements"):
            printer.database.pop(ns, None)
        return result({"namespace": ns, "key": key, "value": value})

    # -- machine -----------------------------------------------------------

    async def system_info(_: web.Request) -> web.Response:
        return result(
            {
                "system_info": {
                    # Real Max4 platform: 4x Cortex-A35 on a Rockchip SoC,
                    # Debian 11 Bullseye, kernel 5.10.160 aarch64.
                    "cpu_info": {
                        "cpu_count": 4, "processor": "aarch64", "model": "ARM Cortex-A35",
                        "total_memory": 1_998_308, "memory_units": "kB",
                        "bits": "64bit",
                    },
                    "distribution": {
                        "name": "Debian GNU/Linux 11 (bullseye)", "id": "debian",
                        "version": "11.8", "codename": "bullseye",
                    },
                    "kernel_version": "5.10.160",
                    "python": {"version_string": "3.9.2"},
                    "available_services": ["klipper", "moonraker", "crowsnest", "KlipperScreen"],
                    "instance_ids": {"moonraker": "moonraker", "klipper": "klipper"},
                    "virtualization": {"virt_type": "none", "virt_identifier": "none"},
                }
            }
        )

    async def proc_stats(_: web.Request) -> web.Response:
        return result(
            {
                "moonraker_stats": [
                    {"time": 1_756_500_000.0, "cpu_usage": 3.2, "memory": 42_000, "mem_units": "kB"}
                ],
                "throttled_state": {"bits": 0, "flags": []},
                "cpu_temp": 48.2,
                "system_cpu_usage": {"cpu": 12.4},
                "system_uptime": 84_000.0,
                "websocket_connections": 2,
            }
        )

    async def machine_reboot(_: web.Request) -> web.Response:
        printer.reboots += 1
        return result("ok")

    async def machine_shutdown(_: web.Request) -> web.Response:
        printer.shutdowns += 1
        return result("ok")

    async def service_action(request: web.Request) -> web.Response:
        action = request.match_info["action"]
        body = await request.json() if request.can_read_body else {}
        service = body.get("service") or request.query.get("service", "")
        printer.service_actions.append((action, service))
        return result("ok")

    # -- misc --------------------------------------------------------------

    async def temperature_store(_: web.Request) -> web.Response:
        return result(
            {
                "extruder": {
                    "temperatures": [24.0, 24.2, 24.5], "targets": [0.0, 0.0, 0.0],
                    "powers": [0.0, 0.0, 0.0],
                },
                "heater_bed": {
                    "temperatures": [23.5, 23.6, 23.8], "targets": [0.0, 0.0, 0.0],
                    "powers": [0.0, 0.0, 0.0],
                },
                "chamber": {"temperatures": [25.0, 25.1, 25.1], "targets": [0.0, 0.0, 0.0]},
            }
        )

    async def gcode_store(request: web.Request) -> web.Response:
        count = int(request.query.get("count", 100))
        return result({"gcode_store": printer.gcode_log[-count:]})

    async def server_config(_: web.Request) -> web.Response:
        config: dict[str, Any] = {"server": {"host": "0.0.0.0", "port": 7125}}
        if printer.queue_automatic_transition is not None:
            config["job_queue"] = {"automatic_transition": printer.queue_automatic_transition}
        return result({"config": config})

    async def webcams_list(_: web.Request) -> web.Response:
        return result(
            {
                "webcams": [
                    {
                        "name": "cam 1", "location": "printer", "service": "mjpegstreamer",
                        "stream_url": "/webcam/?action=stream",
                        "snapshot_url": "/webcam/?action=snapshot",
                        "enabled": True,
                    }
                ]
            }
        )

    async def webcam_snapshot(_: web.Request) -> web.Response:
        # Smallest valid JPEG-ish payload with the right magic bytes.
        return web.Response(body=b"\xff\xd8\xff\xe0" + b"\x00" * 128, content_type="image/jpeg")

    async def webcam_snapshot_dark(_: web.Request) -> web.Response:
        # A structurally valid JPEG that declares 1920x1080 but carries ~2 KB of
        # payload: 0.001 bytes/pixel, i.e. what an unlit chamber actually returns.
        return web.Response(body=dark_jpeg(), content_type="image/jpeg")

    async def not_found(request: web.Request) -> web.Response:
        return error(404, f"Not Found: {request.path}")

    r = app.router
    r.add_get("/server/info", server_info)
    r.add_get("/server/config", server_config)
    r.add_get("/printer/info", printer_info)
    r.add_get("/printer/objects/list", objects_list)
    r.add_get("/printer/objects/query", objects_query)
    r.add_post("/printer/objects/query", objects_query)
    r.add_post("/printer/gcode/script", gcode_script)
    r.add_get("/printer/gcode/help", gcode_help)
    r.add_post("/printer/emergency_stop", emergency_stop)
    r.add_post("/printer/firmware_restart", firmware_restart)
    r.add_post("/printer/restart", host_restart)
    r.add_get("/printer/query_endstops/status", query_endstops)
    # QIDI fork extensions (absent from upstream Moonraker):
    if qidi_fork:
        r.add_get("/printer/list_endpoints", list_endpoints)
        r.add_post("/printer/breakheater", break_heater)
        r.add_post("/printer/breakmacro", break_macro)
        r.add_post("/printer/print/power_off_print", power_loss_resume)
        # Capability discovery detects these POST-only routes by GETting them and
        # reading 405-vs-404, because they are Moonraker-fork routes and so never
        # appear in Klipper's /printer/list_endpoints. A real Max4 answers 405
        # here (measured), but aiohttp would answer 404 for a POST-only route, so
        # register explicit GET handlers to reproduce the firmware's behaviour.
        # Without this the mock cannot exercise the detection path at all.
        async def _method_not_allowed(_request: web.Request) -> web.Response:
            return web.json_response({"error": "Method Not Allowed"}, status=405)

        r.add_get("/printer/breakheater", _method_not_allowed)
        r.add_get("/printer/breakmacro", _method_not_allowed)
        r.add_get("/printer/print/power_off_print", _method_not_allowed)
    r.add_post("/printer/print/start", print_start)
    r.add_post("/printer/print/pause", print_pause)
    r.add_post("/printer/print/resume", print_resume)
    r.add_post("/printer/print/cancel", print_cancel)
    r.add_get("/server/files/list", files_list)
    r.add_get("/server/files/roots", files_roots)
    r.add_get("/server/files/metadata", files_metadata)
    r.add_get("/server/files/directory", files_directory)
    r.add_post("/server/files/directory", files_directory)
    r.add_delete("/server/files/directory", files_directory_delete)
    r.add_post("/server/files/upload", files_upload)
    r.add_post("/server/files/move", files_move)
    r.add_post("/server/files/copy", files_copy)
    r.add_get("/server/files/klippy.log", log_download)
    r.add_get("/server/files/moonraker.log", log_download)
    r.add_get(r"/server/files/{root}/{path:.+}", file_get_or_delete)
    r.add_delete(r"/server/files/{root}/{path:.+}", file_get_or_delete)
    r.add_get("/server/history/list", history_list)
    r.add_get("/server/history/totals", history_totals)
    r.add_get("/server/job_queue/status", queue_status)
    r.add_post("/server/job_queue/job", queue_post)
    r.add_delete("/server/job_queue/job", queue_delete)
    r.add_post("/server/job_queue/pause", queue_pause)
    r.add_post("/server/job_queue/start", queue_start)
    r.add_get("/server/database/list", database_list)
    r.add_get("/server/database/item", database_item_get)
    r.add_post("/server/database/item", database_item_post)
    r.add_delete("/server/database/item", database_item_delete)
    r.add_get("/machine/system_info", system_info)
    r.add_get("/machine/proc_stats", proc_stats)
    r.add_post("/machine/reboot", machine_reboot)
    r.add_post("/machine/shutdown", machine_shutdown)
    r.add_post(r"/machine/services/{action}", service_action)
    r.add_get("/server/temperature_store", temperature_store)
    r.add_get("/server/gcode_store", gcode_store)
    r.add_get("/server/webcams/list", webcams_list)
    r.add_get("/webcam/", webcam_snapshot)
    r.add_get("/darkcam/", webcam_snapshot_dark)
    r.add_get("/", webcam_snapshot)  # crowsnest-style ?action=snapshot
    app.router.add_route("*", "/{tail:.*}", not_found)
    return app


def _apply(printer: MockPrinter, line: str) -> None:
    """Mutate mock state the way Klipper would for the commands we care about."""
    upper = line.upper()
    token = upper.split()[0] if upper.split() else ""

    def param(letter: str) -> float | None:
        for part in line.split()[1:]:
            if part.upper().startswith(letter.upper()):
                try:
                    return float(part[len(letter):].split("=")[-1])
                except ValueError:
                    return None
        return None

    def kv(key: str) -> str | None:
        for part in line.split()[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                if k.upper() == key.upper():
                    return v
        return None

    if token == "TURN_OFF_HEATERS":
        # Core Klipper: turns off every configured heater, generics included.
        printer.extruder_target = 0.0
        printer.bed_target = 0.0
        printer.chamber_target = 0.0
    elif token in {"M104", "M104.6245197"}:
        printer.extruder_target = param("S") or 0.0
    elif token in {"M140", "M140.6245197"}:
        printer.bed_target = param("S") or 0.0
    elif token == "M141":
        printer.chamber_target = param("S") or 0.0
    elif token == "M106":
        s = param("S")
        printer.fan_speed = (s / 255.0) if s is not None else 1.0
    elif token == "M107":
        printer.fan_speed = 0.0
    elif token == "G28":
        axes = "".join(a for a in "XYZ" if a in upper[3:])
        printer.homed_axes = (axes or "XYZ").lower()
        printer.position = [0.0, 0.0, 10.0, 0.0]
    elif token == "M84" or token == "M18":
        printer.homed_axes = ""
    elif token == "M220":
        printer.speed_factor = (param("S") or 100.0) / 100.0
    elif token == "M221":
        printer.extrude_factor = (param("S") or 100.0) / 100.0
    elif token == "G1":
        for i, axis in enumerate("XYZ"):
            val = param(axis)
            if val is not None:
                printer.position[i] = val
    elif token == "SET_FAN_SPEED":
        fan, speed = kv("FAN"), kv("SPEED")
        if fan and speed:
            try:
                value = float(speed)
            except ValueError:
                return
            if fan == "chamber_circulation_fan":
                printer.circulation_fan_speed = value
            elif fan.startswith("auxiliary"):
                printer.aux_fan_speed = value
            else:
                printer.fan_speed = value
    elif token == "SET_PIN":
        pin, value = kv("PIN"), kv("VALUE")
        if pin and value is not None:
            try:
                v = float(value)
            except ValueError:
                return
            if pin == "caselight":
                printer.caselight = v
            elif pin == "beeper":
                printer.beeper = v
            elif pin == "Polar_cooler":
                printer.polar_cooler = v
    elif token in ("ENABLE_AI_DETECT", "SET_AI_SENSITIVITY"):
        kind = (kv("TYPE") or "").strip().lower()
        if kind in printer.ai_detect:
            if token == "ENABLE_AI_DETECT":
                state = kv("STATE")
                if state is not None:
                    with contextlib.suppress(ValueError):
                        printer.ai_detect[kind]["enable"] = int(float(state))
            else:
                level = (kv("VALUE") or "").strip().upper()
                if level in ("LOW", "MEDIUM", "HIGH"):
                    printer.ai_detect[kind]["sensitivity"] = level
    elif token == "SET_AI_DETECT_RESULT":
        # Reachable only if something bypassed the deny-list; model it so a test
        # can prove a forged verdict never lands.
        kind = (kv("TYPE") or "").strip().lower()
        state = kv("STATE")
        if state is not None and str(state).strip() not in ("0", "0.0"):
            printer.ai_error_type = kind
            printer.ai_counts += 1
        else:
            printer.ai_error_type = ""
    elif token == "SAVE_VARIABLE":
        # Klipper stores the VALUE as a Python literal; the Box tools only write ints.
        name, value = kv("VARIABLE"), kv("VALUE")
        if name is not None and value is not None:
            try:
                printer.saved_variables[name] = int(value)
            except ValueError:
                printer.saved_variables[name] = value
    elif token == "DISABLE_BOX_HEATER":
        printer.box_heater_disables += 1
    elif token == "SET_GCODE_OFFSET":
        adjust, absolute = kv("Z_ADJUST"), kv("Z")
        if adjust is not None:
            printer.z_offset += float(adjust)
        elif absolute is not None:
            printer.z_offset = float(absolute)
    elif token == "SET_FILAMENT_SENSOR":
        enable = kv("ENABLE")
        if enable is not None:
            printer.filament_sensor_enabled = enable == "1"
    elif token == "BED_MESH_PROFILE":
        if (name := kv("SAVE")):
            printer.bed_mesh_profiles[name] = {}
        elif (name := kv("LOAD")):
            printer.active_mesh = name
        elif (name := kv("REMOVE")):
            printer.bed_mesh_profiles.pop(name, None)
    elif token == "BED_MESH_CLEAR":
        printer.active_mesh = ""
    elif token == "PROBE_ACCURACY":
        printer.log(
            "probe accuracy results: maximum -0.041250, minimum -0.048750, range 0.007500, "
            "average -0.045000, median -0.045000, standard deviation 0.002500"
        )
    elif token == "QUERY_PROBE":
        printer.log("probe: open")
    elif token == "PAUSE":
        printer.print_state = "paused"
    elif token == "RESUME":
        printer.print_state = "printing"
