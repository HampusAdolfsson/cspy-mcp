"""Breakpoint and watchpoint tools."""

from __future__ import annotations

from typing import Any

from iar_cspy import CSpyError, to_plain
from iar_cspy.errors import with_context

from .._app import get_client, mcp, require_session


@mcp.tool()
def breakpoints_get_all() -> Any:
    """List all breakpoints from the Breakpoints service."""
    require_session("breakpoints_get_all")
    return to_plain(get_client().breakpoints.all())


@mcp.tool()
def breakpoints_get(id: int) -> Any:
    """Get a single breakpoint by id."""
    require_session("breakpoints_get")
    return to_plain(get_client().breakpoints.get(int(id)))


@mcp.tool()
def breakpoints_set_from_descriptor(descriptor: str) -> Any:
    """Create/update a breakpoint from an existing descriptor string.

    Important:
        Descriptor is backend-specific opaque data. The reliable source is
        breakpoints_get_all() -> descriptor, then pass that descriptor back here.
        Do not assume this accepts free-form strings like ULE or JSON.
    """
    require_session("breakpoints_set_from_descriptor")
    try:
        return to_plain(get_client().breakpoints.restore(descriptor))
    except CSpyError as exc:
        raise with_context(exc, f"{exc} Take descriptors from breakpoints_get_all().") from exc


@mcp.tool()
def breakpoints_set_on_ule(ule: str, access_type: int = 1) -> Any:
    """Set a breakpoint/watchpoint on a backend ULE expression.

    Preferred for normal breakpoint creation. For code breakpoints, pass
    access_type=1.

    access_type follows shared.AccessType enum values:
        1 = execute/fetch breakpoint (code breakpoint)
        2 = read watchpoint
        3 = write watchpoint
        4 = read/write watchpoint
    The created breakpoint may report accessType 0; do not rely on that field.

    ULE parsing uses the debugger Universal Location Expression parser
    (DkUle::ParseUleString with code-context parsing). In practice this accepts:
        - expression ULEs: main, func+4, *ptr
        - absolute ULEs: 0x100, Memory:0x42
        - source ULEs (full form): {E:/path/file.c}.123.1
        - optional range suffix: <ule>@<size>

    Notes:
        - full source ULE form is the reliable backend format; shorthand file:line
          may work in some flows but is backend-dependent.
        - access_type controls breakpoint/watchpoint category selection
          (fetch/read/write/read-write).
        - "main()" may fail on some backends even when "main" works.
        - this call expects an active configured+started debug session
          (resolve -> configure -> start).
        - descriptor strings are a different API: use breakpoints_set_from_descriptor
          only with descriptors returned by breakpoints_get_all().
    """
    require_session("breakpoints_set_on_ule")
    return to_plain(get_client().breakpoints.add(ule, int(access_type)))


@mcp.tool()
def breakpoints_set_on_ule_with_category(ule: str, access_type: int, category_id: str) -> Any:
    """Set a breakpoint on ULE with category id."""
    require_session("breakpoints_set_on_ule_with_category")
    return to_plain(get_client().breakpoints.add(ule, int(access_type), category=category_id))


@mcp.tool()
def breakpoints_enable(id: int, enable: bool = True) -> bool:
    """Enable or disable a breakpoint by id."""
    require_session("breakpoints_enable")
    return get_client().breakpoints.enable(int(id), bool(enable))


@mcp.tool()
def breakpoints_remove(id: int) -> bool:
    """Remove a breakpoint by id."""
    require_session("breakpoints_remove")
    return get_client().breakpoints.remove(int(id))


@mcp.tool()
def breakpoints_recently_hit() -> Any:
    """Return recently hit breakpoints."""
    require_session("breakpoints_recently_hit")
    return to_plain(get_client().breakpoints.recently_hit())
