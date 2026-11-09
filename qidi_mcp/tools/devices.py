"""Fans, LEDs, output pins, case light, beeper, display messages, filament sensors.

These are the LOW-risk tier: reversible, non-thermal, non-motion. They need no
confirmation, which is deliberate — an agent should be able to turn the light on
to take a better webcam photo, or slow a fan, without ceremony.

Two exceptions are treated more carefully:

*   Disabling a filament sensor is a real safety decision (it defeats runout
    protection), so it is gated.
*   ``set_output_pin`` can address arbitrary pins including QIDI's ``Polar_cooler``,
    so it validates against the discovered pin list rather than passing anything
    through.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import InvalidRequest, NotSupported
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

LOW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="list_fans",
        description=(
            "Every fan on the printer with its current speed and type: the part-cooling fan, "
            "the Max4's two auxiliary fans, the chamber circulation fan, and the "
            "firmware-managed hotend/board fans (which report but cannot be set)."
        ),
        annotations=READ,
    )
    @guarded
    async def list_fans() -> dict[str, Any]:
        caps = await rt.capabilities()
        objects = dict.fromkeys(caps.fans.values())
        status = await rt.client.query(objects) if objects else {}
        fans = {}
        for name, key in caps.fans.items():
            obj = status.get(key) or {}
            kind = key.split(" ", 1)[0] if " " in key else "fan"
            fans[name] = {
                "type": kind,
                "speed": obj.get("speed"),
                "rpm": obj.get("rpm"),
                "settable": kind in {"fan", "fan_generic"},
                "note": (
                    "Managed automatically by firmware (tied to a heater/stepper)."
                    if kind in {"heater_fan", "controller_fan"}
                    else None
                ),
            }
        return {"ok": True, "fans": fans}

    @server.tool(
        name="set_fan_speed",
        description=(
            "Set a fan's speed as a fraction from 0.0 to 1.0. Use fan='part' (or omit) for "
            "the part-cooling fan; other names come from list_fans (e.g. "
            "'auxiliary_cooling_fan', 'chamber_circulation_fan'). Firmware-managed fans "
            "(hotend, board) cannot be set."
        ),
        annotations=LOW,
    )
    @guarded
    async def set_fan_speed(speed: float, fan: str = "part") -> dict[str, Any]:
        speed = rt.policy.check_fan_speed(float(speed))
        caps = await rt.capabilities()
        name = fan.strip()
        args = {"fan": name, "speed": speed}

        await rt.authorize("set_fan_speed", Risk.LOW, args, interlocks=Interlocks())

        if name.lower() in {"part", "part_cooling", "fan", "cooling_fan", ""}:
            # M106 drives the part fan; QIDI map it onto their own fan_generic.
            script = f"M106 S{round(speed * 255)}" if speed > 0 else "M107"
            await rt.client.gcode(script)
            return rt.ok(
                "set_fan_speed",
                Risk.LOW,
                args,
                {"fan": "part_cooling", "speed": speed, "command_sent": script},
            )

        key = caps.fan_object(name)
        if key is None:
            raise InvalidRequest(
                f"No fan named {name!r} on this printer.",
                hint=f"Available: {sorted(caps.fans)}",
            )
        kind = key.split(" ", 1)[0]
        if kind not in {"fan_generic", "fan"}:
            raise NotSupported(
                f"{name!r} is a {kind} — the firmware controls it automatically and it "
                "cannot be set directly.",
                hint="It responds to heater/stepper activity on its own.",
            )
        script = f"SET_FAN_SPEED FAN={name} SPEED={speed:.3f}"
        await rt.client.gcode(script)
        return rt.ok(
            "set_fan_speed", Risk.LOW, args, {"fan": name, "speed": speed, "command_sent": script}
        )

    @server.tool(
        name="set_case_light",
        description=(
            "Turn the chamber/case light on or off. Handy before taking a webcam snapshot."
        ),
        annotations=LOW,
    )
    @guarded
    async def set_case_light(on: bool) -> dict[str, Any]:
        caps = await rt.capabilities()
        args = {"on": on}
        await rt.authorize("set_case_light", Risk.LOW, args, interlocks=Interlocks())
        pin = next((p for p in caps.output_pins if p.lower() == "caselight"), None)
        if pin is None:
            if caps.has_gcode("LED_ON") and caps.has_gcode("LED_OFF"):
                script = "LED_ON" if on else "LED_OFF"
            else:
                raise NotSupported(
                    "No case light output pin found on this printer.",
                    hint=f"Output pins present: {sorted(caps.output_pins)}",
                )
        else:
            script = f"SET_PIN PIN={pin} VALUE={1 if on else 0}"
        await rt.client.gcode(script)
        return rt.ok("set_case_light", Risk.LOW, args, {"light_on": on, "command_sent": script})

    @server.tool(
        name="set_output_pin",
        description=(
            "Set one of the printer's output pins by name and value (0–1). On the Max4 these "
            "are 'caselight', 'beeper', and 'Polar_cooler' (the chamber air conditioner). "
            "Use list_devices to see what exists."
        ),
        annotations=LOW,
    )
    @guarded
    async def set_output_pin(pin: str, value: float) -> dict[str, Any]:
        caps = await rt.capabilities()
        name = pin.strip()
        match = next((p for p in caps.output_pins if p.lower() == name.lower()), None)
        if match is None:
            raise InvalidRequest(
                f"No output pin named {name!r}.",
                hint=f"Available pins: {sorted(caps.output_pins)}",
            )
        if not 0.0 <= value <= 1.0:
            raise InvalidRequest("value must be between 0.0 and 1.0.")
        args = {"pin": match, "value": value}
        await rt.authorize("set_output_pin", Risk.LOW, args, interlocks=Interlocks())
        script = f"SET_PIN PIN={match} VALUE={value:g}"
        await rt.client.gcode(script)
        return rt.ok("set_output_pin", Risk.LOW, args, {"command_sent": script})

    @server.tool(
        name="set_led",
        description=(
            "Set an RGB(W) LED strip's colour. Components are 0.0–1.0. On the Max4 the strip "
            "is 'RGB' (25 LEDs); omit index to set the whole chain."
        ),
        annotations=LOW,
    )
    @guarded
    async def set_led(
        red: float = 0.0,
        green: float = 0.0,
        blue: float = 0.0,
        white: float = 0.0,
        led: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.leds:
            raise NotSupported("This printer exposes no LED/neopixel strips.")
        name = led or caps.leds[0]
        if name not in caps.leds:
            raise InvalidRequest(f"No LED named {name!r}.", hint=f"Available: {list(caps.leds)}")
        for label, val in (("red", red), ("green", green), ("blue", blue), ("white", white)):
            if not 0.0 <= val <= 1.0:
                raise InvalidRequest(f"{label} must be between 0.0 and 1.0.")
        args = {"led": name, "red": red, "green": green, "blue": blue, "white": white, "index": index}
        await rt.authorize("set_led", Risk.LOW, args, interlocks=Interlocks())
        script = (
            f"SET_LED LED={name} RED={red:.3f} GREEN={green:.3f} BLUE={blue:.3f} WHITE={white:.3f}"
        )
        if index is not None:
            script += f" INDEX={int(index)}"
        await rt.client.gcode(script)
        return rt.ok("set_led", Risk.LOW, args, {"command_sent": script})

    @server.tool(
        name="beep",
        description=(
            "Sound the printer's beeper. duration_ms is capped at 5000. Useful to get a "
            "human's attention at the machine."
        ),
        annotations=LOW,
    )
    @guarded
    async def beep(duration_ms: int = 200, repeat: int = 1) -> dict[str, Any]:
        duration = max(10, min(int(duration_ms), 5000))
        count = max(1, min(int(repeat), 10))
        caps = await rt.capabilities()
        args = {"duration_ms": duration, "repeat": count}
        await rt.authorize("beep", Risk.LOW, args, interlocks=Interlocks())
        if caps.has_gcode("M300"):
            script = "\n".join([f"M300 P{duration}"] * count)
        else:
            pin = next((p for p in caps.output_pins if p.lower() == "beeper"), None)
            if pin is None:
                raise NotSupported("This printer has no beeper.")
            lines = []
            for _ in range(count):
                lines += [f"SET_PIN PIN={pin} VALUE=1", f"G4 P{duration}", f"SET_PIN PIN={pin} VALUE=0"]
            script = "\n".join(lines)
        await rt.client.gcode(script)
        return rt.ok("beep", Risk.LOW, args, {"command_sent": script})

    @server.tool(
        name="display_message",
        description=(
            "Show a short message on the printer's screen (M117) and/or write one to the "
            "G-code console (M118), which is where a human watching Fluidd will see it."
        ),
        annotations=LOW,
    )
    @guarded
    async def display_message(message: str, to_console: bool = False) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise InvalidRequest("message cannot be empty.")
        if len(text) > 200:
            text = text[:200]
        # Newlines and ';' would break out of the command.
        safe = text.replace("\n", " ").replace("\r", " ").replace(";", ",")
        args = {"message": safe, "to_console": to_console}
        await rt.authorize("display_message", Risk.LOW, args, interlocks=Interlocks())
        script = f"M118 {safe}" if to_console else f"M117 {safe}"
        await rt.client.gcode(script)
        return rt.ok("display_message", Risk.LOW, args, {"displayed": safe})

    @server.tool(
        name="list_devices",
        description=(
            "Everything controllable or readable that is not a heater or an axis: fans, "
            "output pins, LEDs, filament sensors, and their current values. A quick inventory "
            "before poking at hardware."
        ),
        annotations=READ,
    )
    @guarded
    async def list_devices() -> dict[str, Any]:
        caps = await rt.capabilities()
        objects: dict[str, list[str] | None] = {}
        objects.update(dict.fromkeys(caps.fans.values()))
        for pin in caps.output_pins:
            objects[f"output_pin {pin}"] = None
        for name in caps.filament_sensors:
            objects[f"filament_switch_sensor {name}"] = None
        status = await rt.client.query(objects) if objects else {}
        return {
            "ok": True,
            "fans": {
                name: (status.get(key) or {}).get("speed") for name, key in caps.fans.items()
            },
            "output_pins": {
                pin: (status.get(f"output_pin {pin}") or {}).get("value")
                for pin in caps.output_pins
            },
            "leds": list(caps.leds),
            "filament_sensors": {
                name: {
                    "filament_detected": (
                        status.get(f"filament_switch_sensor {name}") or {}
                    ).get("filament_detected"),
                    "enabled": (status.get(f"filament_switch_sensor {name}") or {}).get("enabled"),
                }
                for name in caps.filament_sensors
            },
        }

    @server.tool(
        name="set_filament_sensor",
        description=(
            "Enable or disable filament runout detection. Disabling it removes the printer's "
            "protection against printing into thin air after the spool runs out — QIDI's own "
            "macros disable it briefly during filament changes. Requires confirm=true to "
            "disable; enabling is free."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    @guarded
    async def set_filament_sensor(
        enable: bool,
        sensor: str | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.filament_sensors:
            raise NotSupported("This printer has no filament sensor.")
        name = sensor or caps.filament_sensors[0]
        if name not in caps.filament_sensors:
            raise InvalidRequest(
                f"No filament sensor named {name!r}.",
                hint=f"Available: {list(caps.filament_sensors)}",
            )
        args = {"sensor": name, "enable": enable}
        # Turning protection ON is safe; turning it OFF is a decision.
        risk = Risk.LOW if enable else Risk.MOTION
        ticket = await rt.authorize(
            "set_filament_sensor",
            risk,
            args,
            confirm=confirm or enable,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            return ticket
        script = f"SET_FILAMENT_SENSOR SENSOR={name} ENABLE={1 if enable else 0}"
        await rt.client.gcode(script)
        return rt.ok(
            "set_filament_sensor",
            risk,
            args,
            {
                "sensor": name,
                "enabled": enable,
                "warning": None if enable else "Runout protection is now OFF.",
            },
        )

    @server.tool(
        name="query_filament_sensor",
        description="Whether filament is currently detected, and whether the sensor is enabled.",
        annotations=READ,
    )
    @guarded
    async def query_filament_sensor(sensor: str | None = None) -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.filament_sensors:
            raise NotSupported("This printer has no filament sensor.")
        names = [sensor] if sensor else list(caps.filament_sensors)
        objects = {f"filament_switch_sensor {n}": None for n in names if n}
        status = await rt.client.query(objects)
        return {
            "ok": True,
            "sensors": {
                key.split(" ", 1)[1]: {
                    "filament_detected": val.get("filament_detected"),
                    "enabled": val.get("enabled"),
                }
                for key, val in status.items()
                if isinstance(val, dict)
            },
        }

    _ = (
        list_fans,
        set_fan_speed,
        set_case_light,
        set_output_pin,
        set_led,
        beep,
        display_message,
        list_devices,
        set_filament_sensor,
        query_filament_sensor,
    )
