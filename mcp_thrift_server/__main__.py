import sys

from . import __doc__ as _notice  # noqa: F401  (sets up sys.path when needed)

print(
    "mcp_thrift_server is deprecated: run `python -m iar_cspy_mcp` (or `iar-cspy-mcp`) instead.",
    file=sys.stderr,
    flush=True,
)

from iar_cspy_mcp.cli import main  # noqa: E402

main()
