"""Runtime capability discovery.

The whole point: **never assume**. QIDI's Moonraker fork drops components
(``sensor``, ``spoolman``, ``simplyprint``, ``analysis``, ``td1``), stock config
omits ``[update_manager]``, and QIDI's Klipper fork *adds* private modules that
upstream has never heard of (``probe_air``, ``closed_loop``, ``cl_interface``,
``multi_color_controller``, ``print_stats_manager``, ``d_bus_service_manager``).
A firmware update can change any of this.

So on first use we ask the printer what it has — Moonraker's registered
components, Klipper's object list, and the live ``configfile.settings`` — then
derive the real heaters, fans, sensors, pins, LEDs, macros and limits from
that. Tools consult this snapshot instead of hard-coding names, and the
discovered heater ceilings are intersected with the configured safety envelope
so the effective limit is always the *stricter* of the two.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .client import MoonrakerClient
from .config import Settings
from .errors import NotSupported, QidiError
from .limits import HeaterLimits, SafetyEnvelope, clamp_envelope

#: Klipper object prefixes we translate into friendly device categories.
_FAN_PREFIXES = ("fan_generic ", "heater_fan ", "controller_fan ", "temperature_fan ")


@dataclass
class Capabilities:
    """What the connected machine actually supports."""

    discovered_at: float = 0.0

    moonraker_version: str = "unknown"
    klipper_version: str = "unknown"
    hostname: str = "unknown"
    #: Moonraker components present (drives which /server/* endpoints exist).
    components: tuple[str, ...] = ()
    #: Klipper printer objects (drives which status keys exist).
    objects: tuple[str, ...] = ()
    #: Available G-code commands, from /printer/gcode/help.
    gcode_commands: tuple[str, ...] = ()
    #: gcode_macro names defined in config (upper-cased).
    macros: tuple[str, ...] = ()
    #: ``rename_existing`` targets in the running config (upper-cased): the raw
    #: built-ins a vendor macro shadows, still callable under the new name.
    renamed_commands: tuple[str, ...] = ()

    #: Heater name -> {min_temp, max_temp, object_key}
    heaters: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Controllable fans: friendly name -> object key.
    fans: dict[str, str] = field(default_factory=dict)
    #: Read-only temperature sensors: friendly name -> object key.
    temperature_sensors: dict[str, str] = field(default_factory=dict)
    #: [output_pin] names.
    output_pins: tuple[str, ...] = ()
    #: LED/neopixel names.
    leds: tuple[str, ...] = ()
    #: Filament sensor names.
    filament_sensors: tuple[str, ...] = ()

    axis_limits: dict[str, dict[str, float]] = field(default_factory=dict)
    kinematics: str = "unknown"
    #: True when [heater_generic chamber] exists (active heated chamber).
    has_heated_chamber: bool = False
    has_bed_mesh: bool = False
    has_probe: bool = False
    has_exclude_object: bool = False
    has_z_tilt: bool = False
    has_quad_gantry_level: bool = False
    has_input_shaper: bool = False
    has_resonance_tester: bool = False
    #: QIDI's ``multi_color_controller`` Klipper object. NOT proof a Box is attached:
    #: QIDI's stock printer.cfg declares it on every Max4, Box or not.
    has_multi_color: bool = False
    #: The ``box_extras`` Klipper object, which only exists once the Box config is
    #: included. QIDI's macros require it *and* box_count > 0 before acting on a Box.
    has_box_extras: bool = False
    box_count: int = 0
    #: QIDI's non-upstream Klipper modules found on this machine.
    qidi_modules: tuple[str, ...] = ()
    #: QIDI Moonraker-fork routes confirmed present (breakheater, breakmacro,
    #: power-loss resume). Probed, never assumed — a stock Moonraker lacks them.
    qidi_fork_endpoints: tuple[str, ...] = ()
    has_soft_abort: bool = False

    warnings: tuple[str, ...] = ()
    #: True when discovery could not read the device inventory or the running
    #: config, so absent hardware may simply be undiscovered. Such a snapshot is
    #: cached briefly and must never be used to conclude hardware is missing.
    degraded: bool = False

    # -- queries used by tools ---------------------------------------------

    def has_component(self, name: str) -> bool:
        return name in self.components

    def has_object(self, name: str) -> bool:
        return name in self.objects

    def has_gcode(self, cmd: str) -> bool:
        c = cmd.upper()
        return c in self.gcode_commands or c in self.macros

    def require_component(self, name: str, feature: str) -> None:
        if not self.has_component(name):
            raise NotSupported(
                f"{feature} needs Moonraker's '{name}' component, which this printer's "
                "build does not include.",
                hint="QIDI ship a trimmed Moonraker fork; see get_capabilities.",
            )

    def fan_object(self, name: str) -> str | None:
        return self.fans.get(name)

    def heater_limits(self, name: str) -> tuple[float, float] | None:
        h = self.heaters.get(name)
        if not h:
            return None
        return float(h.get("min_temp", 0.0)), float(h.get("max_temp", 0.0))

    def summary(self) -> dict[str, Any]:
        return {
            # The build answering, not the printer's firmware. An older server
            # silently does not enforce the printer lease.
            "server_version": __version__,
            "printer": {
                "hostname": self.hostname,
                "klipper_version": self.klipper_version,
                "moonraker_version": self.moonraker_version,
                "kinematics": self.kinematics,
                "axis_limits_mm": self.axis_limits,
            },
            "thermal": {
                "heaters": {
                    name: {"min_temp": h.get("min_temp"), "max_temp": h.get("max_temp")}
                    for name, h in self.heaters.items()
                },
                "heated_chamber": self.has_heated_chamber,
                "temperature_sensors": sorted(self.temperature_sensors),
            },
            "devices": {
                "controllable_fans": sorted(self.fans),
                "output_pins": sorted(self.output_pins),
                "leds": sorted(self.leds),
                "filament_sensors": sorted(self.filament_sensors),
            },
            "features": {
                "bed_mesh": self.has_bed_mesh,
                "probe": self.has_probe,
                "z_tilt": self.has_z_tilt,
                "quad_gantry_level": self.has_quad_gantry_level,
                "input_shaper": self.has_input_shaper,
                "resonance_tester": self.has_resonance_tester,
                "exclude_object": self.has_exclude_object,
                # A Box is attached only when both halves QIDI's macros test are
                # true. This used to echo the controller object alone, which every
                # Max4 declares, so it reported a Box on machines with none.
                "multi_color_controller": self.has_multi_color,
                "multi_color_box": self.box_count > 0 and self.has_box_extras,
                "box_count": self.box_count,
                "soft_abort": self.has_soft_abort,
            },
            "moonraker_components": sorted(self.components),
            "qidi_specific_klipper_modules": sorted(self.qidi_modules),
            "qidi_fork_endpoints": list(self.qidi_fork_endpoints),
            "abort_options": (
                {
                    "soft": "abort_heating / abort_macro — stop a stuck wait or macro, printer stays alive",
                    "hard": "emergency_stop — everything off, needs restart_firmware to recover",
                }
                if self.has_soft_abort
                else {
                    "soft": None,
                    "hard": "emergency_stop only (this firmware has no soft-abort routes)",
                }
            ),
            "macro_count": len(self.macros),
            "warnings": list(self.warnings),
        }


#: QIDI-only Klipper modules, so we can report them explicitly.
_QIDI_MODULE_HINTS = (
    "probe_air",
    "closed_loop",
    "cl_interface",
    "multi_color_controller",
    "print_stats_manager",
    "d_bus_service_manager",
    "box",
    "box_extras",
    "motion_accuracy_calibration",
)


class CapabilityCache:
    """Discovers capabilities once, then serves them until the TTL expires."""

    #: A discovery that could not read the object list or the running config is
    #: cached only this long. Caching a degraded snapshot for the full TTL means an
    #: inventory taken while Klipper was restarting — a very likely moment for the
    #: first call — would report the machine as having no chamber heater, no fans
    #: and no sensors for five minutes, and tools would silently skip that hardware.
    DEGRADED_TTL = 5.0

    def __init__(self, client: MoonrakerClient, settings: Settings, ttl: float = 300.0) -> None:
        self.client = client
        self.settings = settings
        self.ttl = ttl
        self._caps: Capabilities | None = None

    async def get(self, *, refresh: bool = False) -> Capabilities:
        ttl = self.DEGRADED_TTL if (self._caps is not None and self._caps.degraded) else self.ttl
        fresh = (
            self._caps is not None
            and not refresh
            and (time.monotonic() - self._caps.discovered_at) < ttl
        )
        if fresh and self._caps is not None:
            return self._caps
        self._caps = await self._discover()
        return self._caps

    def cached(self) -> Capabilities | None:
        return self._caps

    def effective_envelope(self, caps: Capabilities) -> SafetyEnvelope:
        """Intersect the configured envelope with what the firmware reports.

        The result is never wider than either source, so a printer that reports a
        lower ``max_temp`` than our default automatically tightens the gate.
        """
        env = self.settings.safety
        hw = self.settings.hardware

        def narrow(current: HeaterLimits, heater: str) -> HeaterLimits:
            reported = caps.heater_limits(heater)
            if not reported:
                return current
            lo, hi = reported
            new_max = min(current.max, hi)
            return HeaterLimits(
                min=max(current.min, max(lo, 0.0)),
                max=new_max,
                confirm_above=min(current.confirm_above, new_max),
            )

        from dataclasses import replace as _replace

        return clamp_envelope(
            _replace(
                env,
                extruder=narrow(env.extruder, "extruder"),
                bed=narrow(env.bed, "heater_bed"),
                chamber=narrow(env.chamber, "chamber"),
            ),
            hw,
        )

    # -- discovery ----------------------------------------------------------

    async def _discover(self) -> Capabilities:
        caps = Capabilities(discovered_at=time.monotonic())
        warnings: list[str] = []

        # 1. Moonraker: version + registered components.
        try:
            info = await self.client.server_info()
            caps.moonraker_version = str(info.get("moonraker_version", "unknown"))
            caps.components = tuple(info.get("components", []) or [])
            klippy_state = info.get("klippy_state")
            if klippy_state and klippy_state != "ready":
                warnings.append(f"Klipper reports state={klippy_state!r}.")
        except QidiError as exc:
            warnings.append(f"Could not read /server/info: {exc.message}")

        # 2. Klipper: version + hostname.
        try:
            pinfo = await self.client.printer_info()
            caps.klipper_version = str(pinfo.get("software_version", "unknown"))
            caps.hostname = str(pinfo.get("hostname", "unknown"))
        except QidiError as exc:
            warnings.append(f"Could not read /printer/info: {exc.message}")

        # 3. Klipper object list — the authoritative device inventory.
        try:
            caps.objects = tuple(await self.client.object_list())
        except QidiError as exc:
            warnings.append(f"Could not list printer objects: {exc.message}")
            # Without the object list we know nothing about the hardware. Flag it
            # so no caller mistakes "not discovered" for "not present".
            caps.degraded = True

        # 4. Available G-code commands and macros.
        try:
            help_map = await self.client.gcode_help()
            caps.gcode_commands = tuple(sorted(k.upper() for k in help_map))
        except QidiError as exc:
            warnings.append(f"Could not read gcode help: {exc.message}")

        caps.macros = tuple(
            sorted(o.split(" ", 1)[1].upper() for o in caps.objects if o.startswith("gcode_macro "))
        )

        # 4b. QIDI fork routes. /printer/list_endpoints is itself QIDI-only, so a
        # successful call both proves the fork is present and names what Klipper
        # registered — including the breakheater/breakmacro abort primitives that
        # let us stop a stuck operation without an emergency stop.
        # NOTE: list_endpoints reports what *Klipper* registered. QIDI's abort
        # routes are added to *Moonraker*, so they are absent from that list even
        # when present -- qualifying against a real Max4 showed this reporting
        # soft_abort=False while GET /printer/breakheater returned 405 (exists).
        # A false negative here is actively harmful: it hides the gentle abort, so
        # an agent facing a stuck heater or runaway macro reaches for
        # emergency_stop, which shuts Klipper down and needs a firmware restart.
        # So probe the Moonraker routes themselves.
        fork_routes = {
            "breakheater": "/printer/breakheater",
            "breakmacro": "/printer/breakmacro",
            # Measured on a real Max4: this one lives under /printer/print/,
            # and /printer/power_off_print is a 404.
            "power_off_print": "/printer/print/power_off_print",
        }
        present: list[str] = []
        for name, path in fork_routes.items():
            try:
                if await self.client.route_exists(path):
                    present.append(name)
            except QidiError:
                continue
        caps.qidi_fork_endpoints = tuple(sorted(present))
        caps.has_soft_abort = "breakheater" in present or "breakmacro" in present

        # 5. configfile.settings — real limits, straight from the running config.
        settings_map: dict[str, Any] = {}
        try:
            result = await self.client.query({"configfile": ["settings", "config"]})
            configfile = result.get("configfile", {}) or {}
            settings_map = configfile.get("settings", {}) or {}
            caps.renamed_commands = _renamed_commands(
                settings_map, configfile.get("config", {}) or {}
            )
        except QidiError as exc:
            warnings.append(f"Could not read configfile.settings: {exc.message}")
            # No running config means no real limits and no heater inventory.
            caps.degraded = True

        self._derive_devices(caps, settings_map)
        await self._derive_multicolor(caps, warnings)

        caps.qidi_modules = tuple(
            sorted(
                {
                    hint
                    for hint in _QIDI_MODULE_HINTS
                    for obj in caps.objects
                    if obj == hint or obj.startswith(hint + " ")
                }
            )
        )
        caps.warnings = tuple(warnings)
        return caps

    def _derive_devices(self, caps: Capabilities, cfg: dict[str, Any]) -> None:
        """Translate the object list + config settings into device inventories."""

        def cfg_section(name: str) -> dict[str, Any]:
            section = cfg.get(name) or cfg.get(name.lower()) or {}
            return section if isinstance(section, dict) else {}

        # Heaters ----------------------------------------------------------
        for obj in caps.objects:
            if obj == "extruder" or (obj.startswith("extruder") and obj[8:].isdigit()):
                sec = cfg_section(obj)
                caps.heaters[obj] = {
                    "object_key": obj,
                    "min_temp": sec.get("min_temp", 0.0),
                    "max_temp": sec.get("max_temp", 0.0),
                    "min_extrude_temp": sec.get("min_extrude_temp", 170.0),
                    "kind": "extruder",
                }
            elif obj == "heater_bed":
                sec = cfg_section("heater_bed")
                caps.heaters["heater_bed"] = {
                    "object_key": "heater_bed",
                    "min_temp": sec.get("min_temp", 0.0),
                    "max_temp": sec.get("max_temp", 0.0),
                    "kind": "bed",
                }
            elif obj.startswith("heater_generic "):
                name = obj.split(" ", 1)[1]
                sec = cfg_section(obj)
                caps.heaters[name] = {
                    "object_key": obj,
                    "min_temp": sec.get("min_temp", 0.0),
                    "max_temp": sec.get("max_temp", 0.0),
                    "kind": "generic",
                }
                if name.lower() == "chamber":
                    caps.has_heated_chamber = True

        # Fans -------------------------------------------------------------
        for obj in caps.objects:
            if obj == "fan":
                # The part-cooling fan; controlled with M106/M107, not SET_FAN_SPEED.
                caps.fans["fan"] = "fan"
            elif obj.startswith(_FAN_PREFIXES):
                kind, name = obj.split(" ", 1)
                caps.fans[name] = obj
                if kind == "temperature_fan":
                    caps.temperature_sensors.setdefault(name, obj)

        # Sensors, pins, LEDs, filament sensors ----------------------------
        for obj in caps.objects:
            if obj.startswith("temperature_sensor "):
                caps.temperature_sensors[obj.split(" ", 1)[1]] = obj

        caps.output_pins = tuple(
            sorted(o.split(" ", 1)[1] for o in caps.objects if o.startswith("output_pin "))
        )
        caps.leds = tuple(
            sorted(
                o.split(" ", 1)[1]
                for o in caps.objects
                if o.startswith(("neopixel ", "led ", "dotstar ", "pca9533 ", "pca9632 "))
            )
        )
        caps.filament_sensors = tuple(
            sorted(
                o.split(" ", 1)[1]
                for o in caps.objects
                if o.startswith(("filament_switch_sensor ", "filament_motion_sensor "))
            )
        )

        # Kinematics + axis travel ----------------------------------------
        printer_cfg = cfg_section("printer")
        caps.kinematics = str(printer_cfg.get("kinematics", "unknown"))
        for axis in ("x", "y", "z"):
            sec = cfg_section(f"stepper_{axis}")
            if sec:
                caps.axis_limits[axis] = {
                    "min": float(sec.get("position_min", 0.0) or 0.0),
                    "max": float(sec.get("position_max", 0.0) or 0.0),
                }

        # Feature flags ----------------------------------------------------
        caps.has_bed_mesh = caps.has_object("bed_mesh")
        caps.has_probe = caps.has_object("probe") or any(
            o in caps.objects for o in ("probe_air", "bltouch", "smart_effector", "beacon")
        )
        caps.has_exclude_object = caps.has_object("exclude_object")
        caps.has_z_tilt = caps.has_object("z_tilt") or "z_tilt" in cfg
        caps.has_quad_gantry_level = caps.has_object("quad_gantry_level") or "quad_gantry_level" in cfg
        caps.has_input_shaper = "input_shaper" in cfg or caps.has_gcode("SET_INPUT_SHAPER")
        caps.has_resonance_tester = "resonance_tester" in cfg or caps.has_gcode("TEST_RESONANCES")

    async def _derive_multicolor(self, caps: Capabilities, warnings: list[str]) -> None:
        """QIDI's Box (multi-material unit) state lives in save_variables."""
        caps.has_multi_color = caps.has_object("multi_color_controller")
        caps.has_box_extras = caps.has_object("box_extras")
        if not caps.has_object("save_variables"):
            return
        try:
            result = await self.client.query({"save_variables": None})
            variables = (result.get("save_variables", {}) or {}).get("variables", {}) or {}
            caps.box_count = int(variables.get("box_count", 0) or 0)
            if caps.box_count and not int(variables.get("enable_box", 0) or 0):
                warnings.append("A Box unit is present but disabled (enable_box=0).")
            if caps.box_count and not caps.has_box_extras:
                warnings.append(
                    f"save_variables.box_count is {caps.box_count} but Klipper has no "
                    "'box_extras' object, so QIDI's macros will ignore the Box "
                    "(see get_box_status)."
                )
        except (QidiError, ValueError, TypeError) as exc:
            warnings.append(f"Could not read save_variables: {exc}")


def _renamed_commands(*sections: Any) -> tuple[str, ...]:
    """Every ``rename_existing`` value in the given config maps, upper-cased."""
    found: set[str] = set()
    for cfg in sections:
        if not isinstance(cfg, dict):
            continue
        for section in cfg.values():
            if isinstance(section, dict):
                value = section.get("rename_existing")
                if isinstance(value, str) and value.strip():
                    found.add(value.strip().upper())
    return tuple(sorted(found))
