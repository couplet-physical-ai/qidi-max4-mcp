"""Safety-gated MCP server for the QIDI Max 4 (Klipper/Moonraker)."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("qidi-mcp")
except PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
