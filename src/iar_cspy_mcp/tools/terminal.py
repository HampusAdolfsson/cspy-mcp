"""Target terminal I/O tools (libsupport)."""

from __future__ import annotations

from typing import Any

from .._app import get_client, mcp


@mcp.tool()
def libsupport_get_output(clear: bool = False, max_chars: int = 4000) -> dict[str, Any]:
    """Return captured target program output received via libsupport."""
    out = get_client().terminal.output(clear=bool(clear))
    return {
        "text": out.text[-max(1, int(max_chars)):],
        "text_len": len(out.text),
        "bytes_hex": out.data.hex(),
        "bytes_len": len(out.data),
        "exit_code": out.exit_code,
        "asserts": out.asserts,
    }


@mcp.tool()
def libsupport_clear_output() -> dict[str, Any]:
    """Clear captured libsupport output and assert history."""
    get_client().terminal.clear()
    return {"ok": True}


@mcp.tool()
def libsupport_push_input(text: str, append_newline: bool = False) -> dict[str, Any]:
    """Queue text for target stdin requests handled by libsupport."""
    added = len((text + ("\n" if append_newline else "")).encode("utf-8"))
    queued = get_client().terminal.send(text, newline=bool(append_newline))
    return {"ok": True, "queued_bytes": queued, "added_bytes": added}


@mcp.tool()
def libsupport_request_input_binary(length: int) -> dict[str, Any]:
    """Request pending input bytes from libsupport service."""
    requested = int(length)
    data = get_client().terminal.request_input_binary(requested)
    return {"requested": requested, "returned": len(data), "data_hex": data.hex()}


@mcp.tool()
def libsupport_request_input(length: int) -> str:
    """Request pending input text from libsupport service."""
    return get_client().terminal.request_input(int(length))
