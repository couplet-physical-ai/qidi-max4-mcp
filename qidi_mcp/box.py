"""QIDI Box (the 4-filament switcher): state model, diagnostics and ID decoding.

This module is deliberately *not* a tool module. It holds the parts of Box support
that several tools share -- reading the Box's state, deciding whether the firmware
will actually act on it, and turning QIDI's numeric IDs into names -- so that
``get_box_status``, ``load_box_slot`` and the ``start_print`` ticket cannot disagree
about what "a Box is present" means.

What is established, and where it comes from
--------------------------------------------

Everything below is read off QIDI's published Max4 config (``QIDI's published config``), not
off a Box. **No Box has been connected to this server yet**, so anything not listed
here is unverified and is treated that way:

*   Every Box code path in QIDI's macros is gated on ``box_count > 0`` **and**
    ``printer["box_extras"]`` (a Klipper object that only exists once the Box
    config is included), and most also on ``enable_box == 1``. If either half is
    missing the firmware silently behaves as though there is no Box -- which is why
    :meth:`BoxState.problems` reports the two halves separately.
*   ``E_LOAD SLOT=<n> S=<temp>`` and ``E_UNLOAD SLOT=<n>`` are the Box's feed
    commands (``filament.cfg``). ``SLOT=16`` is the extruder pseudo-slot: the stock
    ``M604``/``M603`` macros already call them with it, so ``load_filament`` and
    ``unload_filament`` are Box-aware today.
*   Per-slot state is in ``save_variables``: ``slot0..15`` (presence flag),
    ``filament_slot0..16``, ``color_slot0..16``, ``vendor_slot0..16``, plus
    ``slot_sync`` / ``last_load_slot`` (strings like ``'slot16'``; ``'slot-1'`` for
    none) and ``runout_0..15``.
*   The three ID spaces decode through ``officiall_filas_list.cfg``: ``[filaN]``
    (name, type, temperature range), ``[colordict]`` (``#RRGGBB``) and
    ``[vendor_list]``.
*   ``box_extras.is_tool_change`` exists. Its other fields are **not published**, so
    the live object is returned verbatim rather than interpreted.

Not established, so not built: the ``T0``..``T3`` tool-change macros (the slicer's
G-code is what drives those, via the firmware, mid-print), the Box heater's *on*
command (only ``DISABLE_BOX_HEATER`` is published), and the semantics of
``E_LOAD`` for slots below 16 beyond "feed that slot". The first-contact checklist
in ``docs/guides/box-first-contact.md`` is how those get confirmed.
"""

from __future__ import annotations

import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import QidiError
from .filament_names import nearest_color_word

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime imports box lazily
    from .runtime import Runtime

#: One Box unit holds four spools. QIDI's saved variables reserve slots 0..15, i.e.
#: four units of four, plus slot 16 for the extruder.
SLOTS_PER_BOX = 4
MAX_BOXES = 4
#: ``E_LOAD``/``E_UNLOAD`` use this as "whatever is in the extruder right now".
EXTRUDER_SLOT = 16

#: Commands QIDI's published macros reference for the Box, then the ones a real Max4
#: Box actually registered when first observed (2026-10-22). Reported (not required) by
#: ``get_box_status`` so it is visible which of them this firmware exposes. The two
#: sets differ: that machine has none of the published names and answers to
#: ``MULTI_COLOR_*`` instead, so a tool built on a published name has to check first.
BOX_COMMANDS = (
    "E_LOAD",
    "E_UNLOAD",
    "INIT_BOX_STATE",
    "INIT_RFID_READ",
    "BOX_PRINT_START",
    "CLEAR_TOOLCHANGE_STATE",
    "TRY_RESUME_PRINT",
    "DISABLE_BOX_HEATER",
    "MULTI_COLOR_LOAD",
    "MULTI_COLOR_UNLOAD",
    "MULTI_COLOR_BOX_UNLOAD",
    "MULTI_COLOR_DISABLE_HEATER",
    "MULTI_COLOR_DRY",
    "MULTI_COLOR_INIT_RFID",
    "MULTI_COLOR_READ_RFID",
    "QUERY_MULTI_COLOR",
)

