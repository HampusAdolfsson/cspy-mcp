#!/usr/bin/env bash
# Run the bridge over HTTP against a Thrift-enabled IarIde (the IAR IDE).
#
#   ./run_iaride.sh <iar-path>
#
# <iar-path> is an IAR installation or build stage, i.e. the directory with
# under it. iaride is taken from <iar-path>/common/bin.
#
# Env vars:
#   IAR_INSTALL_PATH  the installation, if not given as an argument
#   IARIDE_EXE  override the iaride path
#   MCP_PORT    HTTP port to listen on (default 8000)
#
# IarIde hosts the IDE platform services - ProjectManager and OptionsService -
# so the project_* and options_* tools work against it. If you do not have an
# IDE to hand, run_headless.sh gets the same services from IarServiceLauncher
# without a GUI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

WEB_PORT="${MCP_PORT:-8000}"
IAR_INSTALL_PATH="${1:-${IAR_INSTALL_PATH:-}}"
REGISTRY_FILE="$SCRIPT_DIR/CSpyServer2-ServiceRegistry.txt"

if [ -z "$IAR_INSTALL_PATH" ] && [ -z "${IARIDE_EXE:-}" ]; then
  echo "Path to an IAR installation is required." >&2
  echo "Pass it as the first argument or set IAR_INSTALL_PATH." >&2
  echo "It is the directory with common/bin under it." >&2
  exit 1
fi
IARIDE="${IARIDE_EXE:-$IAR_INSTALL_PATH/common/bin/iaride}"
if [ ! -x "$IARIDE" ]; then
  echo "iaride not found or not executable: $IARIDE" >&2
  exit 1
fi

# Unlike run_web.sh (which lets mcp_thrift_server spawn and manage CSpyServer2
# itself), IarIde is a long-lived GUI app, so we start it here and discover its
# service registry the same way any external tool would: IarIde writes its
# registry location to CSpyServer2-ServiceRegistry.txt in its current working
# directory, which is SCRIPT_DIR since we cd there above.
rm -f "$REGISTRY_FILE"
"$IARIDE" &
IARIDE_PID=$!
trap 'kill "$IARIDE_PID" 2>/dev/null || true' EXIT

echo "Starting IarIde (pid $IARIDE_PID), waiting for $REGISTRY_FILE ..." >&2
found=0
for _ in $(seq 1 600); do
  if [ -s "$REGISTRY_FILE" ]; then
    found=1
    break
  fi
  if ! kill -0 "$IARIDE_PID" 2>/dev/null; then
    echo "IarIde exited before starting its service registry" >&2
    exit 1
  fi
  sleep 0.1
done
if [ "$found" -ne 1 ]; then
  echo "Timed out waiting for $REGISTRY_FILE" >&2
  exit 1
fi

# The file holds a Thrift-JSON-serialized ServiceRegistry.thrift ServiceLocation
# struct, keyed by field id: {"1":{"str":"127.0.0.1"},"2":{"i32":43347},...}
# where 1=host, 2=port, 3=Protocol, 4=Transport.
read -r REGISTRY_HOST REGISTRY_PORT < <("$PYTHON" -c '
import json, sys
with open(sys.argv[1]) as f:
    location = json.load(f)
print(location["1"]["str"], location["2"]["i32"])
' "$REGISTRY_FILE")
echo "Found IarIde service registry at $REGISTRY_HOST:$REGISTRY_PORT" >&2
echo "MCP endpoint: http://127.0.0.1:$WEB_PORT/mcp" >&2

# Standalone mode: resolve services via the registry above instead of spawning
# and managing our own backend.
export THRIFT_CSPYSERVER_MODE=standalone
export THRIFT_REGISTRY_HOST="$REGISTRY_HOST"
export THRIFT_REGISTRY_PORT="$REGISTRY_PORT"

# Not exec'd (unlike run_web.sh): we need to stay alive after the server exits
# so the EXIT trap above can shut IarIde down too.
"$PYTHON" -m mcp_thrift_server \
  --web \
  --web-port "$WEB_PORT"
