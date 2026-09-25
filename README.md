# cspy-mcp: MCP server for IAR C-SPY

[![CI](https://github.com/iarsystems/cspy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/iarsystems/cspy-mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server, `iar-cspy-mcp`, that exposes
the IAR C-SPY debugger, and the Embedded Workbench project and option
services, to AI assistants (Claude Code, Claude Desktop, VS Code Copilot, ...)
as tools. It drives the backend over its Thrift interfaces, on a simulator or
real hardware.

The server is a thin layer over the [`iar-cspy`](https://github.com/iarsystems/cspy-py)
Python API: each tool parses its arguments, calls the API, and returns plain
JSON. For scripts and pytest hardware-in-the-loop tests, use that API
directly.

Licensed under the [MIT License](LICENSE).

## Getting started

```sh
git clone https://github.com/iarsystems/cspy-mcp && cd cspy-mcp
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # the server, and iar-cspy from GitHub
iar-cspy-mcp --iar-path /opt/iar/ewarm   # or: python -m iar_cspy_mcp
```

## Add it to your MCP client

All examples use managed mode: the server starts the backend itself from an
IAR installation or build stage (the directory with `common/bin` under it).
Adjust the path to your machine.

**Claude Code**: `.mcp.json` in your project (or `~/.claude.json`), or
`claude mcp add cspy-debugger -- iar-cspy-mcp --iar-path /opt/iar/ewarm`:

```json
{
  "mcpServers": {
    "cspy-debugger": {
      "command": "iar-cspy-mcp",
      "args": ["--iar-path", "/opt/iar/ewarm"]
    }
  }
}
```

**Claude Desktop**: the same `mcpServers` block in `claude_desktop_config.json`
(Settings > Developer > Edit Config).

**VS Code Copilot**: `.vscode/mcp.json` (or `MCP: Add Server`):

```json
{
  "servers": {
    "cspy-debugger": {
      "type": "stdio",
      "command": "iar-cspy-mcp",
      "args": ["--iar-path", "C:\\iar\\ewarm-9.60"]
    }
  }
}
```

If `iar-cspy-mcp` is not on the client's `PATH`, use the virtualenv's Python:
`"command": "/abs/path/.venv/bin/python", "args": ["-m", "iar_cspy_mcp", ...]`.
From a checkout without installing, `run_headless.sh <iar-path>` does the same
over stdio (see [Helper scripts](#helper-scripts)).

**An already-running backend** (standalone mode): replace `--iar-path` with
its service registry:

```json
"args": ["--registry-host", "127.0.0.1", "--registry-port", "51926"]
```

## Requirements

Python 3.10+, and either an IAR installation (the directory with `common/bin`
under it; the backend is then started for you) or an already-running backend
to connect to.

## Backend modes

- **managed** (default): the server starts and supervises the backend from
  `--iar-path` (or `IAR_INSTALL_PATH`). `IarServiceLauncher` owns the service
  registry and hosts the IDE services, and `CSpyServer2` joins it with
  `-registry <port>`, so the `project_*`/`options_*` tools work alongside the
  debugger ones. An installation without the launcher runs CSpyServer2 alone,
  as does `--no-ide-services`. Unhealthy processes are restarted.
- **standalone**: `--registry-host`/`--registry-port` connect to a backend
  someone else runs: a Thrift-enabled `iaride`, or a hand-started
  `IarServiceLauncher`/`CSpyServer2`. The port is the service registry, not
  the debugger itself.

See [docs/ide-services.md](docs/ide-services.md) for the IDE services, and
the [backend notes](https://github.com/iarsystems/cspy-py/blob/main/docs/backend-notes.md)
for known backend behavior.

### Command line

| Option | Meaning |
| --- | --- |
| `--iar-path PATH` | IAR installation (managed mode). Normally the only option needed. |
| `--cspyserver2 PATH`, `--service-launcher PATH` | Individual program paths, overriding `--iar-path`. |
| `--cspyserver2-args ARGS` | CSpyServer2 arguments when it runs alone (default `-standalone -sockets`). |
| `--no-ide-services`, `--ide-services LIST` | Host no IDE services, or only `projectmanager`/`options`. |
| `--registry-host HOST`, `--registry-port PORT` | Connect to a running backend (standalone mode). |
| `--registry-service NAME` | Registry name of the debugger service (default `debugger`). |
| `--web`, `--web-port PORT` | Serve MCP over streamable HTTP on 127.0.0.1 instead of stdio. |
| `--probe-cspyserver2` | Start the managed backend, print its registry, exit. |

Every option also has an environment variable; see
[`.env.example`](.env.example). `MCP_TRANSPORT=streamable-http`,
`MCP_HOST` and `MCP_PORT` select the network transport without `--web`.

On stdio the server logs every backend RPC to stderr (`[thrift] call ...`),
which MCP hosts collect as server logs; stdout carries only the protocol.

### Helper scripts

Wrappers that run the server from a checkout without installing it. Each
takes the IAR path as its first argument or from `IAR_INSTALL_PATH`, and uses
`.venv/bin/python3` when present.

| Script | Transport | Backend |
| --- | --- | --- |
| `run_headless.sh <iar-path>` | stdio | managed: IarServiceLauncher + CSpyServer2 |
| `run_web.sh <iar-path>` | HTTP on `MCP_PORT` | the same managed backend |
| `run_iaride.sh <iar-path>` | HTTP on `MCP_PORT` | standalone: starts IarIde and uses its registry |

`NO_IDE_SERVICES=1` and `THRIFT_IDE_SERVICES=projectmanager` restrict what
the managed backend hosts.

## Tools

| Area | Tools |
| --- | --- |
| Session | `debugger_configure_and_start_session`, `debugger_configure_session`, `debugger_start_smp_session`, `debugger_stop_session`, `debugger_strict_cleanup`, `debugger_session_status`, `debugger_capabilities` |
| Run control | `debugger_go`, `debugger_stop`, `debugger_reset`, `debugger_step_over`, `debugger_wait_for_core_state`, `debugger_go_and_wait_for_core_state` |
| Target state | `debugger_eval_expression`, `debugger_register_snapshot`, `debugger_get_core_state`, `debugger_get_number_of_cores`, `debugger_get_cycle_counter`, `debugger_get_thread_list`, `debugger_get_modules`, `debugger_load_module`, `debugger_get_version`, `debugger_is_online` |
| Breakpoints | `breakpoints_set_on_ule`, `breakpoints_set_on_ule_with_category`, `breakpoints_set_from_descriptor`, `breakpoints_get_all`, `breakpoints_get`, `breakpoints_enable`, `breakpoints_remove`, `breakpoints_recently_hit` |
| Stack and symbols | `contextmanager_get_stack`, `contextmanager_get_stack_depth`, `contextmanager_get_context_info`, `contextmanager_get_locals`, `contextmanager_get_parameters`, `symbols_list_visible`, `symbols_lookup` |
| Memory and code | `memory_read`, `memory_write_hex`, `disassembly_disassemble_range`, `sourcelookup_get_source_ranges` |
| Terminal I/O | `libsupport_get_output`, `libsupport_clear_output`, `libsupport_push_input`, `libsupport_request_input`, `libsupport_request_input_binary` |
| List windows | `listwindow_list_services`, `listwindow_get_overview`, `listwindow_get_rows`, `listwindow_sliding_navigate`, `listwindow_get_notifications`, `listwindow_trace_status`, `listwindow_trace_set_enabled`, `listwindow_trace_clear` |
| Projects | `project_load_workspace`, `project_status`, `project_get_files`, `project_build`, `project_get_launch_config`, `project_configure_and_start_debug` |
| Options | `options_create_session`, `options_get_category_tree`, `options_get_option_tree`, `options_update_state`, `options_commit`, `options_destroy_session` |
| IDE services | `ide_services_status`, `ide_services_ensure`, `ide_services_stop_launcher` |
| Discovery | `thrift_connection_info`, `debugger_list_methods`, `debugger_error_taxonomy` |
| Raw RPC | `debugger_call`, `projectmanager_call`, `options_call` |

Each tool's description (what the MCP client shows the model) documents its
arguments.

### Response envelope and errors

The lifecycle, status, wait, project and options tools return
`{"ok": bool, "tool": name, "data": {...}, "error": null | {...}}`. Expected
negative outcomes (a timeout, a failed build, rejected option values) are
`ok: false` with a machine-readable `error.code` (`TIMEOUT`, `BUILD_FAILED`,
`OPTION_VERIFICATION_FAILED`, ...) rather than a tool error.
`debugger_error_taxonomy()` lists the codes. When a failure looks like a
backend crash, `details.backend_diagnostics` carries the backend's recent
output.

Most debugger tools need a started session and refuse with an explicit
message otherwise.

## Playbooks

These are compact flows for tool-using agents.

**Start a session**: `debugger_configure_and_start_session(launch_json)`, then
`debugger_session_status()`; continue when `data.started` is true.
`launch_json` is one entry of a C-SPY VS Code `launch.json` (a whole file with
`configurations` also works: the first one is used). In managed mode every
call starts from a fresh backend process. The session runs to the
configuration's `stopOnSymbol`; `data.ranToSymbol` reports whether it got there.

**From an Embedded Workbench project**: `project_load_workspace(path)`, then
`project_build()` (a failed build is `ok: false` with `data.output_tail`),
then `project_configure_and_start_debug()`. No launch.json is needed.

**Run and wait**: `debugger_go_and_wait_for_core_state(desired_state=0, timeout_ms=5000)`.
Core states are 0 = stopped, 1 = running, 2 = sleeping, 3 = unknown, 4 = no power.
On `error.code == "TIMEOUT"`, retry with a longer timeout or call `debugger_stop()`.

**Breakpoints**: `breakpoints_set_on_ule("main", 1)`, where `1` is a code
breakpoint and 2/3/4 are read/write/read-write watchpoints. ULEs are
expressions (`main`, `func+4`), addresses (`0x100`), or full-form source
locations (`{/abs/path/file.c}.123.1`); `main()` and `file.c:123` shorthand
are backend-dependent. `breakpoints_set_from_descriptor` only takes
descriptors returned by `breakpoints_get_all()`.

**Probe before advanced calls**: `debugger_capabilities()` returns the backend
mode, services, debugger methods, and any probe errors.

**Recover**: `debugger_strict_cleanup(reset_target=true)`, inspect
`data.errors[*].details.backend_diagnostics`, then start again.

**Non-intrusive attach**: use a launch configuration with `"request": "attach"`,
`"attachToTarget": true`, `"leaveTargetRunning": true` and all downloads
suppressed. Check `debugger_get_core_state` before deciding to call
`debugger_stop()`, and avoid `debugger_reset()`.

**Options**: `options_create_session` → `options_get_category_tree` →
`options_get_option_tree` → `options_update_state` → `options_commit` or
`options_destroy_session`; see [docs/ide-services.md](docs/ide-services.md#using-the-optionsservice-tools).
For plain option reading and writing, `projectmanager_call("GetOptionsForConfiguration", ...)`
needs no session.

**When a tool cannot do it**: for multi-step work such as loops, conditional
logic, or collecting a lot of state, writing a short script against the
[`iar_cspy` Python API](https://github.com/iarsystems/cspy-py) is often simpler and faster than many
tool calls. Its objects mirror these tools.

## How it works

```
 MCP client ──MCP──▶ iar-cspy-mcp ──▶ iar-cspy ──Thrift──▶ service registry ──▶ CSpyServer2  (debugger, breakpoints, memory, ...)
                                                                         └─▶ IarServiceLauncher (ProjectManager, OptionsService)
```

`iar-cspy` resolves every service through the registry, supervises the
managed processes, and hosts the callback services the backend expects from
a frontend (debug events, terminal I/O, list window notifications).

## Repository layout

```
src/iar_cspy_mcp/    the server: _app.py (app, client, envelope), cli.py, server.py
  tools/             one module of MCP tools per area
tests/               tool tests (against iar_cspy.testing fakes) and live tests
examples/
  firmware/          Cortex-M3 simulator program + launch.json used by the live tests
  mcp/               MCP client configs and a stdio protocol client
docs/                IDE services
mcp_thrift_server/   deprecated shim for the server's old module name (see below)
run_headless.sh, run_web.sh, run_iaride.sh   start the server from a checkout
```

## Development

```sh
pip install -r requirements-dev.txt                 # the server editable + pytest
pip install -e ../cspy-py                           # optional, afterwards: your checkout of iar-cspy
pytest                                              # unit tests; live tests skip without a backend
pytest -m live --cspy-iar-path=/opt/iar/ewarm       # live tests, managed backend
pytest -m live --cspy-cspyserver2=/path/CSpyServer2 # live tests, CSpyServer2 only
```

The tools live in `src/iar_cspy_mcp/tools/`, one module per area. Keep them
thin: argument parsing and result shaping belong here, behavior belongs in
`iar_cspy`. The tool tests run against `iar_cspy.testing.fake_client()`, and
`tests/test_live.py` covers a real backend on the bundled firmware
(`examples/firmware`), including a full MCP stdio round trip. CI
(`.github/workflows/ci.yml`) runs the unit tests on Python 3.10-3.12, builds
the distribution, and runs the live tests against the public cxarm toolchain.

`python -m mcp_thrift_server`, the server's old name, still works from a
checkout (with a deprecation notice), so existing MCP client configs keep
working. Point them at `iar-cspy-mcp` / `python -m iar_cspy_mcp` instead.