#: The saved variables ``set_box_option`` may write, and what flipping each does. This
#: is an allow-list: SAVE_VARIABLE is denied to raw G-code (limits.py), so the only way
#: to write one is a dedicated tool, and only these names are exposed.
#: ``auto_init_detect`` is read-only on purpose -- its effect is not published.
BOX_OPTIONS: Mapping[str, str] = {
    "enable_box": (
        "QIDI's master switch for Box handling. Off: the printer's Box branches that test "
        "it skip the Box."
    ),
    "auto_read_rfid": "Read the RFID tag on a spool when it is inserted into the Box.",
    "auto_reload_detect": (
        "On a filament runout, run AUTO_RELOAD_FILAMENT (printer.cfg's runout_gcode): the "
        "printer feeds filament by itself, unattended."
    ),
}

#: Where QIDI keeps the ID tables, relative to Moonraker's ``config`` root.
FILAMENT_TABLE_FILE = "officiall_filas_list.cfg"

_TABLE_TTL_S = 600.0


def _as_int(value: Any, default: int = 0) -> int:
    """Lenient int: save_variables are Python literals and may be str, bool or None."""
    try:
        if isinstance(value, str):
            return int(float(value.strip().strip("'\"")))
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


as_int = _as_int


def parse_slot_name(value: Any) -> int | None:
    """``'slot2'`` -> 2. ``'slot-1'`` (QIDI's "none"), junk and None -> None."""
    if not isinstance(value, str):
        return None
    text = value.strip().strip("'\"").lower()
    if not text.startswith("slot"):
        return None
    try:
        n = int(text[4:])
    except ValueError:
        return None
    return n if n >= 0 else None


# ---------------------------------------------------------------------------
# ID tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilamentTable:
    """QIDI's filament / colour / vendor ID tables from ``officiall_filas_list.cfg``."""

    filaments: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    colors: Mapping[int, str] = field(default_factory=dict)
    vendors: Mapping[int, str] = field(default_factory=dict)

    def filament(self, ident: Any) -> dict[str, Any] | None:
        entry = self.filaments.get(_as_int(ident, -1))
        return dict(entry) if entry else None

    def color(self, ident: Any) -> str | None:
        return self.colors.get(_as_int(ident, -1))

    def vendor(self, ident: Any) -> str | None:
        return self.vendors.get(_as_int(ident, -1))


