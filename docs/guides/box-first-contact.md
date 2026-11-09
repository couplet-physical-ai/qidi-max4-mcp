# The QIDI Box: first contact

> **WARNING: Read paths checked on one real Box. Nothing has been written or moved.**
>
> The tools were built from [QIDI's published Max4 config](https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900/config) (optionally fetched by `bash scripts/fetch_upstream_sources.sh`) and a
> mock. On 2026-10-22 the **read-only** tools were run against a real Max4 with a
> Box (see [What the first real contact showed](#what-the-first-real-contact-showed)):
> status, slot names, the filament table, the job queue and the previews all
> worked. **No write was sent and nothing was moved**, so every `confirm=true` path
> is still unobserved, and one tool (`load_box_slot`) turned out to have nothing to
> send on that firmware. This page is the procedure for the rest, and for deciding
> which tools to trust afterwards.

The QIDI Box is the four-spool filament switcher. Up to four of them can be chained
(sixteen slots). This server treats it the way the rest of the project treats
unqualified hardware: **read first, discover what the firmware exposes, refuse
anything it cannot confirm, and write down what is still an assumption.**

## What the first real contact showed

Read-only, 2026-10-22, a Max4 with one Box (`mcu_box1`, QIDI BOX V2), reached through
Moonraker. The printer was idle and cold; no write was sent.

| What I expected | What the printer showed |
|---|---|
| `E_LOAD` / `E_UNLOAD` / `DISABLE_BOX_HEATER` registered | **None of them are** — not in `/printer/gcode/help` (316 commands). The Box answers to `MULTI_COLOR_LOAD`, `MULTI_COLOR_UNLOAD`, `MULTI_COLOR_BOX_UNLOAD`, `MULTI_COLOR_DISABLE_HEATER`, `MULTI_COLOR_DRY`, `MULTI_COLOR_READ_RFID`, `MULTI_COLOR_INIT_RFID`, `QUERY_MULTI_COLOR` and others, whose **parameters are unpublished**. So `load_box_slot` refuses and says why, instead of sending a command that does not exist |
| The Box state is in `box_extras` | `box_extras` exists but is thin. A `multi_color_controller` object carries the real state: per-slot flags, decoded materials, runout sensors, RFID, drying, the current operation and the active tool. It is a better source than saved variables and is **not read yet** |
| Slot names decode from the saved-variable ids | They do, and match the controller's own decoded `materials` exactly (`filament_slot0=1`, `color_slot0=1`, `vendor_slot0=1` → PLA Rapido, `#FAFAFA`, QIDI). This is the one assumption the real machine confirmed |
| A working Box is `enable_box = 1` | **`enable_box` is 0** although the Box is connected (`hardware.connected: true`) and three of four runout sensors report filament. The firmware's own `T0`..`T15` macros do nothing while it is 0, so the printer will not use the Box until it is enabled. `get_box_status` reports this as a problem |
| Unassigned slots look unassigned | All 17 slots are the factory 1/1/1, so three spools that are physically present are recorded as "PLA Rapido, white". `get_box_status` flags `ids_look_unassigned` |
| Moonraker's queue advances by itself unless told otherwise | `job_queue.automatic_transition` is explicitly `false` in the printer's config and the queue is `paused`: it will not start the next print on top of a finished one |
| Loose-name queueing works on the printer's files | It does, including `.gcode.3mf` files (`benchy` → `3DBenchy.gcode.3mf`, 0.48 h, PLA); `abs` lists both ABS files instead of choosing |
| `box.cfg` matches the research copy | The printer's `box.cfg` is a different generation: it defines `T0`..`T15` as `EXTRUDER_LOAD SLOT=…`, a command that is also not registered. The research copy and the live firmware have drifted apart, so published config is a hint about this machine, not a description of it |

> **NOTE: How it was reached**
>
> The devbox has no route to the printer's VLAN, so the reads went through an SSH
> port-forward via a laptop that does. Nothing was written to the printer, and
> every preview was made without `confirm`.

## What the tools do

| Tool | Tier | What it does |
|---|---|---|
| `get_box_status` | read | Per-unit slots, what the printer *believes* is in each (filament, colour and vendor decoded to names), the slot synced to the extruder, the live `box_extras` object verbatim, and a `problems` list that names the failing condition when the Box is not being used. |
| `load_box_slot` | destructive — token, maintainer | `E_LOAD SLOT=<n> S=<temp>`. Refused unless the firmware would act on the Box, the slot exists, no tool change is running, and no print is active. |
| `list_box_filament_options` | read | Searches the printer's own filament table (`officiall_filas_list.cfg`): the profiles with their temperature ranges, the 24 colour swatches with names, and the vendors. Use it to find the exact name to pass below. |
| `set_box_slot_filaments` | print tier — `confirm=true`, operator | Records what is in each slot by plain name — `[{"slot": 0, "filament": "PETG Basic", "color": "dark blue"}]` — resolved against the table above, then written as `SAVE_VARIABLE` for `filament_slotN` / `color_slotN` / `vendor_slotN`. Previews first, reads back after, refused during a print. Never writes the `slotN` presence flag, so it cannot make an empty slot look loaded. |
| `check_print_fit` | read | Compares a file's filament types and colours with what the Box records. Advisory; never blocks. |
| `queue_jobs` | print tier | Gains loose filename matching (an ambiguous or unknown name fails the whole call with candidates) and a per-file preview: print time, material, and Box fit. |
| `set_box_option` | print tier — `confirm=true`, operator | Flips `enable_box`, `auto_read_rfid` or `auto_reload_detect` in saved variables, reads the value back, and says whether it took. |
| `cooldown` | low | Gains `also_box_heater` (default off): also sends `DISABLE_BOX_HEATER`. Off by default because a drying run is hours long and ending one should be a decision. |
| `start_print` | print — token | The confirmation ticket gains a `box` section — which slots hold what — when a Box is present. |
| `get_file_metadata` | read | Now returns `filament_colors`, `filament_temps_c` and `referenced_tools`, so a multi-colour file can be compared with the Box. |
| `unload_filament` | destructive | Unchanged. QIDI's own `M603` already calls `E_UNLOAD` when a Box is present, so it is Box-aware today. |

## "A Box is present" is two conditions

QIDI's macros gate every Box branch on `box_count > 0` **and** `printer["box_extras"]`
(a Klipper object that only exists once the Box's config is included), and most also
on `enable_box == 1`. If either half is false the firmware quietly behaves as though
there is no Box — which looks exactly like "the Box does nothing". So the server
checks the halves separately and tells you which is false:

| `get_box_status` says | Meaning | What to do |
|---|---|---|
| `box_count is 1 but Klipper has no 'box_extras' object` | The count is recorded but the Box's Klipper config is not loaded. QIDI's stock `box.cfg` ships with its `[include boxN.cfg]` lines commented out. | Install the Box config per QIDI's instructions. This server never edits printer config. |
| `Klipper has a 'box_extras' object but box_count is 0` | The Box is loaded but was never registered. | QIDI's own Box detection sets the count at start-up. This server never writes `box_count`. |
| `The Box is detected but disabled` | `enable_box` is not 1. | `set_box_option option=enable_box enabled=true`. |
| `Could not read the live Box objects … not proven` | The query failed, so absence is unknown. | Retry; do not conclude there is no Box. |

A stock Max4 reports `detected: false` and an empty `slots` list. (An earlier version
listed all sixteen slots for a printer with none, and `get_capabilities` reported
`multi_color_box: true` on every Max4 because it only checked for the
`multi_color_controller` object, which QIDI's stock config declares regardless. Both
are fixed; the controller is now reported as `features.multi_color_controller`.)

## First contact, in order

Stop at the first step that surprises you and record what you saw. Steps 0–2 never
move anything.

### Step 0 — discover, read-only

1. `get_capabilities(refresh=true)`. Check `features.multi_color_box` and the
   `warnings` list.
2. `get_box_status`. Keep the whole payload, then check each of these against the
   machine in front of you:
    - `problems` — empty is the goal.
    - `commands_available` — which of `E_LOAD`, `E_UNLOAD`, `INIT_BOX_STATE`,
      `INIT_RFID_READ`, `BOX_PRINT_START`, `DISABLE_BOX_HEATER` the firmware lists. A
      command can exist without help text, so `false` is a prompt to look, not proof
      of absence — `list_gcode_commands` and `get_printer_config` show more.
    - `box_extras` and `motion_sensors` — the real field names. Only
      `is_tool_change` is published; everything else is passed through untouched.
3. Put a spool in slot 1 and call `get_box_status` again. Does `slots[0].loaded`
   flip? Does the filament / colour / vendor change, and does that depend on whether
   it is a QIDI RFID spool?
4. Compare the unit and slot numbering with the Box's own labels and the printer's
   screen. This server reports a 0-based firmware `slot` plus a 1-based `position`
   within `unit`.

### Step 1 — a reversible write

`set_box_option option=auto_read_rfid enabled=<the other value> confirm=true`, then
read `get_box_status`, then set it back. `applied: true` means the value read back
matches. This confirms `SAVE_VARIABLE` reaches the Box's state without touching a
motor.

### Step 1b — name what is in each slot

Do this once the status reads sensibly, because everything after it (the ticket's
`box` section, `check_print_fit`, the dashboard) shows these names:

1. `list_box_filament_options query="petg"` to see the exact profile names.
2. `set_box_slot_filaments assignments=[{"slot": 0, "filament": "PETG Basic", "color": "dark blue"}]`
   with **no** `confirm`. It returns the plan: the ids it would write, a before/after
   line per slot, and warnings for anything it assumed (a bare material resolved to its
   "Basic" profile, a colour snapped to the nearest swatch, a slot the Box reports
   empty, `auto_read_rfid` replacing your entry when a tagged spool is inserted).
3. Repeat with `confirm=true`. `all_applied: true` means every slot read back as
   written. **Then look at the printer's own screen.** Whether the firmware and
   touchscreen reflect these variables is exactly what is unverified; if the screen
   still shows the old filament, the write reached the saved variables but is not what
   the screen reads.

If a QIDI-tagged spool is inserted while `auto_read_rfid` is on, the printer re-reads
the tag and overwrites the slot. Turn that off with `set_box_option` if you want
hand-entered names to stick on tagged spools.

### Step 2 — read the ticket for a load

`load_box_slot slot=0 hotend_temp=<material temp> confirm=true` returns a ticket
and **sends nothing**. Read `will_send` and `warnings` (empty slot, another slot
already synced, filament already at the toolhead). On a printer that does not list
`E_LOAD` — the real one did not — the call is refused with `not_supported` before a
ticket exists, so there is no Step 3 for it until a verified command is available.

### Step 3 — one supervised load

Only with someone at the machine, the hotend over the purge chute, the bed clear,
and a hand on `emergency_stop`. Note that QIDI's own `M603` homes, moves to the
trash chute and disables the filament sensors *before* it unloads; this server does
none of that for `E_LOAD`, because nobody has published whether `E_LOAD` needs it.
Home the printer first (`home_axes`) and watch.

Then record:

- Did it block until finished, or return at once?
- Did `active_slot` / `active_slot_index` change to the slot you loaded?
- Did the toolhead filament switch trip (`toolhead_filament_detected`)?
- Did it need `load_filament stage='push'` / `'purge'` afterwards, or did the Box
  finish the job?
- What did the cutter and the purge chute do?

### Step 4 — decide

If every assumption below held, the tools are qualified: say so in
`docs/guides/going-live.md` and remove the warning at the top of this page. If any
did not, fix the code to match the machine and keep the warning.

## What the code assumes and has not checked

| Assumption | Where it comes from | Consequence if wrong |
|---|---|---|
| `E_LOAD SLOT=<0..15> S=<temp>` feeds that slot | Published macros only call it with `SLOT=16` (the extruder) | **Not registered on the one real Box seen**, so the tool refuses. If a firmware does list it, it does something else, or errors |
| `slotN` (saved variable) is truthy when slot N holds filament | Its presence in `saved_variables.cfg` and QIDI's `INIT_BOX_STATE` | `loaded` is wrong; the ticket's empty-slot warning is wrong |
| `slot_sync = 'slotN'` is the slot synced to the extruder; `'slot-1'` is none | Factory file value | `in_extruder` / the already-loaded warning are wrong |
| Unit *u* holds slots 4(*u*−1) … 4(*u*−1)+3 | Slot count 16 = 4 units × 4 | Unit and position labels are wrong |
| `filament_slotN` / `color_slotN` / `vendor_slotN` are ids into `[filaN]` / `[colordict]` / `[vendor_list]` of `officiall_filas_list.cfg` | The id ranges overlap and the factory default is 1/1/1 | Names and colours are wrong. The raw ids are always returned alongside, so the error is visible |
| `box_extras.is_tool_change == 1` means a tool change is running | `pause_resume_cancel.cfg` | `load_box_slot` blocks needlessly or not at all |
| The Box heater's off command is `DISABLE_BOX_HEATER` (or `MULTI_COLOR_DISABLE_HEATER`) | The published idle-timeout macro; the real printer lists only the second | `cooldown(also_box_heater=true)` sends whichever the printer lists, sends nothing if it lists neither, and still cools everything else. `heater_box1` is a Klipper generic heater, so `TURN_OFF_HEATERS` should already have zeroed it; not observed with the heater on |
| The firmware and touchscreen read `filament_slotN` / `color_slotN` / `vendor_slotN` when choosing temperatures or drawing the slot | **Reads confirmed, writes not**: the controller's decoded `materials` match these ids exactly, so the firmware derives its slot materials from them. Whether a *write* is picked up live, and shown on the screen, is untested | `set_box_slot_filaments` saves values the printer ignores. It reads them back, so the write is checked, but the effect on the machine is not |
| Slicer tool `Tn` feeds slot *n*, and a single-colour file uses the slot synced to the extruder | Nothing published | `check_print_fit` lists *candidate* slots by material and colour and says routing is unverified; it never claims a slot will be used |
| `automatic_transition` in `/server/config` → `job_queue` is whether the queue advances without being released | Moonraker's documented behaviour; **read live**: explicitly `false` on the real printer | The queue preview's "pauses by itself" line is wrong |
| `enable_box`, `auto_read_rfid`, `auto_reload_detect` behave as their names say | Macro usage, partly | `set_box_option` changes something else |

## Not exposed, and why

These are left out on purpose, not forgotten:

- **Per-slot unload.** `unload_filament` already runs `M603`, which unloads whatever is
  in the extruder through `E_UNLOAD SLOT=16`. Whether `E_UNLOAD SLOT=<n>` means
  anything else is unpublished.
- **Tool changes (`T0`…`T15`).** The slicer's G-code drives these mid-print through
  the firmware. A tool change on demand puts a hot nozzle, a cutter and a purge in
  motion; the semantics are not published.
- **Box heater on, and drying.** Only the *off* command (`DISABLE_BOX_HEATER`) is
  published. A community macro pack for the Max4 mentions a `TLTG_SET_BOX_TEMP` macro
  (see [community firmware sources](https://github.com/thelegendtubaguy/Qidi-Max-4-Optimized)); nothing here assumes it exists on yours.
- **Re-reading the spools** (`INIT_BOX_STATE`, `INIT_RFID_READ`). Reachable through
  `run_macro`, which is two-step confirmed and only accepts commands the printer
  lists. They are not wrapped: `auto_read_rfid` already reads a spool on insertion,
  and what a forced re-read does to the Box hardware is unpublished.
- **Setting what a slot contains.** Slot contents and `box_count` are not writable
  from here. `SAVE_VARIABLE` stays on the raw G-code deny-list; the only write path is
  `set_box_option`, which allow-lists three names.

Once Step 3 has been done and written up, `load_box_slot`'s siblings are the natural
follow-up: a refresh tool and a tool-change tool, each qualified the same way.

## Before a multi-colour print

There is no automatic check that the slicer's filament order matches the Box, because
the mapping from the slicer's tool index to a Box slot is not published. Do it by
eye:

1. `get_file_metadata` — read `referenced_tools`, `filament_type`, `filament_colors`
   and `filament_temps_c`.
2. `get_box_status` — read each occupied slot's `filament`, `color_hex` and
   `max_temp`.
3. `start_print` — the ticket's `box` section repeats the occupied slots so the two
   are side by side at the moment you approve.

`ids_look_unassigned` is true when every slot still holds QIDI's factory 1/1/1
(white QIDI PLA Rapido). That is a real spool *and* the default, so it is a prompt to
read the spool, not a verdict.
