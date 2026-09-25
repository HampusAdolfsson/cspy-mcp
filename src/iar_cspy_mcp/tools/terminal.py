"""Target terminal I/O tools (libsupport)."""

from __future__ import annotations

from typing import Any

from .._app import get_client, mcp


@mcp.tool()
def libsupport_get_output(clear: bool = False, max_chars: int = 4000) -> dict[str, Any]:
    """Return captured target program output received via libsupport.

    waiting_for_input is true while the target is blocked reading stdin with no
    input queued (see libsupport_push_input).
    """
    terminal = get_client().terminal
    out = terminal.output(clear=bool(clear))
    return {
        "text": out.text[-max(1, int(max_chars)):],
        "text_len": len(out.text),
        "bytes_hex": out.data.hex(),
        "bytes_len": len(out.data),
        "exit_code": out.exit_code,
        "asserts": out.asserts,
        "waiting_for_input": terminal.waiting_for_input,
    }


@mcp.tool()
def libsupport_clear_output() -> dict[str, Any]:
    """Clear captured libsupport output and assert history."""
    get_client().terminal.clear()
    return {"ok": True}


@mcp.tool()
def libsupport_push_input(text: str, append_newline: bool = False) -> dict[str, Any]:
    """Queue text for the target's stdin (libsupport).

    A target stdin read waits for input, as at a console, so input can be
    pushed before the read or once the target asks for it (see
    libsupport_wait_for_input_request). Ending the session answers a waiting
    read with end-of-file.
    """
    added = len((text + ("\n" if append_newline else "")).encode("utf-8"))
    queued = get_client().terminal.send(text, newline=bool(append_newline))
    return {"ok": True, "queued_bytes": queued, "added_bytes": added}


@mcp.tool()
def libsupport_wait_for_input_request(timeout_ms: int = 5000) -> dict[str, Any]:
    """Wait until the target blocks reading stdin with no input queued.

    Returns {"waiting_for_input": bool}; false means the timeout passed first.
    Push the input with libsupport_push_input.
    """
    waiting = get_client().terminal.wait_for_input_request(timeout=max(0.0, int(timeout_ms) / 1000.0))
    return {"waiting_for_input": bool(waiting)}


@mcp.tool()
def libsupport_request_input_binary(length: int) -> dict[str, Any]:
    """Take up to length queued stdin bytes, as a target read would, without waiting."""
    requested = int(length)
    data = get_client().terminal.request_input_binary(requested)
    return {"requested": requested, "returned": len(data), "data_hex": data.hex()}


@mcp.tool()
def libsupport_request_input(length: int) -> str:
    """Take up to length queued stdin bytes as text, as a target read would, without waiting."""
    return get_client().terminal.request_input(int(length))
