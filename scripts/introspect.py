"""Extract the tool inventory from a REAL stdio session, not from source."""
import asyncio, json, os, sys
sys.path.insert(0, '.')
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = {**os.environ, "QIDI_PRINTER_URL": "http://127.0.0.1:7125",
           "QIDI_READ_ONLY": "1", "QIDI_TEST_MODE": "1"}
    p = StdioServerParameters(command=sys.executable, args=["-m","qidi_mcp.server"], env=env, cwd=os.getcwd())
    async with stdio_client(p) as (r,w):
        async with ClientSession(r,w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            out = {"server": {"name": init.server_info.name, "version": init.server_info.version},
                   "instructions": init.instructions or "",
                   "tools": []}
            for t in tools:
                a = t.annotations
                out["tools"].append({
                    "name": t.name,
                    "description": t.description or "",
                    "read_only": bool(getattr(a,"read_only_hint",False)) if a else False,
                    "destructive": bool(getattr(a,"destructive_hint",False)) if a else False,
                    "idempotent": bool(getattr(a,"idempotent_hint",False)) if a else False,
                    "schema": t.inputSchema if hasattr(t,"inputSchema") else t.input_schema,
                })
            print(json.dumps(out, indent=1))
asyncio.run(main())
