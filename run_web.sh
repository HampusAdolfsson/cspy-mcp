#!/usr/bin/env bash
# MCP server over HTTP, for a client that connects to a URL rather than
# launching the server itself. Same backend as run_headless.sh - an
# IarServiceLauncher hosting the IDE services plus a CSpyServer2 joining its
# registry - so every tool is available; only the transport differs.
#
#   ./run_web.sh <iar-path>
#
# <iar-path> is an IAR installation or build stage, i.e. the directory with
# under it. Every program used here is taken from <iar-path>/common/bin.
#
# Env vars:
#   IAR_PATH             the IAR path, if not given as an argument
#   NO_IDE_SERVICES=1    debugger only, no IarServiceLauncher
#   THRIFT_IDE_SERVICES  restrict which IDE services to host
#   MCP_PORT             HTTP port to listen on (default 8000)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

WEB_PORT="${MCP_PORT:-8000}"
IAR_PATH="${1:-${IAR_PATH:-}}"
[ "$#" -gt 0 ] && shift

if [ -z "$IAR_PATH" ]; then
  echo "Path to an IAR installation is required." >&2
  echo "Pass it as the first argument or set IAR_PATH." >&2
  echo "It is the directory with common/bin under it." >&2
  exit 1
fi
if [ ! -d "$IAR_PATH/common/bin" ]; then
  echo "No common/bin under it, so not an IAR installation: $IAR_PATH" >&2
  exit 1
fi

ARGS=(--web --web-port "$WEB_PORT" --iar-path "$IAR_PATH")

if [ -n "${NO_IDE_SERVICES:-}" ]; then
  echo "Hosting no IDE services: debugger tools only." >&2
  ARGS+=(--no-ide-services)
fi
if [ -n "${THRIFT_IDE_SERVICES:-}" ]; then
  ARGS+=(--ide-services "$THRIFT_IDE_SERVICES")
fi

echo "IAR path:     $IAR_PATH" >&2
echo "MCP endpoint: http://127.0.0.1:$WEB_PORT/mcp" >&2

exec "$PYTHON" -m mcp_thrift_server "${ARGS[@]}" "$@"
