"""The FastMCP application and the one iar_cspy Client every tool uses."""

from __future__ import annotations

import os
import threading
from typing import Any

from iar_cspy import Client, Config, SessionError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "thrift-debugger-server",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8000")),
)

_lock = threading.Lock()
_client: Client | None = None


def get_client() -> Client:
    """The server's client, created from the environment on first use."""
    global _client
    with _lock:
        if _client is None:
            _client = Client(Config.from_env(), owns_backend=True)
        return _client


def set_client(client: Client | None) -> None:
    """Install the client the tools use (the CLI and tests do this)."""
    global _client
    with _lock:
        _client = client


def require_session(tool_name: str) -> None:
    """The lifecycle guard for tools that need a started debug session."""
    if not get_client().debugger.started:
        raise SessionError(
            f"{tool_name} requires an active debug session. Required order: "
            "debugger_configure_session(launch_json) -> debugger_start_smp_session()."
        )


def envelope(*, ok: bool, data: Any, tool: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """The stable response shape of the AI-first tools."""
    return {"ok": bool(ok), "tool": tool, "data": data, "error": error}
