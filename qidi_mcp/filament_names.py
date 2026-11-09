"""Turn what a person says ("black PETG") into the ids QIDI's saved variables hold.

The Box stores what is in each slot as three small integers -- a filament id, a colour
id and a vendor id -- that index tables shipped on the printer
(``officiall_filas_list.cfg``, see :mod:`qidi_mcp.box`). Nobody remembers that 39 is
"PETG Basic" or that 2 is near-black, so this module does the lookup.

Two rules govern every resolver here:

* **Only the printer's own tables are targets.** A name resolves to an id that exists in
  the table the firmware reads, or it does not resolve. Nothing here invents an id.
* **Anything that was a guess says so.** A colour word is approximate by nature (the
  palette has 24 swatches), and "PLA" with no further word is several different
  profiles. Each resolution reports *how* it matched, so a caller shows the person the
  result and lets them correct it before anything is written.

Pure functions only: no I/O, so every behaviour is unit-testable without a printer.
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import InvalidRequest

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

#: Reference colours for the words people actually use. Deliberately small and
#: unremarkable: the goal is "nearest of QIDI's 24 swatches", not colour science.
COLOR_WORDS: Mapping[str, tuple[int, int, int]] = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "grey": (128, 128, 128),
    "gray": (128, 128, 128),
    "silver": (215, 225, 236),
    "light grey": (200, 200, 200),
    "light gray": (200, 200, 200),
    "dark grey": (70, 70, 75),
    "dark gray": (70, 70, 75),
    "red": (255, 40, 40),
    "orange": (245, 135, 50),
    "yellow": (225, 215, 40),
    "green": (35, 130, 50),
    "lime": (110, 245, 20),
    "yellow green": (202, 222, 75),
    "light green": (100, 230, 145),
    "dark green": (20, 90, 40),
    "mint": (100, 230, 145),
    "blue": (40, 80, 255),
    "dark blue": (23, 20, 176),
    "navy": (23, 20, 176),
    "light blue": (153, 222, 255),
    "sky blue": (94, 169, 253),
    "sky": (94, 169, 253),
    "cyan": (153, 222, 255),
    "purple": (168, 120, 255),
    "violet": (168, 120, 255),
    "lavender": (206, 192, 254),
    "pink": (254, 152, 254),
    "salmon": (254, 113, 122),
    "coral": (254, 113, 122),
    "brown": (110, 56, 18),
    "beige": (202, 197, 159),
    "tan": (202, 197, 159),
    "cream": (226, 223, 205),
    "ivory": (226, 223, 205),
    "gold": (184, 127, 43),
    "bronze": (184, 127, 43),
}

#: Spellings kept working but not advertised, so the hint lists each colour once.
_ALIASES = frozenset({"gray", "light gray", "dark gray"})

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")

#: Weighted-RGB distance under which two colours read as "the same swatch" to a person.
#: Used only for the advisory colour comparison, never to gate anything.
SIMILAR_COLOR_DISTANCE = 160.0

#: Beyond this a requested colour and the swatch chosen for it look different.
APPROXIMATE_COLOR_DISTANCE = 60.0


def parse_hex(value: Any) -> tuple[int, int, int] | None:
    """``#RRGGBB`` / ``RRGGBB`` -> (r, g, b), else None."""
    if not isinstance(value, str):
        return None
    m = _HEX.match(value.strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """"Redmean" distance: close to what an eye sees, and far cheaper than Lab.

    Plain Euclidean RGB calls a dark green and a dark blue closer than they look;
    weighting by the mean red channel fixes the worst of it.
    """
    rmean = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt(
        (2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db
    )


def nearest_color_word(hex_value: Any) -> str | None:
    """A plain-English name for a swatch: the closest word in :data:`COLOR_WORDS`."""
    rgb = parse_hex(hex_value)
    if rgb is None:
        return None
    return min(COLOR_WORDS, key=lambda w: color_distance(rgb, COLOR_WORDS[w]))


def colors_similar(a: Any, b: Any) -> bool | None:
    """Whether two ``#RRGGBB`` values read as the same colour; None if either is not one."""
    ra, rb = parse_hex(a), parse_hex(b)
    if ra is None or rb is None:
        return None
    return color_distance(ra, rb) <= SIMILAR_COLOR_DISTANCE


@dataclass(frozen=True)
class ColorChoice:
    id: int
    hex: str
    name: str | None
    matched_by: str
    exact: bool
    distance: float

    @property
    def approximate(self) -> bool:
        """Far enough from what was asked that a person should look before agreeing.

        Colour *words* are never exact (there is one "red" and the palette has a
        slightly different red), so only a visibly different swatch counts.
        """
        return self.distance > APPROXIMATE_COLOR_DISTANCE


def _palette(colors: Mapping[int, str]) -> list[tuple[int, str, tuple[int, int, int]]]:
    out = []
    for cid, hx in sorted(colors.items()):
        rgb = parse_hex(hx)
        if rgb is not None:
            out.append((cid, hx, rgb))
    return out


def palette_listing(colors: Mapping[int, str]) -> list[dict[str, Any]]:
    """Every swatch the printer offers, each with a plain-English name."""
    return [
        {"id": cid, "hex": hx, "name": nearest_color_word(hx)} for cid, hx, _ in _palette(colors)
    ]


def _is_id(query: Any) -> bool:
    return (isinstance(query, int) and not isinstance(query, bool)) or (
        isinstance(query, str) and query.strip().isdigit()
    )


def resolve_color(colors: Mapping[int, str], query: Any) -> ColorChoice:
    """A palette id, a ``#RRGGBB``, or a colour word -> the nearest swatch the printer has."""
    palette = _palette(colors)
    if not palette:
        raise InvalidRequest("The printer's colour table is empty, so a colour cannot be chosen.")

    def nearest(rgb: tuple[int, int, int], how: str) -> ColorChoice:
        cid, hx, prgb = min(palette, key=lambda p: color_distance(rgb, p[2]))
        dist = color_distance(rgb, prgb)
        return ColorChoice(cid, hx, nearest_color_word(hx), how, dist == 0, round(dist, 1))

    if isinstance(query, bool) or query is None:
        raise InvalidRequest("color must be a colour word, #RRGGBB, or a palette id.")
    if _is_id(query):
        cid = int(query)
        if cid in colors and parse_hex(colors[cid]):
            hx = colors[cid]
            return ColorChoice(cid, hx, nearest_color_word(hx), "palette id", True, 0.0)
        raise InvalidRequest(
            f"There is no colour id {cid} in the printer's table.",
            hint=f"Valid ids: {', '.join(str(p[0]) for p in palette)}.",
        )

    text = str(query).strip()
    rgb = parse_hex(text)
    if rgb is not None:
        return nearest(rgb, "nearest swatch to that hex value")
    word = re.sub(r"[\s_\-]+", " ", text.lower())
    if word in COLOR_WORDS:
        return nearest(COLOR_WORDS[word], f"colour word {word!r}")
    raise InvalidRequest(
        f"I don't know the colour {text!r}.",
        hint=(
            "Use a common colour word, a #RRGGBB value, or one of the palette ids. "
            f"Words: {', '.join(sorted(set(COLOR_WORDS) - _ALIASES))}."
        ),
        palette=palette_listing(colors),
    )


# ---------------------------------------------------------------------------
# Filaments
# ---------------------------------------------------------------------------


def norm(value: Any) -> str:
    """Case, spacing and punctuation-insensitive form: "PLA-CF" == "pla cf" == "PLACF"."""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _candidate(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "type": entry.get("type"),
        "nozzle_temp_c": [entry.get("min_temp"), entry.get("max_temp")],
    }


@dataclass(frozen=True)
class FilamentChoice:
    id: int
    name: str
    type: str | None
    matched_by: str
    assumed: bool
    entry: Mapping[str, Any]


def resolve_filament(filaments: Mapping[int, Mapping[str, Any]], query: Any) -> FilamentChoice:
    """A filament id or a name -> exactly one profile from the printer's table.

    Order: an id; an exact name; a type with a single profile; a type with a generic
    "<type> Basic" profile; words that all appear in exactly one name. Anything left
    over is ambiguous or unknown, and the error lists the candidates -- picking one
    silently would put a wrong temperature range behind a spool.
    """
    entries = [e for e in filaments.values() if e.get("name")]
    if not entries:
        raise InvalidRequest(
            "The printer's filament table is empty, so a filament cannot be chosen."
        )
    if isinstance(query, bool) or query is None:
        raise InvalidRequest("filament must be a name (e.g. 'PETG Basic') or an id.")

    def choice(entry: Mapping[str, Any], how: str, assumed: bool = False) -> FilamentChoice:
        return FilamentChoice(
            int(entry["id"]), str(entry["name"]), entry.get("type"), how, assumed, dict(entry)
        )

    if _is_id(query):
        fid = int(query)
        if fid in filaments and filaments[fid].get("name"):
            return choice(filaments[fid], "filament id")
        raise InvalidRequest(
            f"There is no filament id {fid} in the printer's table.",
            hint="Use list_box_filament_options to search the table.",
        )

    q = norm(query)
    if not q:
        raise InvalidRequest("filament is empty.")

    exact = [e for e in entries if norm(e["name"]) == q]
    if len(exact) == 1:
        return choice(exact[0], "exact name")

    by_type = [e for e in entries if norm(e.get("type")) == q]
    if len(by_type) == 1:
        return choice(
            by_type[0],
            f"the only profile of type {by_type[0].get('type')!r}, which is what {query!r} names",
            assumed=True,
        )
    if len(by_type) > 1:
        basic = [e for e in by_type if norm(e["name"]) == q + "basic"]
        if len(basic) == 1:
            return choice(
                basic[0],
                f"generic: {query!r} is a material type, so its 'Basic' profile was used",
                assumed=True,
            )

    tokens = [t for t in re.split(r"[\s/]+", str(query).lower()) if norm(t)]
    token_hits = exact or [
        e for e in entries if all(norm(t) in norm(e["name"]) for t in tokens)
    ]
    if len(token_hits) == 1:
        return choice(token_hits[0], "name contains your words")

    pool = token_hits or by_type
    if pool:
        raise InvalidRequest(
            f"{query!r} matches {len(pool)} profiles; say which one.",
            hint="Repeat the call with the exact name of one of the candidates.",
            candidates=[_candidate(e) for e in pool][:12],
        )
    near = difflib.get_close_matches(q, [norm(e["name"]) for e in entries], n=5, cutoff=0.5)
    raise InvalidRequest(
        f"No filament called {query!r} in the printer's table.",
        hint="Use list_box_filament_options to search it.",
        candidates=[_candidate(e) for e in entries if norm(e["name"]) in near],
    )


def search_filaments(
    filaments: Mapping[int, Mapping[str, Any]], query: str | None
) -> list[dict[str, Any]]:
    """Profiles whose name or type contains every word of ``query`` (all of them if blank)."""
    tokens = [t for t in re.split(r"[\s/]+", (query or "").lower()) if norm(t)]
    out = []
    for _, e in sorted(filaments.items()):
        if not e.get("name"):
            continue
        hay = norm(f"{e['name']} {e.get('type') or ''}")
        if all(norm(t) in hay for t in tokens):
            out.append(_candidate(e))
    return out


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


def resolve_vendor(vendors: Mapping[int, str], query: Any) -> tuple[int, str]:
    if isinstance(query, bool) or query is None:
        raise InvalidRequest("vendor must be a name (e.g. 'QIDI', 'Generic') or an id.")
    listing = [{"id": k, "name": v} for k, v in sorted(vendors.items())]
    if _is_id(query):
        vid = int(query)
        if vid in vendors:
            return vid, vendors[vid]
        raise InvalidRequest(f"There is no vendor id {vid}.", vendors=listing)
    hits = [(k, v) for k, v in vendors.items() if norm(v) == norm(query)]
    if len(hits) == 1:
        return hits[0]
    raise InvalidRequest(
        f"Unknown vendor {query!r}.",
        hint="Use one of the vendors the printer lists.",
        vendors=listing,
    )
