"""Will the filament in the Box suit this file?  Advisory only, never a gate.

A G-code file says what the slicer assumed (``filament_type``, ``filament_colors``,
``referenced_tools``); the Box's saved variables say what the printer believes sits in
each slot. Putting the two side by side catches the expensive mistakes -- an ABS file
with PLA loaded, a black part queued with white in every slot -- before the machine is
committed for hours.

What this deliberately does *not* claim is **routing**. Which slot the firmware feeds
for slicer tool ``T1``, or for a single-colour file with no tool change, is QIDI's
decision and is not published, so the report compares materials and colours, names the
slots that could serve each filament, and says so. Because it is advisory it can never
block a call, and any input it cannot read degrades to ``unknown`` rather than ``ok``.

Pure functions only (the async glue lives in :func:`fit_for_metadata`'s callers).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .box import box_reader, slot_views
from .errors import QidiError
from .filament_names import colors_similar, nearest_color_word, norm, parse_hex

if TYPE_CHECKING:
    from .runtime import Runtime

ROUTING_NOTE = (
    "Advisory: this compares the file's materials and colours with what the Box records "
    "per slot. Which slot the printer actually feeds for each tool is decided by its "
    "firmware and the slicer's tool numbering, which is not verified here -- look at "
    "the printer before a long or multi-colour job."
)

_RANK = {"ok": 0, "unknown": 1, "check": 2, "mismatch": 3}


def _split(value: Any) -> list[str]:
    """``"PLA;PETG"`` / ``["PLA", "PETG"]`` / ``"PLA"`` -> ``["PLA", "PETG"]``."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[;,]", str(value))
    return [p.strip() for p in parts if p and p.strip()]


def _tools(meta: Mapping[str, Any], n_types: int, n_colors: int) -> list[int]:
    raw = meta.get("referenced_tools")
    tools: list[int] = []
    if isinstance(raw, (list, tuple)):
        for t in raw:
            try:
                tools.append(int(t))
            except (TypeError, ValueError):
                continue
    if tools:
        return sorted(set(tools))
    # No tool list: a one-filament file uses tool 0; otherwise assume every declared filament.
    return list(range(max(1, n_types, n_colors)))


