#!/usr/bin/env bash
# MCP server over stdio, for an agent to launch directly.
#
# Starts and supervises the whole backend itself: an IarServiceLauncher hosting
# the IDE platform services (ProjectManager, OptionsService) and owning the
# service registry, plus a CSpyServer2 that joins that same registry. Every tool
# is available - debugger, breakpoints, project and options.
#
# Headless equivalent of run_iaride.sh: same services, no GUI. Use run_web.sh
# for the same thing over HTTP instead of stdio.
#
#   ./run_headless.sh <iar-stage>
#
# <iar-stage> is a stage or installation directory, i.e. the one with common/bin
# under it. Every program used here is taken from <iar-stage>/common/bin.
#
# Point an MCP host at it, for example in .mcp.json:
#
#   {
#     "mcpServers": {
#       "cspy-debugger": {
#         "command": "/abs/path/to/cspy-mcp/run_headless.sh",
#         "args": ["/opt/iar/ewarm"]
#       }
#     }
#   }
#
# Env vars:
#   IAR_STAGE            the stage, if not given as an argument
#   NO_IDE_SERVICES=1    debugger only, no IarServiceLauncher
#   THRIFT_IDE_SERVICES  restrict which IDE services to host,
#                        e.g. "projectmanager"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MCP hosts invoke this by absolute path from their own project directory, so cd
# here for `python -m mcp_thrift_server` to find the package and the thrift/ IDL.
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

STAGE="${1:-${IAR_STAGE:-}}"
[ "$#" -gt 0 ] && shift

# Diagnostics go to stderr: on stdio, stdout carries the MCP protocol and
# nothing else. The backends' own output is captured to log files by the bridge.
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

ARGS=(--iar-stage "$STAGE")

# Dropping the IDE services leaves a plain managed CSpyServer2, which is all the
# debugger_* tools need.
if [ -n "${NO_IDE_SERVICES:-}" ]; then
  echo "Hosting no IDE services: debugger tools only." >&2
  ARGS+=(--no-ide-services)
fi

if [ -n "${THRIFT_IDE_SERVICES:-}" ]; then
  ARGS+=(--ide-services "$THRIFT_IDE_SERVICES")
fi

echo "IAR stage: $STAGE (MCP over stdio)" >&2

# The IDE services are loaded lazily: the first project_* tool call starts the
# ProjectManager, the first options_* call starts the OptionsService. Call the
# ide_services_ensure tool to warm them up front, or ide_services_status to see
# what the registry currently holds.
#
# Any remaining arguments are forwarded to the module. exec'ing keeps this
# script's pid, so the MCP host's signals reach the server directly; it installs
# its own SIGTERM handler and tears the backends down on the way out.
exec "$PYTHON" -m mcp_thrift_server "${ARGS[@]}" "$@"
