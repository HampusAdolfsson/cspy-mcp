"""Deprecated name of the MCP server, kept so existing MCP client configs work.

``python -m mcp_thrift_server`` still starts the server, now ``iar_cspy_mcp``.
Point configs at ``python -m iar_cspy_mcp`` (or the ``iar-cspy-mcp`` script)
instead; this shim will be removed.
"""

import sys
from pathlib import Path

try:
    import iar_cspy_mcp  # noqa: F401
except ImportError:
    # Running from a checkout with PYTHONPATH=<repo>, as the old configs do:
    # find the server next to this shim. iar-cspy must be installed.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
