"""Tool modules, each registering its tools onto the MCPServer.

Every module exposes ``register(server, rt)``. ``server.py`` calls them in
order; the split is by printer subsystem so a reader can find "everything about
temperature" in one file.
"""

from __future__ import annotations

from . import (
    calibration,
    camera,
    devices,
    files,
    jobs,
    motion,
    qidi,
    sharing,
    status,
    system,
    thermal,
)

MODULES = (
    status,
    thermal,
    motion,
    jobs,
    files,
    devices,
    calibration,
    camera,
    system,
    qidi,
    sharing,
)

__all__ = ["MODULES"]
