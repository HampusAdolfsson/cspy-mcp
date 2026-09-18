#!/usr/bin/env bash
# MCP server over HTTP, for a client that connects to a URL rather than
# launching the server itself. Same backend as run_headless.sh - an
# IarServiceLauncher hosting the IDE services plus a CSpyServer2 joining its
# registry - so every tool is available; only the transport differs.
#
#   ./run_web.sh <iar-stage>
#
# <iar-stage> is a stage or installation directory, i.e. the one with common/bin
# under it. Every program used here is taken from <iar-stage>/common/bin.
#
# Env vars:
#   IAR_STAGE            the stage, if not given as an argument
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
STAGE="${1:-${IAR_STAGE:-}}"
[ "$#" -gt 0 ] && shift

if [ -z "$STAGE" ]; then
  echo "Path to an IAR stage is required." >&2
  echo "Pass it as the first argument or set IAR_STAGE." >&2
  echo "It is the directory with common/bin under it." >&2
  exit 1
fi
if [ ! -d "$STAGE/common/bin" ]; then
  echo "Not an IAR stage - no common/bin under it: $STAGE" >&2
  exit 1
fi

ARGS=(--web --web-port "$WEB_PORT" --iar-stage "$STAGE")

if [ -n "${NO_IDE_SERVICES:-}" ]; then
  echo "Hosting no IDE services: debugger tools only." >&2
  ARGS+=(--no-ide-services)
fi
if [ -n "${THRIFT_IDE_SERVICES:-}" ]; then
  ARGS+=(--ide-services "$THRIFT_IDE_SERVICES")
fi

echo "IAR stage:    $STAGE" >&2
echo "MCP endpoint: http://127.0.0.1:$WEB_PORT/mcp" >&2

exec "$PYTHON" -m mcp_thrift_server "${ARGS[@]}" "$@"
