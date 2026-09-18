# MCP Thrift Server (Python)

[![CI](https://github.com/iarsystems/cspy-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/iarsystems/cspy-mcp/actions/workflows/ci.yml)

This project runs an MCP server that talks to a Thrift backend (C-SPY style IDL) and exposes debugger capabilities as MCP tools.

Licensed under the [MIT License](LICENSE).

## Quick Start: Add To Your MCP Client

All examples use managed mode: the MCP server spawns `CSpyServer2.exe` itself
and auto-detects the registry port. Adjust the two paths (`CSpyServer2.exe`
and this repo) to your machine. Install dependencies first
(`pip install -r requirements.txt`).

### Claude Code

Add to `.mcp.json` in your project root (or `~/.claude.json` for user scope):

```json
{
  "mcpServers": {
    "cspy-debugger": {
      "command": "python",
      "args": ["-m", "mcp_thrift_server", "--cspyserver2", "C:\\iar\\qtarm-10.2.1\\common\\bin\\CSpyServer2.exe"],
      "env": { "PYTHONPATH": "C:\\path\\to\\this-repo" }
    }
  }
}
```

Or from the terminal:

```bash
claude mcp add cspy-debugger --env PYTHONPATH=C:\path\to\this-repo -- python -m mcp_thrift_server --cspyserver2 "C:\iar\qtarm-10.2.1\common\bin\CSpyServer2.exe"
```

### Claude Desktop

Add the same `mcpServers` block to `claude_desktop_config.json`
(Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "cspy-debugger": {
      "command": "python",
      "args": ["-m", "mcp_thrift_server", "--cspyserver2", "C:\\iar\\qtarm-10.2.1\\common\\bin\\CSpyServer2.exe"],
      "env": { "PYTHONPATH": "C:\\path\\to\\this-repo" }
    }
  }
}
```

### VS Code Copilot

Add to `.vscode/mcp.json` in your workspace (or run `MCP: Add Server` from the
Command Palette):

```json
{
  "servers": {
    "cspy-debugger": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "mcp_thrift_server", "--cspyserver2", "C:\\iar\\qtarm-10.2.1\\common\\bin\\CSpyServer2.exe"],
      "cwd": "C:\\path\\to\\this-repo"
    }
  }
}
```

### Connecting to an already-running backend (external mode)

Replace the `--cspyserver2` argument with registry flags in any config above:

```json
"args": ["-m", "mcp_thrift_server", "--registry-host", "127.0.0.1", "--registry-port", "51926"]
```

Environment variables (`THRIFT_FILE`, `THRIFT_INCLUDE_DIRS`, ...) are only
needed when your thrift IDLs live outside this repo; see
[Backend Modes](#backend-modes) below.

## What it provides

- MCP server over `stdio` (default) or `streamable-http`
- Managed backend mode: spawns and supervises `CSpyServer2.exe`, auto-detects
  the registry port, and restarts the backend on failure
- Runtime loading of the bundled `cspy.thrift` IDL via `thriftpy2`
- Registry-aware service resolution (debugger, breakpoints, contextmanager,
  memory, disassembly, sourcelookup, symbols, listwindow, libsupport)
- Launcher backend mode: also hosts the IDE platform services (ProjectManager,
  OptionsService) via `IarServiceLauncher`, sharing one service registry with
  `CSpyServer2` - see [docs/ide-services.md](docs/ide-services.md)
- Tools for session lifecycle, run control, breakpoints/watchpoints, stack and
  locals inspection, memory read/write, disassembly, source lookup, symbol
  lookup, terminal I/O capture, error taxonomy, and arbitrary debugger RPC calls
- AI-first response envelopes with machine-readable error codes and backend
  crash diagnostics

## Prerequisites

- Python 3.10+
- An IAR toolchain installation providing `CSpyServer2.exe` (managed mode),
  or an already-running CSpyServer2/Service Registry to connect to (external mode)
- Thrift IDLs are bundled in this repo (`thrift/cspy.thrift` plus includes);
  nothing extra is needed unless your IDLs live elsewhere

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Optional: configure environment variables (see `.env.example`). With the
   bundled thrift files, `THRIFT_FILE` and `THRIFT_INCLUDE_DIRS` are not needed;
   the server auto-detects `thrift/cspy.thrift` and uses its directory as
   include path.

If you connect to an externally started `CSpyServer2.exe -standalone`:
- The printed/known port may be the Service Registry, not the Debugger service itself.
- Set `THRIFT_REGISTRY_PORT` (or pass `--registry-port`) to that registry port
  and this bridge will auto-resolve the real `debugger` endpoint.

## Backend Modes

This server supports managed, external and launcher backend operation.

1. `managed` (default):
- MCP server starts `CSpyServer2.exe` itself using:
  - executable: provided via CLI (`--cspyserver2`)
  - args: `THRIFT_CSPYSERVER_ARGS` (default `-standalone -sockets`)
- It parses CSpyServer2 stdout for:
  - `Service registry running on local socket on port: <port>`
- The detected registry port is used automatically for service resolution.
- If the managed process is unhealthy, the server attempts restart when
  `THRIFT_CSPYSERVER_RESTART_ON_FAILURE=1`.

2. `external`:
- Connect to an existing CSpyServer2/registry using CLI flags:
  - `--registry-host`
  - `--registry-port`
  - optional `--registry-service` (default: `debugger`)

3. `launcher`:
- MCP server starts `IarServiceLauncher -standalone -sockets`, which owns the
  service registry and hosts the IDE platform services (ProjectManager,
  OptionsService). A `CSpyServer2` given via `--cspyserver2` then joins that
  same registry with `-sockets -registry <port>`, so debugger and IDE services
  resolve through one registry.
- Selected by `--service-launcher <path>` or `THRIFT_SERVICE_LAUNCHER_EXE`.
- Required for the `project_*` and `options_*` tools: a standalone
  `CSpyServer2` has no service manager and cannot host those services.
- `--cspyserver2` is optional here; omit it for a project/options-only setup.

```sh
python -m mcp_thrift_server \
  --service-launcher /abs/path/common/bin/IarServiceLauncher \
  --cspyserver2      /abs/path/common/bin/CSpyServer2
