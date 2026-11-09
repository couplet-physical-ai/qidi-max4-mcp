"""Heater control: set targets, wait for them, cool down.

Safety notes specific to this module:

*   Targets are range-checked against the *effective* envelope (configured
    limits intersected with what the firmware reports) before anything is sent.
*   We deliberately drive heaters through QIDI's own ``M104``/``M140``/``M141``
    macros rather than raw ``SET_HEATER_TEMPERATURE``. Those macros apply the
    firmware's ``set_heater_scaling`` clamps, so a request passes *three*
    independent checks: ours, QIDI's macro, and Klipper's ``min_temp``/
    ``max_temp``. Defence in depth on the one subsystem that can start a fire.
*   ``wait_for_temperature`` polls rather than issuing ``M109``/``M190``. A
    blocking wait inside Klipper's G-code queue would wedge every other command
    (including, in the worst case, the agent's own attempt to stop it) for the
    duration; polling keeps the queue free and lets the agent see progress.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import InvalidRequest, NotSupported, QidiError
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

THERMAL_ANN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

#: Friendly name -> (klipper heater name, QIDI macro that applies scaling).
_HEATER_ALIASES: dict[str, tuple[str, str]] = {
    "extruder": ("extruder", "M104"),
    "hotend": ("extruder", "M104"),
    "nozzle": ("extruder", "M104"),
    "bed": ("heater_bed", "M140"),
    "heater_bed": ("heater_bed", "M140"),
    "chamber": ("chamber", "M141"),
}


def _resolve(heater: str) -> tuple[str, str, str]:
    """Return (policy_key, klipper_object_name, macro)."""
    key = heater.strip().lower()
    if key not in _HEATER_ALIASES:
        raise InvalidRequest(
            f"Unknown heater {heater!r}.",
            hint="Use 'extruder', 'bed', or 'chamber' (see get_capabilities).",
        )
    klipper_name, macro = _HEATER_ALIASES[key]
    policy_key = "bed" if klipper_name == "heater_bed" else klipper_name
    return policy_key, klipper_name, macro


def _status_key(klipper_name: str) -> str:
    return "heater_generic chamber" if klipper_name == "chamber" else klipper_name


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        name="set_temperature",
        description=(
            "Set a heater's target temperature. heater is 'extruder', 'bed', or 'chamber'; "
            "target is °C (0 turns the heater off). Returns immediately — the printer heats "
            "in the background; use wait_for_temperature to block until it arrives. "
            "Requires confirm=true. Refused if the target is outside the safety envelope, "
            "and while a print is running unless the server allows it."
        ),
        annotations=THERMAL_ANN,
    )
    @guarded
    async def set_temperature(
        heater: str,
        target: float,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        policy_key, klipper_name, macro = _resolve(heater)
        args = {"heater": heater, "target": target}

        await rt.sync_limits()
        caps = await rt.capabilities()

        # Refusing an unsupported heater is right for heating, but never for
        # cooling: if discovery was degraded (Klipper restarting when the
        # inventory was taken) the chamber looks absent, and refusing
        # target=0 would leave a ~300 W heater on with no way to turn it off.
        # "Turn it off" is always safe to attempt — the macro is a no-op on a
        # printer that genuinely lacks the hardware.
        if klipper_name == "chamber" and not caps.has_heated_chamber and target > 0:
            raise NotSupported(
                "This printer has no actively heated chamber ([heater_generic chamber])."
                + (
                    " (Capability discovery was degraded, so this may be wrong — see "
                    "get_capabilities warnings.)"
                    if caps.degraded
                    else ""
                ),
                hint="Use get_capabilities to see the heaters that exist.",
            )

        # Numeric envelope first — never send an out-of-range target at all.
        rt.policy.check_temperature(policy_key, target)

        # Turning a heater OFF is a safety-positive act: allow without confirm.
        risk = Risk.LOW if target <= 0 else Risk.THERMAL
        needs_ticket = target > 0 and rt.policy.temperature_needs_confirm(policy_key, target)

        ticket = await rt.authorize(
            "set_temperature",
            Risk.DESTRUCTIVE if needs_ticket else risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            limits = rt.settings.safety.heater(policy_key)
            ticket["why"] = (
                f"{target} °C is above the {limits.confirm_above} °C threshold for "
                f"{policy_key}, so this needs a second, explicit confirmation."
                if limits
                else "High-temperature request."
            )
            return ticket

        await rt.client.gcode(f"{macro} S{target:.0f}")
        return rt.ok(
            "set_temperature",
            risk,
            args,
            {
                "heater": policy_key,
                "target_c": target,
                "command_sent": f"{macro} S{target:.0f}",
                "note": (
                    "Heater off."
                    if target <= 0
                    else "Heating started; call wait_for_temperature to block until reached."
                ),
            },
        )

    @server.tool(
        name="wait_for_temperature",
        description=(
            "Poll until a heater reaches its target (within tolerance) or the timeout "
            "expires. Does not change any target — set_temperature does that. Safe to call "
            "repeatedly; returns the temperature trend so you can tell whether heating is "
            "actually progressing."
        ),
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    @guarded
    async def wait_for_temperature(
        heater: str,
        target: float | None = None,
        tolerance: float = 2.0,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        _, klipper_name, _ = _resolve(heater)
        key = _status_key(klipper_name)
        timeout_s = max(1.0, min(timeout_s, 1800.0))
        tolerance = max(0.5, min(tolerance, 20.0))

        started = time.monotonic()
        first: float | None = None
        last: float | None = None
        samples = 0

        while True:
            status = await rt.client.query({key: ["temperature", "target"]})
            obj = status.get(key) or {}
            current = float(obj.get("temperature", 0.0) or 0.0)
            goal = float(target if target is not None else (obj.get("target") or 0.0) or 0.0)
            if first is None:
                first = current
            last = current
            samples += 1
            elapsed = time.monotonic() - started

            if goal <= 0:
                return {
                    "ok": True,
                    "reached": True,
                    "heater": klipper_name,
                    "temperature_c": current,
                    "target_c": goal,
                    "note": "No target set for this heater; nothing to wait for.",
                    "waited_s": round(elapsed, 1),
                }
            if abs(current - goal) <= tolerance:
                return {
                    "ok": True,
                    "reached": True,
                    "heater": klipper_name,
                    "temperature_c": current,
                    "target_c": goal,
                    "waited_s": round(elapsed, 1),
                }
            if elapsed >= timeout_s:
                rate = ((last - first) / elapsed * 60) if first is not None and elapsed > 0 else 0.0
                return {
                    "ok": False,
                    "error": "timeout",
                    "reached": False,
                    "heater": klipper_name,
                    "temperature_c": current,
                    "target_c": goal,
                    "waited_s": round(elapsed, 1),
                    "trend_c_per_min": round(rate, 2),
                    "message": (
                        f"{klipper_name} reached {current:.1f} °C of {goal:.0f} °C in "
                        f"{elapsed:.0f} s."
                    ),
                    "hint": (
                        "A trend near zero suggests the heater is not actually on, or the "
                        "chamber/bed cannot reach that target."
                        if abs(rate) < 0.5
                        else "Still climbing — call again with a longer timeout_s."
                    ),
                }
            await asyncio.sleep(2.0)

    @server.tool(
        name="cooldown",
        description=(
            "Turn off every heater (extruder, bed, chamber) in one call. Always permitted — "
            "this is the safe direction — and the right thing to call when you are unsure "
            "of the machine's state or finishing a session. The QIDI Box's own heater "
            "(filament drying) is left alone unless also_box_heater=true, so a drying run "
            "is not ended by accident."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
    )
    @guarded
    async def cooldown(
        also_stop_fans: bool = False, also_box_heater: bool = False
    ) -> dict[str, Any]:
        # Deliberately does NOT consult capability discovery. This is the tool an
        # agent reaches for when it is unsure of the machine's state, which is
        # exactly when discovery is most likely to have been degraded — and a
        # degraded snapshot reports no chamber heater, silently skipping M141 S0
        # and leaving the chamber heating. TURN_OFF_HEATERS is a core Klipper
        # command that turns off every configured heater including generics, and
        # M141 S0 is harmless where no chamber exists.
        script = ["TURN_OFF_HEATERS", "M104 S0", "M140 S0", "M141 S0"]
        if also_stop_fans:
            script.append("M107")
        await rt.client.gcode("\n".join(script))

        # The Box heater is opt-in, and sent as its own call afterwards. On the Max4 it
        # is a Klipper generic heater (``heater_generic heater_box1``), which
        # TURN_OFF_HEATERS above already switches off; what this adds is the firmware's
        # own "disable Box heater" command, for whatever drying state the firmware keeps
        # beside the heater target. QIDI's published macros call it DISABLE_BOX_HEATER;
        # the first real Box (2026-10-22) had MULTI_COLOR_DISABLE_HEATER instead and no
        # DISABLE_BOX_HEATER. So ask the printer which exists and send only that, and
        # when its command list cannot be read fall back to the published name -- a
        # refusal must not undo a cooldown that has already succeeded.
        box_heater: dict[str, Any] | None = None
        if also_box_heater:
            candidates = ("DISABLE_BOX_HEATER", "MULTI_COLOR_DISABLE_HEATER")
            try:
                help_map = await rt.client.gcode_help()
            except QidiError:
                help_map = None
            help_map = help_map or None  # an empty list is a failed read, not "no commands"
            command = next((c for c in candidates if help_map is None or c in help_map), None)
            if command is None:
                box_heater = {
                    "requested": True,
                    "off": False,
                    "error": (
                        "This printer lists neither DISABLE_BOX_HEATER nor "
                        "MULTI_COLOR_DISABLE_HEATER, so no Box-heater command was sent. "
                        "heater_box1 is a generic heater, so TURN_OFF_HEATERS above should "
                        "already have set its target to 0; check get_box_status."
                    ),
                }
            else:
                try:
                    await rt.client.gcode(command)
                    script.append(command)
                    box_heater = {"requested": True, "off": True, "command": command}
                except QidiError as exc:
                    box_heater = {"requested": True, "off": False, "error": exc.message}

        return rt.ok(
            "cooldown",
            Risk.LOW,
            {"also_stop_fans": also_stop_fans, "also_box_heater": also_box_heater},
            {
                "heaters_off": ["extruder", "heater_bed", "chamber (if fitted)"],
                "box_heater": box_heater,
                "commands_sent": script,
                "note": (
                    "Sent TURN_OFF_HEATERS plus explicit zeros for every heater, without "
                    "relying on capability discovery — so this works even when the printer's "
                    "device inventory could not be read."
                ),
            },
        )

    _ = (set_temperature, wait_for_temperature, cooldown)
