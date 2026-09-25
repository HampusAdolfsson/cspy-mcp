"""Inspection tools: stack and symbols, memory, disassembly, source lookup."""

from __future__ import annotations

from typing import Any

from iar_cspy import to_plain

from .._app import get_client, mcp, require_session
from ._args import context_from_json


@mcp.tool()
def contextmanager_get_stack(context_json: str = "", low: int = 0, high: int = 20) -> Any:
    """Get call stack frames for a context.

    context_json example: {"type":"CurrentBase"}

    Headless backends (no IDE) fail this with "No such service,
    serviceName=frontend": the backend calls back into the IDE's frontend
    service. Registers, eval and memory reads work regardless.
    """
    require_session("contextmanager_get_stack")
    return to_plain(get_client().context.stack(int(low), int(high), context_from_json(context_json)))


@mcp.tool()
def contextmanager_get_stack_depth(context_json: str = "", max_depth: int = 256) -> int:
    """Get stack depth for a context."""
    require_session("contextmanager_get_stack_depth")
    return get_client().context.depth(int(max_depth), context_from_json(context_json))


@mcp.tool()
def contextmanager_get_context_info(context_json: str = "") -> Any:
    """Get context info (function, location, source) for a context reference.

    Headless backends (no IDE) fail this with "No such service,
    serviceName=frontend": the backend calls back into the IDE's frontend
    service. Registers, eval and memory reads work regardless.
    """
    require_session("contextmanager_get_context_info")
    return to_plain(get_client().context.info(context_from_json(context_json)))


@mcp.tool()
def contextmanager_get_locals(context_json: str = "") -> Any:
    """Get local symbols for a context reference."""
    require_session("contextmanager_get_locals")
    return to_plain(get_client().context.locals(context_from_json(context_json)))


@mcp.tool()
def contextmanager_get_parameters(context_json: str = "") -> Any:
    """Get function parameters for a context reference."""
    require_session("contextmanager_get_parameters")
    return to_plain(get_client().context.parameters(context_from_json(context_json)))


@mcp.tool()
def symbols_list_visible(context_json: str = "") -> dict[str, Any]:
    """List visible local and parameter symbols in the selected context."""
    require_session("symbols_list_visible")
    visible = get_client().context.visible_symbols(context_from_json(context_json))
    return {**visible, "count": len(visible["all"])}


@mcp.tool()
def symbols_lookup(name: str, context_json: str = "", prefix: bool = False) -> dict[str, Any]:
    """Lookup symbol names and evaluate value for exact matches.

    - Uses ContextManager locals/parameters for discovery.
    - Uses Debugger.evalExpression for exact value evaluation.
    """
    require_session("symbols_lookup")
    return get_client().context.lookup(name, context_from_json(context_json), prefix=bool(prefix))


@mcp.tool()
def memory_read(zone_id: int, address: int, wordsize: int = 1, bitsize: int = 8, count: int = 16) -> dict[str, Any]:
    """Read memory and return bytes as hex.

    address is in units for the selected zone. Zone 0 is normally the main
    memory zone; debugger_call("getAllZones") lists the zones of the target.
    """
    require_session("memory_read")
    data = get_client().memory.read(
        int(address), int(count), zone=int(zone_id), wordsize=int(wordsize), bitsize=int(bitsize)
    )
    return {
        "zone_id": int(zone_id),
        "address": int(address),
        "wordsize": int(wordsize),
        "bitsize": int(bitsize),
        "count": int(count),
        "hex": data.hex(),
        "byte_len": len(data),
    }


@mcp.tool()
def memory_write_hex(
    zone_id: int,
    address: int,
    data_hex: str,
    wordsize: int = 1,
    bitsize: int = 8,
    count: int | None = None,
) -> dict[str, Any]:
    """Write memory from hex string payload."""
    require_session("memory_write_hex")
    payload = bytes.fromhex(data_hex)
    written = get_client().memory.write(
        int(address), payload, zone=int(zone_id), wordsize=int(wordsize), bitsize=int(bitsize), count=count
    )
    return {
        "ok": True,
        "zone_id": int(zone_id),
        "address": int(address),
        "count": written,
        "written_bytes": len(payload),
    }


@mcp.tool()
def disassembly_disassemble_range(
    from_zone_id: int,
    from_address: int,
    to_zone_id: int,
    to_address: int,
    context_json: str = "",
) -> Any:
    """Disassemble instruction range between two locations."""
    require_session("disassembly_disassemble_range")
    result = get_client().disassembly.range(
        int(from_address),
        int(to_address),
        zone=int(from_zone_id),
        end_zone=int(to_zone_id),
        context=context_from_json(context_json),
    )
    return to_plain(result)


@mcp.tool()
def sourcelookup_get_source_ranges(zone_id: int, address: int) -> Any:
    """Map an execution location to source ranges."""
    require_session("sourcelookup_get_source_ranges")
    return to_plain(get_client().source.ranges(int(address), zone=int(zone_id)))