```

See [docs/ide-services.md](docs/ide-services.md) for the full picture,
including connecting to a Thrift-enabled `iaride` instead.

## Run

Managed mode (spawns CSpyServer2, auto-detects registry port):

```powershell
python -m mcp_thrift_server --cspyserver2 "C:\iar\qtarm-10.2.1\common\bin\CSpyServer2.exe"
```

Optional custom CSpyServer2 args:

```powershell
python -m mcp_thrift_server --cspyserver2 "C:\iar\qtarm-10.2.1\common\bin\CSpyServer2.exe" --cspyserver2-args "-standalone -sockets"
```

External mode (connect to an already-running backend registry):

```powershell
python -m mcp_thrift_server --registry-host 127.0.0.1 --registry-port 51926
```

The server starts in `stdio` transport mode by default. In `stdio` mode, the
process is expected to block while waiting for an MCP client, and you should see:
`MCP server ready (stdio). Waiting for an MCP client connection...`

Simple web mode option (HTTP on localhost):

```powershell
python -m mcp_thrift_server --web --web-port 8000
```

Explicit HTTP transport via environment (use `MCP_HOST="0.0.0.0"` to listen on
all interfaces; the server listens on the single port `MCP_PORT`):

```powershell
$env:MCP_TRANSPORT="streamable-http"
$env:MCP_HOST="127.0.0.1"
$env:MCP_PORT="8000"
python -m mcp_thrift_server
```

Terminal-only health probe (starts managed CSpyServer2, parses registry port,
prints status, exits):

```powershell
python -m mcp_thrift_server --cspyserver2 "C:\iar\qtarm-10.2.1\common\bin\CSpyServer2.exe" --probe-cspyserver2
```

### Helper scripts (Linux/macOS)

Three thin wrappers that serve the MCP endpoint over HTTP on `MCP_PORT`
(default 8000), one per backend arrangement. Each takes its path as the first
argument or from an environment variable, and uses `.venv/bin/python3` when
present.

| Script | Backend | Tools available |
| --- | --- | --- |
| `run_web.sh <CSpyServer2>` | managed: spawns CSpyServer2 | `debugger_*`, `breakpoints_*`, ... |
| `run_headless.sh <common/bin>` | launcher: spawns IarServiceLauncher + CSpyServer2 on one registry | all of the above **plus** `project_*` and `options_*` |
| `run_iaride.sh <iaride>` | external: starts IarIde and resolves through its registry | same as `run_headless.sh`, but with the IDE's GUI |

```sh
./run_web.sh      /opt/iar/ewarm/common/bin/CSpyServer2
./run_headless.sh /opt/iar/ewarm/common/bin
./run_iaride.sh   /opt/iar/ewarm/common/bin/iaride

