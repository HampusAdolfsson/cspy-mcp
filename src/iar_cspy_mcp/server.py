"""The MCP server: every tool registered on one FastMCP app.

The tools are thin adapters over the iar_cspy API: they parse the JSON-string
arguments MCP clients send, call the API on the server's one
:class:`iar_cspy.Client`, and shape the result into plain JSON (with the
``{"ok", "tool", "data", "error"}`` envelope for the AI-first tools).
"""

from __future__ import annotations

from ._app import envelope, get_client, mcp, require_session, set_client
from .tools.breakpoints import *  # noqa: F401,F403
from .tools.debugger import *  # noqa: F401,F403
from .tools.inspection import *  # noqa: F401,F403
from .tools.listwindow import *  # noqa: F401,F403
from .tools.options import *  # noqa: F401,F403
from .tools.project import *  # noqa: F401,F403
from .tools.terminal import *  # noqa: F401,F403

__all__ = ["envelope", "get_client", "mcp", "require_session", "set_client"]
