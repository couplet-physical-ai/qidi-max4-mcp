# Printer options

> **Note.** This page is hand-maintained in this release (the internal
> `scripts/build_mkdocs.py` generator that regenerated it from a live stdio
> session is not part of this export).

Everything the QIDI Max 4 exposes, and which tool reaches it. The numbers come
from this printer's own `printer.cfg` and `moonraker.conf`
([QIDITECH/QIDI_MAX4](https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900/config/)
at the pinned revision used throughout this repo), not from the marketing sheet.

## Hardware envelope

| Axis | Range (mm) |
|---|---|
| `X` | -2 → 392 |
| `Y` | -5 → 410 |
| `Z` | -5 → 342 |

CoreXY. The negative minima are real — the toolhead parks slightly off-bed.

| Heater | Firmware max | Vendor macro clamp | This server's default |
|---|---|---|---|
| Extruder | 375 °C | 370 °C | **300 °C** (confirm above 260) |
| Bed | 125 °C | 120 °C | **120 °C** (confirm above 100) |
| Chamber | 68 °C | 65 °C | **65 °C** (confirm above 55) |

> **Firmware `max_temp` is the shutdown trip, not a target.** 375 °C is the
> temperature at which the MCU halts, not one you should ask for. QIDI's own
> macros clamp to 370, and this server defaults lower still. Raise it with
> `QIDI_MAX_EXTRUDER_TEMP` only if your hotend genuinely supports it.

## Thermal and chamber

| Capability | Detail | Tools |
|---|---|---|
| Extruder / bed | Standard PID heaters | `set_temperature`, `wait_for_temperature`, `cooldown` |
| Heated chamber | `heater_generic chamber`, max 68 °C, via `M141`/`M191` | `chamber_heater` |
| Chamber protection | `Chamber_Thermal_Protection_Sensor` | `get_temperatures` |
| Filament drying | `drying.conf` profiles exist in firmware | *not exposed* — no tool wraps the drying macros |
| Min extrude temp | **135 °C** in firmware — unusually low; the extrude interlock uses a higher floor | `extrude_filament` |

## Motion and leveling

| Capability | Detail | Tools |
|---|---|---|
| Bed mesh | 9×9 bicubic, 15–375 mm | `bed_mesh_calibrate`, `bed_mesh_profile`, `get_bed_mesh` |
| Probe | `probe_air` (QIDI-specific), z_offset −0.05 | `probe_accuracy`, `query_probe` |
| Dual Z / gantry | `z_tilt` with two Z steppers | `z_tilt_adjust`, `screws_tilt_calculate` |
| Input shaper | Configured `ei`/54.4 Hz X, `mzv`/36.2 Hz Y | `set_input_shaper`, `input_shaper_calibrate` |
| Resonance | `lis2dw` accelerometer | `input_shaper_calibrate` |
| Closed loop | X/Y closed-loop steppers (QIDI-only module) | reported by `get_capabilities` |
| Z offset | Live adjust via `M290`, ±5 mm cap | `set_z_offset` |

## Fans, lights, sensors

| Device | Names | Tools |
|---|---|---|
| Fans (8) | `cooling_fan`, `auxiliary_cooling_fan`, `auxiliary_cooling_fan2`, `chamber_circulation_fan`, `chamber_fan`, `hotend_fan`, `board_fan`, `fan` | `set_fan_speed`, `list_fans` |
| Output pins | `caselight`, `beeper`, `Polar_cooler` | `set_output_pin`, `set_case_light`, `beep` |
| LED | `RGB`, 25 addressable LEDs | `set_led` |
| Filament sensor | `filament_switch_sensor` | `query_filament_sensor`, `set_filament_sensor` |

## QIDI-specific features

These have no upstream Klipper equivalent. They were confirmed against QIDI's own
macros and its Moonraker fork, not guessed.

| Feature | Mechanism | Tools |
|---|---|---|
| Staged filament load | `M604 S<temp> T<0\|1\|2>` — heat, push, purge as separate stages | `load_filament` |
| Filament unload | `M603 S<temp>` | `unload_filament` |
| QIDI Box (4-slot filament switcher) | Up to 4 units × 4 slots; needs `box_count > 0` **and** a live `box_extras` object. **Never run against a real Box** — see [first contact](../guides/box-first-contact.md). | `get_box_status`, `load_box_slot`, `set_box_option` |
| Nozzle cleaning | `CLEAR_NOZZLE HOTEND=`, `MOVE_TO_TRASH` | `clean_nozzle` |
| Soft abort — heaters | `POST /printer/breakheater` (fork route) | `abort_heating` — ungated |
| Soft abort — macro | `POST /printer/breakmacro` | `abort_macro` — one-step confirm |
| Power-loss resume | `POST /printer/print/power_off_print` (断电续打). Despite the name it **resumes**; it does not power off. | `resume_after_power_loss` |
| AI failure detection | QIDI `algo_app` on `:9010`, reporting into Klipper via `SET_AI_DETECT_RESULT` | `get_ai_detection_status`, `set_ai_detection` |

> **Soft aborts are deliberately cheap to reach.** `abort_heating` needs no
> confirmation and `abort_macro` needs only one step. Requiring a two-step token
> to *escape* a stuck operation would be perverse, and `get_capabilities`
> advertises them so an agent does not reach for the e-stop first.

## Deliberately not exposed

| Capability | Why not |
|---|---|
| Cloud print (`fileurl`, `consumable` on the fork's start-print) | Triggers an async download from QIDI's cloud, then `G32`. The client sends only `filename`; a print should come from a file you can inspect. |
| Implicit `SAVE_CONFIG` | Never issued automatically. It rewrites `printer.cfg` and restarts Klipper — always an explicit decision. |
| `update_manager` | Not present in this firmware's `moonraker.conf`, so there is nothing to call. |

## Open-source firmware: there is none for the Max 4

Worth stating plainly, because the alternatives are easy to assume apply.

| Project | Supports |
|---|---|
| FreeDi | X-Max 3, X-Plus 3, X-Smart 3, Q1 Pro, Plus 4 — **not Max 4** |
| FreeQIDI | X-3 series only |
| OpenQ1 | Q1 Pro only |
| `Qidi-Max-4-Optimized` | A macro/slicer overlay, not firmware |
| Kalico port | No verified Max 4 port exists |

The stock platform, measured: Debian 11.8 Bullseye, kernel 5.10.160, aarch64,
4× ARM Cortex-A35 on a Rockchip SoC, Moonraker API 1.4.0, Klipper closest to
upstream v0.13.0 with QIDI patches. Separate mainboard and toolhead MCUs
(toolhead on `/dev/ttyS4` at 500000 baud).