MCP_PORT=8123 ./run_headless.sh /opt/iar/ewarm/common/bin
THRIFT_CSPYSERVER_EXE=none ./run_headless.sh /opt/iar/ewarm/common/bin   # no debugger
THRIFT_IDE_SERVICES=projectmanager ./run_headless.sh /opt/iar/ewarm/common/bin
```

`run_headless.sh` is the headless equivalent of `run_iaride.sh`: same IDE
services, no GUI. See [docs/ide-services.md](docs/ide-services.md).

## Testing (pytest)

Install test dependencies:

```powershell
pip install -r requirements-dev.txt
```

Run fast unit tests (mocked backend):

```powershell
pytest -q tests/test_server_tools_unit.py
```

Run the full default suite (live tests are skipped unless enabled):

```powershell
pytest -q
```

Run live backend tests:

```powershell
pytest -q tests -m live --cspyserver2 "C:\\iar\\qtarm-10.2.1\\common\\bin\\CSpyServer2.exe"
```

Continuous integration (GitHub Actions, `.github/workflows/ci.yml`):
- `unit`: compile check + unit tests on Python 3.10/3.11/3.12 (Ubuntu).
- `live-sim`: downloads the cxarm toolchain from the public
  `iarsystems/arm` GitHub release, locates `CSpyServer2`, and runs the live
  simulator tests plus `examples/run_live_demo.py` against the bundled
  `test.out` ELF. Set the `IAR_LMS_BEARER_TOKEN` repository secret if license
  checkout is required in CI. The toolchain is cached between runs.

Live test assets bundled in repo:
- `tests/live_assets/launch.json`
- `tests/live_assets/test.ewp`
- `tests/live_assets/Debug/Exe/test.out`

So `pytest -q -m live` can run without external launch/project/output files.
You still need a working C-SPY installation/executable.

One-command validation (unit + live):

```powershell
./scripts/run_validation.ps1 -CSpyServerExe "C:\iar\qtarm-10.2.1\common\bin\CSpyServer2.exe"
```

Optional launch override:

```powershell
./scripts/run_validation.ps1 -CSpyServerExe "C:\iar\qtarm-10.2.1\common\bin\CSpyServer2.exe" -LaunchJson "E:\path\to\launch.json"
```

Auto handlers are always-on defaults:
- Some backends require `debugger.eventhandler` before configure/start succeeds.
- Terminal I/O and exit/assert capture requires `libsupport` callbacks.
- Keeping these handlers on by default prevents lifecycle foot-guns.

Live test lifecycle expectation:
- `debugger_configure_session(launch_json)` performs resolve + configure.
- `debugger_start_smp_session()` must be called after configure.
- Effective startup sequence is `resolve -> configure -> start`.

## MCP tools exposed

- `thrift_connection_info()`
- `debugger_list_methods()`
- `debugger_get_version()`
- `debugger_is_online()`
- `debugger_get_number_of_cores()`
- `debugger_get_core_state(core=0)`
- `debugger_session_status()`
- `debugger_configure_session(launch_json)`
- `debugger_start_smp_session()`
- `debugger_configure_and_start_session(launch_json)`
- `debugger_stop_session()`
- `debugger_strict_cleanup(reset_target=False)`
- `debugger_capabilities()`
- `debugger_error_taxonomy()`
- `debugger_load_module(filename)`
- `debugger_get_modules()`
- `debugger_register_snapshot(group="CPU Registers (ABI)", limit=64)`
- `debugger_go()`
- `debugger_stop()`
- `debugger_reset()`
- `debugger_step_over()`
- `debugger_get_thread_list()`
- `debugger_get_cycle_counter(core=0)`
- `debugger_eval_expression(expression, context_json="", format=0, dereference=False)`
- `debugger_wait_for_core_state(desired_state=0, core=0, timeout_ms=5000, poll_interval_ms=50)`
- `debugger_go_and_wait_for_core_state(desired_state=0, core=0, timeout_ms=5000, poll_interval_ms=50)`
- `debugger_call(method, args_json="[]")`
- `breakpoints_get_all()`
- `breakpoints_get(id)`
- `breakpoints_set_from_descriptor(descriptor)`
- `breakpoints_set_on_ule(ule, access_type=1)`
- `breakpoints_set_on_ule_with_category(ule, access_type, category_id)`
- `breakpoints_enable(id, enable=True)`
- `breakpoints_remove(id)`
- `breakpoints_recently_hit()`
- `contextmanager_get_stack(context_json="", low=0, high=20)`
- `contextmanager_get_stack_depth(context_json="", max_depth=256)`
- `contextmanager_get_context_info(context_json="")`
- `contextmanager_get_locals(context_json="")`
- `contextmanager_get_parameters(context_json="")`
- `symbols_list_visible(context_json="")`
- `symbols_lookup(name, context_json="", prefix=False)`
- `memory_read(zone_id, address, wordsize=1, bitsize=8, count=16)`
- `memory_write_hex(zone_id, address, data_hex, wordsize=1, bitsize=8, count=None)`
- `disassembly_disassemble_range(from_zone_id, from_address, to_zone_id, to_address, context_json="")`
- `sourcelookup_get_source_ranges(zone_id, address)`
- `libsupport_get_output(clear=False, max_chars=4000)`
- `libsupport_clear_output()`
- `libsupport_push_input(text, append_newline=False)`
- `libsupport_request_input_binary(len)`
- `libsupport_request_input(len)`
- `listwindow_list_services(name_filter="listwindow")`
- `listwindow_get_overview(service_name)`
- `listwindow_get_rows(service_name, first_row=0, max_rows=50)`
- `listwindow_get_notifications(clear=False)`
- `project_load_workspace(file_path, fetch_dependency_data=True)`
- `project_status()`
- `project_get_files(project_path="", config_name="", collection="ProjFiles")`
- `project_build(project_path="", config_name="", num_parallel_builds=4, max_output_lines=200)`
- `project_get_launch_config(project_path="", config_name="")`
- `project_configure_and_start_debug(project_path="", config_name="", build_first=True, start_session=True)`
- `projectmanager_call(method, args_json="[]")`
- `ide_services_status()`
- `ide_services_ensure(services="", force=False)`
- `ide_services_stop_launcher()`
- `options_create_session(project_path="", config_name="", node_path_or_index="", show_hidden_options=False)`
- `options_destroy_session(session_id)`
- `options_get_category_tree(session_id)`
- `options_get_option_tree(session_id, tree_id)`
- `options_update_state(session_id, tree_id, updated_json="[]", created_json="[]", deleted_json="[]")`
- `options_commit(session_id)`
- `options_call(method, args_json="[]")`

### ProjectManager tools (build/debug/edit loop)

The `project_*` tools talk to the `ProjectManager` thrift service
(`projectmanager.thrift`, registry name
`com.iar.thrift.service.projectmanager`, override with
`THRIFT_PROJECTMANAGER_SERVICE_NAME`). That service is **not** available from
a standalone `CSpyServer2`, which has no service manager and cannot load it.
Run the bridge in [launcher mode](docs/ide-services.md) so it hosts the service
itself via `IarServiceLauncher`, or point it at a backend that already does
(a hand-started launcher, or a Thrift-enabled `iaride`). `ide_services_status()`
reports what the current backend provides.

Canonical edit → build → debug loop:
1. `project_load_workspace("/abs/path/workspace.eww")` (or a bare `.ewp`)
2. edit source files on disk
3. `project_build()` — synchronous; a failed build returns `ok=false` with
   the trailing build output in `data.output_tail` instead of raising
4. `project_configure_and_start_debug()` — rebuilds (optional), fetches the
   launch configuration for the project's current build configuration via
   `GetLaunchConfigurationForConfiguration`, passes it straight to
   `Debugger.configureSession`, and starts the SMP session. No hand-written
   `launch.json` is needed.
5. Use the regular `debugger_*` / `breakpoints_*` tools, then
   `debugger_stop_session()` and loop back to step 2.

`project_get_launch_config()` returns the launch configuration as JSON if
you want to inspect or tweak it before configuring a session manually.
Empty `project_path` / `config_name` arguments mean "the current project" /
"its current configuration"; `projectmanager_call` is the generic fallback
for the rest of the ProjectManager API (workspace/node/option editing,
toolchains, batch builds, ...).

`debugger_list_methods` returns the RPC names parsed from `cspy.thrift`.

`debugger_register_snapshot` returns register metadata and values (hex and unsigned little-endian integer) for a whole register group in one call.

Standard response envelope (AI-first tools):
- The following tools return a stable envelope shape:
  `{"ok": <bool>, "tool": <name>, "data": <payload>, "error": <object|null>}`
- Current enveloped tools:
  - `debugger_session_status`
  - `debugger_configure_session`
  - `debugger_start_smp_session`
  - `debugger_configure_and_start_session`
  - `debugger_stop_session`
  - `debugger_strict_cleanup`
  - `debugger_capabilities`
  - `debugger_wait_for_core_state`
  - `debugger_go_and_wait_for_core_state`
  - `project_status`
  - `project_get_files`
  - `project_build`
  - `project_get_launch_config`
  - `project_configure_and_start_debug`
- Timeout-style outcomes use `ok=false` with machine-readable `error.code` (for example `TIMEOUT`).
- Use `debugger_error_taxonomy()` to discover known error codes/categories and recovery hints.
- When available, structured error `details` may include `backend_diagnostics`
  with managed backend crash/output context to speed up recovery decisions.

Breakpoint usage notes:
- `breakpoints_set_on_ule` is the primary creation API.
- For code breakpoints, set `access_type=1` (fetch/execute).
- ULE is parsed by the debugger Universal Location Expression parser.
- Supported ULE categories:
  - expression ULEs: `main`, `func+4`, `*ptr`
  - absolute ULEs: `0x100`, `Memory:0x42`
  - source ULEs (reliable full form): `{E:/path/file.c}.123.1`
  - optional size suffix: `<ule>@<size>`
- Source shorthand like `file.c:123` can be backend-dependent; prefer the full
  source ULE form shown above.
- `breakpoints_set_on_ule*` now fail explicitly if backend returns an invalid
  breakpoint object (for example `valid=false` / `id=0`) instead of silently
  returning it.
- `breakpoints_set_from_descriptor` expects opaque descriptor values from
  `breakpoints_get_all()` and is intended for round-trip restore/update,
  not free-form descriptor construction.
- `breakpoints_set_on_ule_with_category` category IDs can be translated by
  backend (for example `STD_CODE` to `STD_CODE2`).
- If breakpoint calls fail with backend transport resets, the backend session
  may have crashed/reset; restart C-SPY and reconfigure session.

AI usage notes:
- Prefer dedicated tools over `debugger_call` when available.
- Prefer calling `debugger_session_status()` first to confirm lifecycle/backend state before deeper operations.
- Use `debugger_capabilities()` when you need a one-shot view of backend mode,
  available services, and currently discoverable debugger methods.
- For `debugger_configure_session`, pass one configuration object JSON, not the outer `{"configurations": [...]}` wrapper.
- Required startup flow (recommended):
  1. `debugger_configure_session(launch_json)`
  2. `debugger_start_smp_session()`
- One-call happy path:
  1. `debugger_configure_and_start_session(launch_json)`
  - In `managed` backend mode, this wrapper always performs a strict cleanup
    first and starts from a fresh CSpyServer2 process before
    resolve/configure/start.
  - In managed mode, no backend session/runtime state is expected to carry
    over between calls.
  - In `external` backend mode, this wrapper performs best-effort handoff
    teardown when stale/active session state is detected.
- Equivalent low-level flow:
  1. `debugger_call("resolveLaunchConfiguration", ...)`
  2. `debugger_call("configureSession", ...)`
  3. `debugger_start_smp_session()` (or `debugger_call("startSession", ...)` if applicable)
- Important: `debugger_configure_session` does not start the session; always call
  `debugger_start_smp_session` after configure before stack/context/breakpoint-heavy operations.
- The MCP server enforces this lifecycle invariant for most debugger-dependent
  tools and returns an explicit error if configure/start has not completed.
- `debugger.eventhandler` and `libsupport` registration are handled automatically
  by the MCP wrapper during configure/start flows; no manual registration tool call
  is required in normal usage.
- Stability note: `debugger_configure_session` intentionally does not call
  `stopSession()` internally. In some backend lifecycle states, forcing
  `stopSession()` during reconfigure can trigger backend assertions/crashes.
  Use `debugger_stop_session()` explicitly only when you intend to tear down
  the current session.
- `debugger_stop_session()` remains idempotent for local lifecycle state.
  In `managed` backend mode it also shuts down the managed CSpyServer2 process,
  so the next startup uses a fresh backend process.
- `debugger_strict_cleanup(reset_target=False)` is the strongest recovery tool:
  it best-effort stops session, clears local caches/buffers, and shuts down the
  managed backend process to restore a known-good baseline.
- If execution state becomes inconsistent, call `debugger_reset()` before retrying start/go.
- Common non-intrusive attach flow (read state, avoid perturbing target):
  1. Build an attach config with:
     - `request: "attach"`
     - `attachToTarget: true`
     - `download.suppressAllDownloads: true`
     - `download.suppressProgramDownload: true`
     - `leaveTargetRunning: true`
  2. Call `debugger_configure_session(launch_json)`.
  3. Call `debugger_start_smp_session()`.
  4. Check run state first (for example `debugger_call("getCoreState", "[0]")` or stack/context).
  5. Read registers/state directly when possible.
  6. Only call `debugger_stop()` if state confirms the core is running and halt is required for the read.
  7. Avoid `debugger_reset()` in attach mode unless explicitly requested.
- Eventhandler listener timeout defaults to 3600000 ms (1 hour). Override with
  `THRIFT_EVENTHANDLER_CLIENT_TIMEOUT_MS` if needed.
- `debugger_call` is best for simple scalar/list arguments; nested thrift
  structs may require dedicated wrappers. It accepts:
  - JSON array for positional args, example: `"[123, \"abc\"]"`
  - JSON object for keyword args, example: `"{\"sessionConfig\": {...}}"`
- Listwindow/trace note: in standalone/headless sessions, instruction trace
  listwindow services may not be published in ServiceRegistry. Use
  `listwindow_list_services("")` to confirm availability before attempting row reads.

### OptionsService tools (build/debug option GUI model)

The `options_*` tools talk to the `OptionsService` thrift service
(`OptionsService.thrift`, registry name `com.iar.optionsservice`, override with
`THRIFT_OPTIONSSERVICE_SERVICE_NAME`). Like ProjectManager it is hosted by
`IarServiceLauncher`/`iaride`, not by `CSpyServer2` - see
[docs/ide-services.md](docs/ide-services.md).

`OptionsService` is the presentation view of a configuration's options: the
same category/option tree the IDE's options dialog renders, served as XML, plus
backend validation of proposed values. It is session based:

1. `options_create_session("/abs/path/p.ewp", "Debug")` -> `session_id`
2. `options_get_category_tree(session_id)` -> `<pages>` XML; take a page id
   such as `General-GEN-TARGET` from it
3. `options_get_option_tree(session_id, "General-GEN-TARGET")` -> the option
   widgets and their current values
4. `options_update_state(session_id, tree_id, updated_json)` -> validated tree
   plus `verification_errors`; the envelope's `ok` is false when any value was
   rejected
5. `options_commit(session_id)` -> writes the state into the configuration and
   marks the project modified; persist with
   `projectmanager_call("SaveEwpFile", ...)`
6. `options_destroy_session(session_id)`

For plain option reading/writing prefer ProjectManager's
`GetOptionsForConfiguration` / `ApplyOptionsForConfiguration` via
`projectmanager_call` - a flat list of option ids and values, no session or XML.
Use `options_*` when you want the GUI's grouping and presentation, or the
backend's verdict on a value.

Note that *reading* options can mutate the configuration (some target options
persist derived values as a side effect of being read), so treat a session as a
transaction: do the reads and writes you need, then commit or destroy it.

`ide_services_status()` reports how the IDE services are currently hosted and
is the first thing to call when an `options_*` or `project_*` tool cannot reach
its service.

## AI Playbooks

These are compact, canonical flows intended for tool-using AI agents.

Playbook A: Standard debug session bootstrap
1. `debugger_configure_and_start_session(launch_json)`
2. `debugger_session_status()`
3. Continue only if `ok=true` and `data.started=true`.

Playbook B: Safe capability probe before advanced calls
1. `debugger_capabilities()`
2. Inspect `data.debugger_methods`, `data.services`, and `data.errors`.
3. Branch behavior based on discovered methods/services.

Playbook C: Run and wait deterministically
1. `debugger_go_and_wait_for_core_state(desired_state=0, core=0, timeout_ms=5000)`
2. If `ok=false` and `error.code=="TIMEOUT"`, either retry with higher timeout or call `debugger_stop()`.

Playbook D: Breakpoint round-trip
1. `breakpoints_set_on_ule("main", 1)`
2. `breakpoints_get_all()`
3. Persist `descriptor` values only from `breakpoints_get_all()` for future restore.

Playbook E: Failure recovery baseline
1. `debugger_strict_cleanup(reset_target=true)`
2. If `ok=false`, inspect `data.errors[*].details.backend_diagnostics` when present.
3. Re-run bootstrap from Playbook A.

## Notes

- If your backend uses custom transports/protocols (SSL, multiplexing, framed transport variants), adapt `mcp_thrift_server/thrift_client.py`.
- Current bridge expects socket endpoints for the final service call. Registry-discovered non-socket (named pipe) endpoints are reported as unsupported.

## Quick MCP protocol smoke test

This verifies MCP transport and tools/call over stdio, not only direct Python
imports. Adjust the registry env values inside the script for your backend,
then run:

```powershell
python smoke_test_mcp_stdio.py
```

It starts the MCP server as a stdio subprocess, lists tools, and calls
`debugger_get_version`, `debugger_is_online`, and `debugger_list_methods`.
