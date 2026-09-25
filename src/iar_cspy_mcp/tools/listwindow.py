"""List window tools (trace and other tabular debugger views)."""

from __future__ import annotations

from typing import Any

from .._app import get_client, mcp, require_session


@mcp.tool()
def listwindow_list_services(name_filter: str = "listwindow") -> list[dict[str, Any]]:
    """List services registered in ServiceRegistry, optionally filtered by name.

    Useful to discover listwindow-like services (for example trace/list windows)
    before querying rows. In standalone/headless sessions, instruction trace
    services may not be published: pass "" to see everything registered.
    """
    require_session("listwindow_list_services")
    return get_client().listwindows.services(name_filter)


@mcp.tool()
def listwindow_get_overview(service_name: str) -> dict[str, Any]:
    """Fetch list window metadata such as display name, columns, and row count."""
    require_session("listwindow_get_overview")
    overview = get_client().listwindows.window(service_name).overview()
    return {"service_name": service_name, **overview.to_dict()}


@mcp.tool()
def listwindow_get_rows(service_name: str, first_row: int = 0, max_rows: int = 50) -> dict[str, Any]:
    """Read rows from a ListWindowBackend service.

    Args:
        service_name: Exact ServiceRegistry name for the listwindow backend.
        first_row: Start row index.
        max_rows: Maximum rows to fetch.
    """
    require_session("listwindow_get_rows")
    result = get_client().listwindows.window(service_name).rows(int(first_row), max(1, int(max_rows)))
    return {
        "service_name": service_name,
        "is_sliding": result.is_sliding,
        "chunk_info": result.chunk_info,
        "row_count": result.row_count,
        "first_row": result.first_row,
        "returned": len(result.rows),
        "rows": result.rows,
    }


@mcp.tool()
def listwindow_sliding_navigate(
    service_name: str,
    fraction: float = 0.5,
    chunk_pos: int = 0,
    min_lines: int = 100,
) -> dict[str, Any]:
    """Navigate a sliding listwindow to materialize chunk rows.

    Useful for trace/list windows that report zero rows until a chunk has been requested.
    """
    require_session("listwindow_sliding_navigate")
    chunk = get_client().listwindows.window(service_name).navigate(float(fraction), int(chunk_pos), int(min_lines))
    return {"service_name": service_name, **chunk.to_dict()}


@mcp.tool()
def listwindow_get_notifications(clear: bool = False) -> dict[str, Any]:
    """Return captured ListWindowFrontend notifications from backend callbacks."""
    require_session("listwindow_get_notifications")
    notes, toolbar_notes = get_client().listwindows.notifications(clear=bool(clear))
    return {
        "notes": notes,
        "toolbar_notes": toolbar_notes,
        "note_count": len(notes),
        "toolbar_note_count": len(toolbar_notes),
    }


@mcp.tool()
def listwindow_trace_status(service_name: str) -> dict[str, Any]:
    """Get trace-window capabilities and enablement state for a trace listwindow service."""
    require_session("listwindow_trace_status")
    status = get_client().listwindows.trace(service_name).status()
    return {"service_name": service_name, **status.to_dict()}


@mcp.tool()
def listwindow_trace_set_enabled(service_name: str, enabled: bool = True) -> dict[str, Any]:
    """Enable or disable a TraceListWindowBackend service."""
    require_session("listwindow_trace_set_enabled")
    state = get_client().listwindows.trace(service_name).set_enabled(bool(enabled))
    return {"ok": True, "service_name": service_name, "enabled": state}


@mcp.tool()
def listwindow_trace_clear(service_name: str) -> dict[str, Any]:
    """Clear trace data in a TraceListWindowBackend service when supported."""
    require_session("listwindow_trace_clear")
    get_client().listwindows.trace(service_name).clear()
    return {"ok": True, "service_name": service_name}