def wanted_filaments(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the file asks for, one entry per tool it uses."""
    types = _split(meta.get("filament_type"))
    colors = [c for c in _split(meta.get("filament_colors"))]
    out = []
    for tool in _tools(meta, len(types), len(colors)):
        ftype = types[tool] if tool < len(types) else (types[0] if len(types) == 1 else None)
        color = colors[tool] if tool < len(colors) else (colors[0] if len(colors) == 1 else None)
        hex_color = color if parse_hex(color) else None
        out.append(
            {
                "tool": tool,
                "type": ftype,
                "color_hex": ("#" + hex_color.lstrip("#").upper()) if hex_color else None,
            }
        )
    return out


def assess(meta: Mapping[str, Any], slots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare one file's metadata with the Box's slots (as :func:`box.slot_views` builds them).

    ``slots`` entries need ``slot``, ``loaded``, ``label`` and, when the ID table was
    readable, ``filament`` / ``color_hex``. Returns a JSON-ready report.
    """
    wants = wanted_filaments(meta)
    loaded = [s for s in slots if s.get("loaded")]
    report: dict[str, Any] = {"wants": [], "note": ROUTING_NOTE}

    if not loaded:
        report.update(
            verdict="mismatch",
            summary="The Box reports no filament in any slot.",
        )
        report["wants"] = [{**w, "slots": [], "verdict": "mismatch"} for w in wants]
        return report

    worst = "ok"
    for w in wants:
        entry: dict[str, Any] = {**w, "slots": []}
        want_type = norm(w["type"]) if w["type"] else None
        verdict = "ok" if want_type or w["color_hex"] else "unknown"
        type_hits = 0
        for s in loaded:
            info = s.get("filament") or {}
            slot_type = info.get("type")
            type_ok = (
                None if not (want_type and slot_type) else norm(slot_type) == want_type
            )
            color_ok = colors_similar(w["color_hex"], s.get("color_hex"))
            if type_ok is False:
                continue
            type_hits += 1
            entry["slots"].append(
                {
                    "slot": s["slot"],
                    "label": s.get("label"),
                    "type_matches": type_ok,
                    "color_similar": color_ok,
                }
            )
        if want_type and not type_hits:
            verdict = "mismatch"
            have = sorted({(s.get("filament") or {}).get("type") or "?" for s in loaded})
            entry["problem"] = (
                f"the file wants {w['type']} and the Box records only {', '.join(have)}"
            )
        elif w["color_hex"] and entry["slots"]:
            judged = [s["color_similar"] for s in entry["slots"] if s["color_similar"] is not None]
            if judged and not any(judged):
                verdict = "check"
                entry["problem"] = (
                    f"the file wants {nearest_color_word(w['color_hex'])} {w['color_hex']} "
                    "and no matching-material slot holds a similar colour"
                )
        if (
            verdict == "ok"
            and want_type
            and not any(s["type_matches"] for s in entry["slots"])
        ):
            # Only slots whose material the tables could not name: that is not a match.
            verdict = "unknown"
            entry["problem"] = "the printer's filament table could not name the loaded slots"
        entry["verdict"] = verdict
        if _RANK[verdict] > _RANK[worst]:
            worst = verdict
        report["wants"].append(entry)

    report["verdict"] = worst
    problems = [e["problem"] for e in report["wants"] if e.get("problem")]
    report["summary"] = (
        "; ".join(problems)
        if problems
        else (
            "Materials and colours line up with what the Box records."
            if worst == "ok"
            else "The file does not say enough to compare."
        )
    )
    return report


async def box_slots(rt: Runtime) -> list[dict[str, Any]] | None:
    """The Box's slot views, or None when there is no Box (or it cannot be read).

    Best effort by design: the fit report is advisory, so a printer without a Box or a
    failed read must leave the caller's own result untouched.
    """
    try:
        reader = box_reader(rt)
        state = await reader.state()
        if not (state.box_count > 0 or state.extras is not None):
            return None
        table = await reader.table() if state.slot_total else None
        views = slot_views(state, table)
    except QidiError:
        return None
    return views


def fit_summary(
    meta: Mapping[str, Any], slots: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any] | None:
    """:func:`assess` when there is a Box to compare with, else None."""
    return None if slots is None else assess(meta, slots)


# ---------------------------------------------------------------------------
# The master queue's gate: may this file start on this printer unattended?
# ---------------------------------------------------------------------------
#
# Everything above is advisory because a person is pressing the button and can
# look at the machine. The master queue starts prints with nobody at the button,
# so the same comparison has to become a decision -- and a decision made from
# missing information has to be "no". The rules, in the order they are applied:
#
# * A file that uses more than one filament is never routed by this code: which
#   Box slot feeds which slicer tool is unpublished (see ROUTING_NOTE), so the
#   file is only eligible on the printer a person pinned it to, having
#   acknowledged that the fit was not verified.
# * A Box that is ready, has had its slots assigned (not the factory 1/1/1 ids)
#   and whose ID table could be read is the best evidence there is, and wins over
#   anything a person declared -- but only when every loaded slot agrees with the
#   file (see _box_feed_rule): which slot feeds is unpublished, so a Box holding
#   PETG and PLA is not evidence that a PETG file gets PETG.
# * Otherwise the operator's declared extruder filament is compared, material
#   first (required), colour second (``check`` unless an exact match is waived).
# * No Box and no declaration is ``unknown``, and unknown never starts unless the
#   submitter pinned the printer and accepted an unverified fit.
# * ``mismatch`` -- positive evidence of the wrong material -- never starts, pinned
#   or not. The acknowledgement covers "nobody checked", not "it is wrong".


def _declared_fit(
    wants: Sequence[Mapping[str, Any]], declared: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare a single-filament file with the operator's declaration (Amendment A)."""
    want = wants[0]
    want_type = norm(want["type"]) if want.get("type") else None
    have_type = declared.get("material_type") or declared.get("material")
    if not want_type:
        return {"verdict": "unknown",
                "reason": "the file does not say which material it was sliced for"}
    if not have_type:
        return {"verdict": "unknown", "reason": "the declared filament names no material"}
    if norm(have_type) != want_type:
        return {
            "verdict": "mismatch",
            "reason": f"the file wants {want['type']} and {have_type} is declared as loaded",
        }
    want_word = nearest_color_word(want.get("color_hex"))
    if want.get("color_hex"):
        similar = colors_similar(want["color_hex"], declared.get("color_hex"))
        if similar is None:
            return {
                "verdict": "check",
                "reason": f"material matches; the file wants {want_word} and the declared "
                "colour is not one the printer's palette knows",
            }
        if not similar:
            have = declared.get("color") or declared.get("color_hex")
            return {
                "verdict": "check",
                "reason": f"material matches; the file wants {want_word} {want['color_hex']} "
                f"and {have} is declared as loaded",
            }
    return {
        "verdict": "ok",
        "reason": f"{have_type} is declared as loaded"
        + (f", {declared['color']}" if declared.get("color") else ""),
    }


def _box_feed_rule(
    want: Mapping[str, Any], report: Mapping[str, Any], views: Sequence[Mapping[str, Any]],
    out: dict[str, Any],
) -> str:
    """Tighten the advisory Box verdict for an unattended start.

    ``assess`` answers "does *some* slot hold this?", which is the right question
    for a person who can look. Which slot the firmware feeds is unpublished, so
    for a start nobody watches the Box is evidence only when it cannot be read two
    ways: the file must name a material, and **every loaded slot** must hold that
    material (one loaded slot with the rest empty qualifies). A second loaded
    slot with another material makes the verdict ``unknown``; one with another
    colour makes it ``check``.
    """
    verdict = str(report["verdict"])
    if not want.get("type"):
        out["reason"] = "the file does not say which material it was sliced for"
        return "unknown"
    if verdict in {"mismatch", "unknown"}:
        return verdict
    entry = (report.get("wants") or [{}])[0]
    matched = {s["slot"]: s for s in entry.get("slots", []) if s.get("type_matches") is True}
    loaded = [s for s in views if s.get("loaded")]
    others = [s for s in loaded if s["slot"] not in matched]
    if others:
        held = ", ".join(
            f"slot {s['slot']} holds {(s.get('filament') or {}).get('type') or 'an unnamed filament'}"
            for s in others
        )
        out["reason"] = (
            f"{held}, and which slot feeds the extruder is not published, so the Box "
            f"cannot vouch for a {want['type']} print"
        )
        return "unknown"
    if want.get("color_hex") and any(s.get("color_similar") is False for s in matched.values()):
        out["reason"] = (
            f"every loaded slot holds {want['type']} but not all in the colour the file wants "
            f"({nearest_color_word(want['color_hex'])})"
        )
        return "check"
    return verdict


def dispatch_fit(
    meta: Mapping[str, Any],
    box_state: Any,
    table: Any,
    declared: Mapping[str, Any] | None,
    *,
    pinned: bool,
    allow_color_mismatch: bool = False,
    accept_unverified: bool = False,
) -> dict[str, Any]:
    """Whether a queued file may start on a printer, and why.

    ``box_state`` / ``table`` are that printer's :class:`~qidi_mcp.box.BoxState`
    and :class:`~qidi_mcp.box.FilamentTable` (either may be None); ``declared`` is
    the operator's loaded-filament record for the extruder, or None. ``pinned``
    says the submitter chose this printer by name; the two flags are what they
    accepted when they enqueued.

    Returns ``{"eligible", "verdict", "reason", "basis", "wants"}`` where ``basis``
    names the evidence used: ``box``, ``declared`` or ``none``.
    """
    from .box import all_default_ids, slot_views

    wants = wanted_filaments(meta)
    out: dict[str, Any] = {"wants": wants, "basis": "none"}

    if len(wants) > 1:
        out.update(
            verdict="unknown",
            reason=(
                f"the file uses {len(wants)} filaments and which Box slot feeds which "
                "tool is not published, so it can only run where a person pinned it"
            ),
            eligible=bool(pinned and accept_unverified),
        )
        return out

    box_ready = bool(
        box_state is not None
        and getattr(box_state, "ready", False)
        and table is not None
        and getattr(table, "filaments", None)
        and not all_default_ids(box_state)
    )
    if box_ready:
        views = slot_views(box_state, table)
        report = assess(meta, views)
        verdict = report["verdict"]
        out.update(basis="box", verdict=verdict, reason=report["summary"])
        verdict = out["verdict"] = _box_feed_rule(wants[0], report, views, out)
    elif declared:
        fit = _declared_fit(wants, declared)
        verdict = fit["verdict"]
        out.update(basis="declared", verdict=verdict, reason=fit["reason"])
    else:
        verdict = "unknown"
        out.update(
            verdict="unknown",
            reason=(
                "no usable Box and no loaded filament declared for this printer"
                if box_state is None or not getattr(box_state, "ready", False)
                else "the Box's slots have never been assigned (factory ids) or its table "
                "is unreadable, and no loaded filament is declared"
            ),
        )

    if verdict == "ok":
        eligible = True
    elif verdict == "check":
        eligible = bool(allow_color_mismatch)
        if not eligible:
            out["reason"] += " (colour differs; allow_color_mismatch was not set)"
    elif verdict == "unknown":
        eligible = bool(pinned and accept_unverified)
        if not eligible:
            out["reason"] += " (pin the printer and accept an unverified fit to run it anyway)"
    else:  # mismatch: never, whatever was acknowledged
        eligible = False
    out["eligible"] = eligible
    return out
