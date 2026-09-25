"""The MCP server against a real backend (skipped without one; see the --cspy-* options)."""

from __future__ import annotations

import json
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from iar_cspy import Client

pytestmark = pytest.mark.live


@pytest.fixture()
def server(cspy_config):
    from iar_cspy_mcp import server

    client = Client(cspy_config, owns_backend=cspy_config.mode == "managed")
    server.set_client(client)
    yield server
    try:
        server.debugger_stop_session()
    finally:
        server.set_client(None)
        client.close()


def test_configure_start_and_breakpoint_roundtrip(server, cspy_launch):
    # Explicit lifecycle: resolve -> configure -> start.
    assert server.debugger_configure_session(json.dumps(cspy_launch))["ok"]
    assert server.debugger_start_smp_session()["ok"]

    bp = server.breakpoints_set_on_ule("main", 1)
    assert bp["valid"] is True and bp["id"] > 0
    assert any(entry["id"] == bp["id"] for entry in server.breakpoints_get_all())


def test_discovery_and_read_only_calls(server, cspy_launch):
    out = server.debugger_configure_and_start_session(json.dumps(cspy_launch))
    assert out["ok"] and out["data"]["ranToSymbol"] is True

    assert server.debugger_get_version()
    assert server.debugger_is_online() is True
    assert isinstance(server.listwindow_list_services("trace"), list)
    assert server.debugger_register_snapshot(limit=4)["returned"] == 4
    assert server.debugger_session_status()["data"]["core_states"] == [0]
    assert server.memory_read(0, 0, count=4)["byte_len"] == 4


def cli_args(config) -> list[str]:
    if config.mode == "standalone":
        return ["--registry-host", config.registry_host or "127.0.0.1", "--registry-port", str(config.registry_port)]
    if config.iar_path is not None:
        return ["--iar-path", str(config.iar_path)]
    return ["--cspyserver2", str(config.cspy_executable)]


def test_mcp_protocol_over_stdio(cspy_config, cspy_launch):
    """The server as an MCP host runs it: a subprocess speaking MCP on stdio."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "iar_cspy_mcp", *cli_args(cspy_config)], env=None
    )

    async def run() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"debugger_configure_and_start_session", "breakpoints_set_on_ule"} <= tools

            async def call(name: str, **arguments: object) -> object:
                result = await session.call_tool(name, arguments)
                assert not result.isError, result.content
                return json.loads(result.content[0].text)

            started = await call("debugger_configure_and_start_session", launch_json=json.dumps(cspy_launch))
            assert started["ok"] is True
            bp = await call("breakpoints_set_on_ule", ule="main", access_type=1)
            assert bp["valid"] is True
            stopped = await call("debugger_stop_session")
            assert stopped["ok"] is True

    anyio.run(run)
