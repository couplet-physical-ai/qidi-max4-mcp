"""Match what a person typed ("benchy", "bracket abs") to a file that is on the printer.

Moonraker's queue and print endpoints want the exact path relative to the ``gcodes``
root, and refuse anything else. Typing ``projects/bracket_abs.gcode`` from memory is
the friction this removes -- but only by *resolving* names, never by guessing: a name
that matches nothing, or matches several files, is an error that lists the candidates,
because queueing the wrong file commits hours of machine time.

Pure functions, no I/O.
"""

from __future__ import annotations

import difflib
import posixpath
from collections.abc import Sequence
from typing import Any

from .errors import InvalidRequest
from .filament_names import norm

#: Most names one call will resolve: each costs a metadata read afterwards.
MAX_NAMES = 50


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def resolve_gcode_name(requested: str, available: Sequence[str]) -> str:
    """One requested name -> exactly one path from ``available``, else an explanatory error.

    Tiers, first non-empty wins: the exact path; the same ignoring case and a leading
    slash; the file's name or name-without-extension; the requested words contained in
    the path. A tier that yields several files is ambiguous, not "pick the first".
    """
    q = (requested or "").strip().lstrip("/")
    if not q:
        raise InvalidRequest("A filename is empty.")
    if q in available:
        return q
    low = q.lower()
    tiers = (
        [p for p in available if p.lower() == low],
        [p for p in available if posixpath.basename(p).lower() == low or _stem(p).lower() == low],
        [p for p in available if norm(q) and norm(q) in norm(p)],
    )
    for hits in tiers:
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise InvalidRequest(
                f"{requested!r} matches {len(hits)} files; say which one.",
                hint="Repeat the call with the full path of one of the candidates.",
                candidates=sorted(hits)[:12],
            )
    near = difflib.get_close_matches(q, list(available), n=5, cutoff=0.5)
    raise InvalidRequest(
        f"No file called {requested!r} on the printer.",
        hint="Use list_gcode_files to see what is there (paths are relative to gcodes).",
        candidates=near,
    )


def resolve_gcode_names(requested: Sequence[Any], available: Sequence[str]) -> list[dict[str, str]]:
    """Resolve every name before anything is queued; one bad name fails the whole call."""
    if not requested:
        raise InvalidRequest("Give at least one filename.")
    if len(requested) > MAX_NAMES:
        raise InvalidRequest(f"At most {MAX_NAMES} files per call.")
    out = []
    for raw in requested:
        if not isinstance(raw, str):
            raise InvalidRequest(f"Filenames must be strings, got {raw!r}.")
        out.append({"requested": raw, "file": resolve_gcode_name(raw, available)})
    return out
