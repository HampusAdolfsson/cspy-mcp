"""Talk to the MCP server the way an MCP host does: a subprocess over stdio.

    python examples/mcp/stdio_client.py --iar-path /opt/iar/ewarm
    python examples/mcp/stdio_client.py --registry-port 51926   # running backend

Arguments are passed to the server (``python -m iar_cspy_mcp``). Lists the
tools, then starts a session on the bundled firmware and sets a breakpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from iar_cspy import LaunchConfig

LAUNCH = Path(__file__).resolve().parents[1] / "firmware" / "launch.json"


async def main(server_args: list[str]) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "iar_cspy_mcp", *server_args])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = sorted(tool.name for tool in (await session.list_tools()).tools)
        print(f"{len(tools)} tools:", ", ".join(tools))

        async def call(name: str, **arguments: object) -> object:
            result = await session.call_tool(name, arguments)
            text = result.content[0].text if result.content else ""
            print(f"\n{name}({', '.join(f'{k}=...' for k in arguments)}) ->", "ERROR" if result.isError else "")
            print(text[:600])
            return result

        launch_json = json.dumps(LaunchConfig.from_file(LAUNCH))
        await call("debugger_configure_and_start_session", launch_json=launch_json)
        await call("breakpoints_set_on_ule", ule="commit_history", access_type=1)
        await call("debugger_go_and_wait_for_core_state", desired_state=0, timeout_ms=10000)
        await call("debugger_eval_expression", expression="g_phase")
        await call("debugger_stop_session")


if __name__ == "__main__":
    anyio.run(main, sys.argv[1:])