def parse_filament_table(text: str) -> FilamentTable:
    """Parse QIDI's ``key   = value`` INI.

    Hand-rolled rather than ``configparser`` on purpose: the file's ``[colordict]``
    keys are bare numbers and its values start with ``#`` (``1 = #FAFAFA``), which
    configparser's comment handling would eat.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), {})
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip()

    filaments: dict[int, dict[str, Any]] = {}
    for name, body in sections.items():
        if not (name.startswith("fila") and name[4:].isdigit()):
            continue
        entry: dict[str, Any] = {
            "id": int(name[4:]),
            "name": body.get("filament"),
            "type": body.get("type"),
        }
        for key in ("min_temp", "max_temp", "box_min_temp", "box_max_temp"):
            if key in body:
                entry[key] = _as_int(body[key], 0)
        filaments[int(name[4:])] = entry

    def numbered(section: str) -> dict[int, str]:
        out: dict[int, str] = {}
        for key, value in sections.get(section, {}).items():
            if key.isdigit() and value:
                out[int(key)] = value
        return out

    return FilamentTable(
        filaments=filaments, colors=numbered("colordict"), vendors=numbered("vendor_list")
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoxState:
    """One read of everything the Box exposes, plus what the firmware would do with it."""

    variables: Mapping[str, Any]
    #: The live ``box_extras`` Klipper object, verbatim, or None when it does not exist.
    extras: Mapping[str, Any] | None = None
    #: Every ``filament_motion_sensor`` object, keyed by its full Klipper name. QIDI's
    #: published macros call theirs ``box_motion_sensor``, but the name is discovered.
    motion_sensors: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Toolhead filament switch: True when filament is sitting in the extruder path.
    toolhead_filament: bool | None = None
    controller_present: bool = False
    #: command name -> whether the printer's help list / macro list includes it.
    commands: Mapping[str, bool] = field(default_factory=dict)
    #: Set when the live Box objects could not be read, so "absent" is not proven.
    live_error: str | None = None

    # -- the numbers --------------------------------------------------------

    @property
    def box_count(self) -> int:
        return max(0, _as_int(self.variables.get("box_count"), 0))

    @property
    def units(self) -> int:
        """Units we will describe. Clamped: only four exist in the saved variables."""
        return min(self.box_count, MAX_BOXES)

    @property
    def slot_total(self) -> int:
        return self.units * SLOTS_PER_BOX

    @property
    def enabled(self) -> bool:
        return _as_int(self.variables.get("enable_box"), 0) == 1

    @property
    def active_slot(self) -> int | None:
        """The slot the firmware believes is synced to the extruder, if any."""
        return parse_slot_name(self.variables.get("slot_sync"))

    @property
    def tool_change_active(self) -> bool | None:
        if self.extras is None or "is_tool_change" not in self.extras:
            return None
        return _as_int(self.extras.get("is_tool_change"), 0) == 1

    # -- the verdicts -------------------------------------------------------

    @property
    def detected(self) -> bool:
        """Both halves QIDI's macros require: a recorded count and a live object."""
        return self.box_count > 0 and self.extras is not None

    @property
    def ready(self) -> bool:
        """``detected`` and enabled: the condition the firmware's Box branches use."""
        return self.detected and self.enabled and self.live_error is None

    def problems(self) -> list[str]:
        """Why the firmware may be ignoring the Box, in plain words.

        Each line corresponds to one clause of the gate QIDI's macros evaluate, so a
        user can see *which* clause is false instead of "Box not working".
        """
        out: list[str] = []
        if self.live_error:
            out.append(
                f"Could not read the live Box objects ({self.live_error}); "
                "'box_extras' being absent is therefore not proven."
            )
        if self.box_count > MAX_BOXES:
            out.append(
                f"save_variables.box_count is {self.box_count}; only {MAX_BOXES} units "
                f"({MAX_BOXES * SLOTS_PER_BOX} slots) exist in the saved variables."
            )
        if self.box_count > 0 and self.extras is None and self.live_error is None:
            out.append(
                f"box_count is {self.box_count} but Klipper has no 'box_extras' object. "
                "Every Box macro tests for it, so the firmware treats this printer as "
                "having no Box. A likely cause is that box.cfg still has its "
                "'[include boxN.cfg]' lines commented out, as in QIDI's stock file."
            )
        if self.box_count == 0 and self.extras is not None:
            out.append(
                "Klipper has a 'box_extras' object but save_variables.box_count is 0, so "
                "QIDI's macros skip every Box branch. The count is normally set by the "
                "Box detection at start-up; this server never writes it."
            )
        if self.detected and not self.enabled:
            out.append("The Box is detected but disabled (save_variables.enable_box is not 1).")
        if self.detected and not self.controller_present:
            out.append("The 'multi_color_controller' Klipper object is missing.")
        return out

    def load_blockers(self) -> list[str]:
        """Everything that must stop a slot load: the faults, plus a tool change under way.

        A tool change is a *state* rather than a configuration fault, so it is not in
        :meth:`problems` (which a user reads as "what is wrong with my setup").
        """
        out = self.problems()
        if self.tool_change_active:
            out.append(
                "A tool change is in progress (box_extras.is_tool_change = 1); "
                "wait for it to finish."
            )
        return out


