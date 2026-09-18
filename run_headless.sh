#!/usr/bin/env bash
# Run the bridge over HTTP in launcher mode: it spawns IarServiceLauncher,
# which owns the service registry and hosts the IDE platform services
# (ProjectManager, OptionsService), plus a CSpyServer2 that joins that same
# registry. This is the headless equivalent of run_iaride.sh - same services,
# no GUI.
#
#   ./run_headless.sh [/path/to/common/bin]
#
# Env vars:
#   IAR_STAGE_BIN               <install>/common/bin holding IarServiceLauncher
#                               and CSpyServer2 (if not given as an argument)
#   THRIFT_SERVICE_LAUNCHER_EXE override the IarServiceLauncher path
#   THRIFT_CSPYSERVER_EXE       override the CSpyServer2 path; "none" to run
#                               without a debugger (project/options only)
#   THRIFT_IDE_SERVICES         restrict which IDE services to host,
#                               e.g. "projectmanager"
#   MCP_PORT                    HTTP port to listen on (default 8000)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

WEB_PORT="${MCP_PORT:-8000}"

# Everything lives in one <install>/common/bin: IarServiceLauncher, CSpyServer2,
# the service libraries and projectmanager.json.
STAGE_BIN="${1:-${IAR_STAGE_BIN:-}}"
if [ -z "$STAGE_BIN" ] && [ -z "${THRIFT_SERVICE_LAUNCHER_EXE:-}" ]; then
  echo "Path to an <install>/common/bin is required." >&2
  echo "Pass it as the first argument or set IAR_STAGE_BIN." >&2
  echo "It is the directory holding IarServiceLauncher and CSpyServer2." >&2
  exit 1
fi
[ "$#" -gt 0 ] && shift

SERVICE_LAUNCHER="${THRIFT_SERVICE_LAUNCHER_EXE:-$STAGE_BIN/IarServiceLauncher}"
CSPYSERVER2="${THRIFT_CSPYSERVER_EXE:-$STAGE_BIN/CSpyServer2}"

# Unlike run_iaride.sh, this script does not babysit any backend process.
# mcp_thrift_server's "launcher" mode owns both of them: it starts
# IarServiceLauncher (which hosts the service registry plus the ProjectManager
# and OptionsService libraries) and then starts CSpyServer2 with
# "-registry <port>" so it joins that same registry instead of creating a
# second one. So there is no CSpyServer2-ServiceRegistry.txt to poll for and
# no shutdown trap to install - both children are supervised and torn down by
# the server itself (CSpyServer2 first, then the launcher).
#
# The IDE services are loaded lazily: the first project_* tool call starts the
# ProjectManager, the first options_* call starts the OptionsService. Call the
# ide_services_ensure tool to warm them up front, or ide_services_status to see
# what the registry currently holds.

if [ ! -x "$SERVICE_LAUNCHER" ]; then
  echo "IarServiceLauncher not found or not executable: $SERVICE_LAUNCHER" >&2
  echo "Set IAR_STAGE_BIN to an <install>/common/bin, or THRIFT_SERVICE_LAUNCHER_EXE." >&2
  echo "On Windows the executable is IarServiceLauncher.exe." >&2
  exit 1
fi

ARGS=(--web --web-port "$WEB_PORT" --service-launcher "$SERVICE_LAUNCHER")

# CSpyServer2 is optional here. Without it the project_* and options_* tools
# still work and only the debugger_* tools fail to resolve, which is a useful
# mode when all you want is project/option access. Set
# THRIFT_CSPYSERVER_EXE=none for it.
if [ "${CSPYSERVER2}" = "none" ]; then
  echo "Starting without CSpyServer2: project/options tools only." >&2
  # The sentinel must not reach the server, which would take it for a path.
  unset THRIFT_CSPYSERVER_EXE
elif [ -x "$CSPYSERVER2" ]; then
  ARGS+=(--cspyserver2 "$CSPYSERVER2")
else
  echo "CSpyServer2 not found or not executable: $CSPYSERVER2" >&2
  echo "Set THRIFT_CSPYSERVER_EXE, or CSPYSERVER2=none to run without a debugger." >&2
  exit 1
fi

# Restrict which IDE services get hosted, e.g. THRIFT_IDE_SERVICES=projectmanager.
if [ -n "${THRIFT_IDE_SERVICES:-}" ]; then
  ARGS+=(--ide-services "$THRIFT_IDE_SERVICES")
fi

echo "Stage:            $STAGE_BIN" >&2
echo "Service launcher: $SERVICE_LAUNCHER" >&2
echo "MCP endpoint:     http://127.0.0.1:$WEB_PORT/mcp" >&2

# Any remaining arguments are forwarded to the module, so this also works for
# other transports and flags:
#   ./run_headless.sh /opt/iar/common/bin
#   MCP_PORT=8123 ./run_headless.sh /opt/iar/common/bin
#   THRIFT_CSPYSERVER_EXE=none ./run_headless.sh /opt/iar/common/bin

# The server installs its own SIGTERM handler and tears both backends down on
# the way out, so exec'ing is enough - no trap or process babysitting needed
# here, unlike run_iaride.sh which owns the iaride process itself.
exec "$PYTHON" -m mcp_thrift_server "${ARGS[@]}" "$@"
