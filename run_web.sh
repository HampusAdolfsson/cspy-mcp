#!/usr/bin/env bash
# Run the bridge in managed mode over HTTP: it spawns and supervises
# CSpyServer2 itself and auto-detects the registry port that process prints.
#
#   ./run_web.sh [/path/to/CSpyServer2]
#
# Env vars:
#   THRIFT_CSPYSERVER_EXE  path to CSpyServer2 (if not given as an argument)
#   MCP_PORT               HTTP port to listen on (default 8000)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

WEB_PORT="${MCP_PORT:-8000}"
CSPYSERVER2="${1:-${THRIFT_CSPYSERVER_EXE:-}}"

if [ -z "$CSPYSERVER2" ]; then
  echo "Path to CSpyServer2 is required." >&2
  echo "Pass it as the first argument or set THRIFT_CSPYSERVER_EXE." >&2
  echo "It lives in <install>/common/bin of an IAR toolchain installation." >&2
  exit 1
fi
if [ ! -x "$CSPYSERVER2" ]; then
  echo "CSpyServer2 not found or not executable: $CSPYSERVER2" >&2
  exit 1
fi

echo "CSpyServer2:  $CSPYSERVER2" >&2
echo "MCP endpoint: http://127.0.0.1:$WEB_PORT/mcp" >&2

exec "$PYTHON" -m mcp_thrift_server \
  --web \
  --web-port "$WEB_PORT" \
  --cspyserver2 "$CSPYSERVER2"
