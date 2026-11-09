#!/usr/bin/env python
"""Print an MCP server registration block for *this* checkout.

`mcp-entry.json` is a template with placeholder paths, because a committed file
cannot know where you cloned the repo. Copying someone else's absolute paths is
the most common way a first run fails, and it fails confusingly: an MCP client
reports the server as unavailable with no hint that the interpreter path does
not exist.

So this resolves the real paths from where the script actually lives:

    .venv/bin/python scripts/mcp_entry.py                       # read-only (default)
    .venv/bin/python scripts/mcp_entry.py --printer 192.0.2.10
    .venv/bin/python scripts/mcp_entry.py --mock                # point at the local mock
    .venv/bin/python scripts/mcp_entry.py --writes              # drop QIDI_READ_ONLY

Paste the printed `"qidi": {...}` entry into your MCP client's server-list config
(e.g. the `mcpServers` object in Claude Desktop or Claude Code's config, or the
equivalent for whatever MCP client you use), then restart the client.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The mock Moonraker's default loopback port (see scripts/run_mock_printer.py).
MOCK_PORT = 7125


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--printer",
        default=None,
        help="printer host or URL (port 7125 is added if you omit it)",
    )
    ap.add_argument(
        "--mock",
        action="store_true",
        help=f"point at scripts/run_mock_printer.py on 127.0.0.1:{MOCK_PORT} instead of real hardware",
    )
    ap.add_argument(
        "--writes",
        action="store_true",
        help="omit QIDI_READ_ONLY=1. Read docs/guides/going-live.md first.",
    )
    ap.add_argument("--full", action="store_true", help="wrap it in an mcpServers object")
    args = ap.parse_args()

    if args.mock:
        url = f"http://127.0.0.1:{MOCK_PORT}"
    elif args.printer:
        url = args.printer if args.printer.startswith("http") else f"http://{args.printer}"
        if ":" not in url.split("//", 1)[1]:
            url += ":7125"
    else:
        url = "http://192.0.2.10:7125"  # placeholder; replace with your printer's address

    interpreter = ROOT / ".venv" / "bin" / "python"
    if not interpreter.exists():
        print(
            f"warning: {interpreter} does not exist yet — create the venv first:\n"
            f"  uv sync --extra dev",
            file=sys.stderr,
        )

    env = {"QIDI_PRINTER_URL": url}
    if not args.writes:
        # Read-only first is the documented path, so it is the default here too.
        env["QIDI_READ_ONLY"] = "1"
    env["QIDI_AUDIT_LOG"] = str(ROOT / "audit.jsonl")

    entry = {
        "qidi": {
            "type": "stdio",
            "command": str(interpreter),
            "args": ["-m", "qidi_mcp.server"],
            "cwd": str(ROOT),
            "env": env,
        }
    }
    payload = {"mcpServers": entry} if args.full else entry
    print(json.dumps(payload, indent=2))
    if not args.writes:
        print(
            "\n# QIDI_READ_ONLY=1: every mutating tool is refused before any network\n"
            "# call. Confirm get_capabilities and get_printer_status look right, then\n"
            "# see docs/guides/going-live.md before dropping it.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
