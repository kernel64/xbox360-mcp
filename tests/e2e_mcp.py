"""Manual end-to-end check: starts the MCP server over stdio against the mock console.
   uv run python tests/e2e_mcp.py"""
import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(__file__))
import mock_xbdm
srv, port = mock_xbdm.serve(0)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    env = dict(os.environ, XBOX360_HOST="127.0.0.1", XBOX360_PORT=str(port), PYTHONPATH="src")
    params = StdioServerParameters(command=sys.executable, args=["-m", "xbox360_mcp"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(len(tools), [t.name for t in tools])
            for name, args in [("xbox_status", {}), ("xbox_list_dir", {"path": "Hdd:/"}),
                               ("xbox_read_memory", {"address": "82000100", "length": 32, "format": "u32"}),
                               ("xbox_disassemble", {"address": "0x82000100", "count": 5}),
                               ("xbox_launch", {"xex_path": "Hdd:\\Apps\\demo\\default.xex", "wait_for": "init ok", "timeout_seconds": 10}),
                               ("xbox_debug_log", {"types": ["debug"]}),
                               ("xbox_screenshot", {"max_width": 640}),
                               ("xbox_raw_command", {"command": "systime"})]:
                res = await s.call_tool(name, args)
                for c in res.content:
                    if c.type == "text": print(f"--- {name} (err={res.is_error}):", c.text[:500])
                    else: print(f"--- {name}: {c.type} {getattr(c,"mime_type","")} {len(getattr(c,'data',''))}")
asyncio.run(main())