async def read_box_state(rt: Runtime) -> BoxState:
    """Read the Box's state fresh from the printer.

    Live reads rather than the cached capability snapshot: the snapshot is up to five
    minutes old and Box state (a spool inserted, a tool change finishing) moves faster
    than that. Missing objects are omitted from a Klipper query rather than raising,
    which is also how QIDI's own ``printer["box_extras"]`` checks see them.
    """
    caps = await rt.capabilities()
    status = await rt.client.query({"save_variables": None})
    variables = (status.get("save_variables") or {}).get("variables") or {}

    # Discover the sensors rather than naming them: a motion sensor is whatever
    # [filament_motion_sensor *] this firmware declares, and the toolhead switch is
    # the printer's own [filament_switch_sensor *]. (caps.filament_sensors mixes the
    # two kinds, so it cannot say which is which.)
    switch_objects = [o for o in caps.objects if o.startswith("filament_switch_sensor ")]
    motion_objects = [o for o in caps.objects if o.startswith("filament_motion_sensor ")]
    wanted: dict[str, list[str] | None] = {"box_extras": None}
    for name in (*switch_objects[:1], *motion_objects):
        wanted[name] = None

    toolhead_filament: bool | None = None
    live_error: str | None = None
    try:
        live = await rt.client.query(wanted)
    except QidiError as exc:
        live = {}
        live_error = exc.message
    extras = live.get("box_extras")
    if switch_objects and isinstance(live.get(switch_objects[0]), dict):
        detected = live[switch_objects[0]].get("filament_detected")
        toolhead_filament = detected if isinstance(detected, bool) else None
    motion = {
        name: dict(live[name]) for name in motion_objects if isinstance(live.get(name), dict)
    }

    return BoxState(
        variables=variables,
        extras=extras if isinstance(extras, dict) else None,
        motion_sensors=motion,
        toolhead_filament=toolhead_filament,
        controller_present=caps.has_object("multi_color_controller"),
        commands={cmd: caps.has_gcode(cmd) for cmd in BOX_COMMANDS},
        live_error=live_error,
    )


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def _slot_entry(state: BoxState, table: FilamentTable | None, slot: int) -> dict[str, Any]:
    v = state.variables
    fid, cid, vid = (v.get(f"{kind}_slot{slot}") for kind in ("filament", "color", "vendor"))
    runout = v.get(f"runout_{slot}")
    entry: dict[str, Any] = {
        "slot": slot,
        "filament_id": fid,
        "color_id": cid,
        "vendor_id": vid,
    }
    if slot < EXTRUDER_SLOT:
        entry.update(
            unit=slot // SLOTS_PER_BOX + 1,
            position=slot % SLOTS_PER_BOX + 1,
            # The Box's own presence flag for the slot. Named ``loaded`` for
            # compatibility with the first release of get_box_status; it does NOT
            # mean "feeding the extruder" -- see ``in_extruder``.
            loaded=bool(_as_int(v.get(f"slot{slot}"), 0)),
            in_extruder=state.active_slot == slot,
            runout=None if runout is None else bool(_as_int(runout, 0)),
        )
    if table is not None:
        entry["filament"] = table.filament(fid)
        entry["color_hex"] = table.color(cid)
        entry["color_name"] = nearest_color_word(entry["color_hex"])
        entry["vendor"] = table.vendor(vid)
        entry["label"] = slot_label(entry)
    return entry


def slot_label(entry: Mapping[str, Any]) -> str:
    """One line a person can read: ``slot 1 (unit 1, position 2): PLA Matte, black (QIDI)``.

    ``loaded`` is the Box's own presence flag, so an empty slot says so and shows what
    was last recorded there instead of presenting stale ids as if they were a spool.
    """
    info = entry.get("filament") or {}
    what = info.get("name") or f"filament id {entry.get('filament_id')}"
    parts = [what]
    if entry.get("color_name"):
        parts.append(entry["color_name"])
    described = ", ".join(parts)
    if entry.get("vendor"):
        described += f" ({entry['vendor']})"
    if entry["slot"] >= EXTRUDER_SLOT:
        return f"extruder: {described}"
    where = f"slot {entry['slot']} (unit {entry['unit']}, position {entry['position']})"
    if not entry.get("loaded"):
        return f"{where}: empty (last recorded: {described})"
    return f"{where}: {described}"


def slot_views(state: BoxState, table: FilamentTable | None) -> list[dict[str, Any]]:
    return [_slot_entry(state, table, i) for i in range(state.slot_total)]


def extruder_view(state: BoxState, table: FilamentTable | None) -> dict[str, Any]:
    """What the printer's saved variables say is in the extruder (pseudo-slot 16)."""
    return _slot_entry(state, table, EXTRUDER_SLOT)


def unit_views(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: dict[int, list[dict[str, Any]]] = {}
    for s in slots:
        units.setdefault(s["unit"], []).append(s)
    return [{"unit": u, "slots": members} for u, members in sorted(units.items())]


def all_default_ids(state: BoxState) -> bool:
    """Every described slot still holds QIDI's factory 1/1/1 (filament/colour/vendor).

    The published ``saved_variables.cfg`` ships with all of them set to 1, which is a
    real spool (white QIDI PLA Rapido) -- so this cannot prove nothing is assigned, but
    it is the signature of a Box whose slots have never been read or set.
    """
    if not state.slot_total:
        return False
    v = state.variables
    return all(
        all(_as_int(v.get(f"{kind}_slot{i}"), -1) == 1 for kind in ("filament", "color", "vendor"))
        for i in range(state.slot_total)
    )


def brief(state: BoxState, table: FilamentTable | None) -> dict[str, Any]:
    """Compact view for tickets: which slots hold something, and what it is called."""
    loaded = []
    for s in slot_views(state, table):
        if not s["loaded"]:
            continue
        info = s.get("filament") or {}
        loaded.append(
            {
                "slot": s["slot"],
                "filament": info.get("name") or f"id {s['filament_id']}",
                "type": info.get("type"),
                "color_hex": s.get("color_hex"),
            }
        )
    return {
        "detected": state.detected,
        "ready": state.ready,
        "loaded_slots": loaded,
        "active_slot": state.active_slot,
        "ids_look_unassigned": all_default_ids(state),
    }


# ---------------------------------------------------------------------------
# Per-runtime reader (caches only the static ID table)
# ---------------------------------------------------------------------------


class BoxReader:
    """Reads Box state fresh each time, and the ID table at most every ten minutes."""

    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._table: FilamentTable | None = None
        self._table_at = 0.0

    async def state(self) -> BoxState:
        return await read_box_state(self._rt)

    async def table(self) -> FilamentTable | None:
        """The ID table, or None when the printer does not serve it.

        Failure is not cached: a missing file costs one 404, and a transient error
        should not hide names for ten minutes.
        """
        now = time.monotonic()
        if self._table is not None and now - self._table_at < _TABLE_TTL_S:
            return self._table
        try:
            raw = await self._rt.client.file_download(
                "config", FILAMENT_TABLE_FILE, max_bytes=100_000
            )
        except QidiError:
            return self._table  # a stale table beats no table
        table = parse_filament_table(raw.decode("utf-8", errors="replace"))
        if not table.filaments:
            return self._table
        self._table, self._table_at = table, now
        return table


_readers: weakref.WeakKeyDictionary[Any, BoxReader] = weakref.WeakKeyDictionary()


def box_reader(rt: Runtime) -> BoxReader:
    """The one BoxReader for this runtime, so every tool shares the table cache."""
    reader = _readers.get(rt)
    if reader is None:
        reader = _readers[rt] = BoxReader(rt)
    return reader
