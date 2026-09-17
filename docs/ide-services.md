# Hosting the IDE services: ProjectManager and OptionsService

The `project_*` and `options_*` tools talk to two IDE platform Thrift services
that **`CSpyServer2` cannot host**. This document explains why, how to start
them, and how to verify the result.

- [Why CSpyServer2 is not enough](#why-cspyserver2-is-not-enough)
- [Option A: let the bridge host them (launcher mode)](#option-a-let-the-bridge-host-them-launcher-mode)
- [Option B: connect to a backend that already hosts them (external mode)](#option-b-connect-to-a-backend-that-already-hosts-them-external-mode)
- [Verifying and troubleshooting](#verifying-and-troubleshooting)
- [Reference: services, manifests, environment](#reference-services-manifests-environment)
- [Using the OptionsService tools](#using-the-optionsservice-tools)
- [Known quirks](#known-quirks)

## Why CSpyServer2 is not enough

`CSpyServer2 -standalone -sockets` starts a service registry and registers the
debugger side of the platform:

```
breakpoints                debugger.contextmanager     sourcelookup
debugger                   debugger.memory             disassembly
com.iar.thrift.service.registry                        SharedAmpSync.*
```

That is the whole list. `CSpyServer2` has no *service manager*, so it cannot
load additional service implementations — and ProjectManager and OptionsService
are exactly that: implementations in dynamic libraries
(`libProjectManagerHandler.so` / `ProjectManagerHandler.dll`,
`libOptionsService.so` / `OptionsService.dll`) that sit next to `CSpyServer2` in
`<install>/common/bin`.

`IarServiceLauncher`, in the same directory, is the generic host for them. With
`-standalone -sockets` it:

1. starts a service registry and writes its location to
   `CSpyServer2-ServiceRegistry.txt` in its working directory,
2. registers a `com.iar.thrift.service.manager` service
   (`ServiceManager.thrift`),
3. loads service libraries described by JSON manifest files — given on the
   command line, or at runtime via
   `CSpyServiceManager.startServicesFromJsonManifest`.

The key detail that makes this pleasant: a registry can be **shared**.
`CSpyServer2 -sockets -registry <port>` joins an existing registry instead of
starting its own. So one launcher-owned registry ends up holding the debugger
services *and* the IDE services, and every tool in this bridge resolves through
that single registry with no per-service special casing.

## Option A: let the bridge host them (launcher mode)

`THRIFT_CSPYSERVER_MODE=launcher` makes the MCP server own the whole backend:

```
MCP server
 ├── spawns IarServiceLauncher -standalone -sockets   → owns the registry
 │      └── loads projectmanager.json / OptionsService.json on demand
 └── spawns CSpyServer2 -sockets -registry <port>     → joins that registry
```

Point `--service-launcher` at `IarServiceLauncher` and `--cspyserver2` at
`CSpyServer2`:

```sh
python -m mcp_thrift_server \
  --service-launcher /path/to/install/common/bin/IarServiceLauncher \
  --cspyserver2      /path/to/install/common/bin/CSpyServer2
```

or via the environment:

```sh
export THRIFT_CSPYSERVER_MODE=launcher
export THRIFT_SERVICE_LAUNCHER_EXE=/path/to/install/common/bin/IarServiceLauncher
export THRIFT_CSPYSERVER_EXE=/path/to/install/common/bin/CSpyServer2
python -m mcp_thrift_server
```

On Windows use `IarServiceLauncher.exe` / `CSpyServer2.exe`.

The launcher comes up first and the registry it publishes is what everything
resolves through, so ordering is handled for you. The individual IDE services
are started lazily: the first `project_*` call loads the ProjectManager, the
first `options_*` call loads the OptionsService. Call `ide_services_ensure()` to
warm them up front, or set `THRIFT_AUTO_IDE_SERVICES=0` to require it
explicitly.

`--cspyserver2` is optional. Omit it for a project/options-only setup — the
launcher still starts and `project_*`/`options_*` work; only the `debugger_*`
tools will fail to resolve the `debugger` service.

Both processes are supervised the same way managed mode supervises
`CSpyServer2`: stdout goes to a log file, a recent tail is attached to failure
envelopes (`service_launcher_diagnostics`), and both are stopped on exit —
CSpyServer2 first, then the launcher.

### As an MCP client entry

```json
{
  "mcpServers": {
    "cspy-debugger": {
      "command": "python",
      "args": [
        "-m", "mcp_thrift_server",
        "--service-launcher", "/abs/path/common/bin/IarServiceLauncher",
        "--cspyserver2", "/abs/path/common/bin/CSpyServer2"
      ],
      "cwd": "/abs/path/to/cspy-mcp"
    }
  }
}
```

## Option B: connect to a backend that already hosts them (external mode)

Anything that hosts a `com.iar.thrift.service.manager` works — a hand-started
`IarServiceLauncher`, or a Thrift-enabled `iaride`, which registers
ProjectManager and OptionsService itself.

Start the launcher yourself, with or without manifests on the command line:

```sh
cd /path/to/install/common/bin
./IarServiceLauncher -standalone -sockets projectmanager.json
```

```
     IAR Service Launcher V9.6.0.47
[1003892]
Service registry running on local socket on port: 41821
Also available in serialized form in CSpyServer2-ServiceRegistry.txt
[1003892] Starting service manager service...
[1003892] Service: com.iar.ProjectManager
[1003892] Loading native library .../common/bin/ProjectManagerHandler
[1003892] Service started: com.iar.ProjectManager
[1003892] Entering main loop...
```

Then point the bridge at that registry port:

```sh
python -m mcp_thrift_server --registry-port 41821
```

`-standalone` is required for the launcher to host a registry, and `-sockets`
is required because the Python Thrift runtime does not support named pipes.

Any manifest not given on the command line is still loaded on demand through
the service manager, so `-standalone -sockets` with no manifests at all is
enough — the bridge will add what it needs.

To attach a debugger to the *same* registry, start CSpyServer2 with
`-sockets -registry <port>` rather than `-standalone`.

## Verifying and troubleshooting

`ide_services_status()` is the first thing to call when a `project_*` or
`options_*` tool cannot reach its service. It reports the active mode, the
registry endpoint, the launcher process state, everything currently registered,
and which manifest would be used for each missing service:

```json
{
  "cspy_mode": "launcher",
  "service_bin_dir": "/abs/path/common/bin",
  "registry_port": 37523,
  "has_service_manager": true,
  "services": {
    "projectmanager": { "registry_name": "com.iar.thrift.service.projectmanager",
                        "registered": true,  "manifest": "projectmanager.json",
                        "depends_on": [] },
    "options":        { "registry_name": "com.iar.optionsservice",
                        "registered": false, "manifest": "OptionsService.json",
                        "depends_on": ["projectmanager"] }
  },
  "service_launcher": { "running": true, "pid": 1006182, "log_path": "/tmp/iar-service-launcher-….log" }
}
```

| Symptom | Cause and fix |
| --- | --- |
| `... has no com.iar.thrift.service.manager service to start it with` | Only a bare `CSpyServer2` is running. Switch to launcher mode, or point at a launcher/iaride registry. The error lists the services the registry *did* have. |
| `Cannot reach IDE service ...: no service registry configured` | No registry at all: set launcher mode plus `THRIFT_SERVICE_LAUNCHER_EXE`, or `THRIFT_REGISTRY_HOST`/`THRIFT_REGISTRY_PORT`. |
| `Cannot host IDE services: set THRIFT_SERVICE_LAUNCHER_EXE ...` | Launcher mode selected but no launcher path given. |
| `neither the manifest ... nor the service library ... exists` | Wrong `common/bin`. Check `THRIFT_SERVICE_BIN_DIR` / `THRIFT_SERVICE_LAUNCHER_EXE`. |
| `CSpyServer2 was asked to join registry port N but reported port M` | CSpyServer2 did not attach to the launcher's registry; check its log path in the error. |
| `Timed out waiting for the IarServiceLauncher registry port` | Raise `THRIFT_SERVICE_LAUNCHER_START_TIMEOUT_MS`; the error includes the launcher log path. |
| `OptionsService CreateSession failed: Project not found: <path>` | The project is not loaded in the project manager. Call `project_load_workspace(<path>)` first. |
| The host process dies on the first `options_*` call | OptionsService was started without ProjectManager in the same process. Let the bridge resolve the dependency (do not bypass it by loading only `OptionsService.json` by hand). |

`ide_services_ensure(force=true)` re-checks the registry after a backend
restart, and `ide_services_stop_launcher()` tears down a launcher this bridge
started. Note that in launcher mode the launcher owns the registry, so stopping
it also takes the managed CSpyServer2's registry away — expect to restart both.

## Reference: services, manifests, environment

A service's registry name is the `SERVICE_ID`/`*_ID` constant in its IDL, which
is *not* the `name` field in its manifest — that one is only a
ServiceManager-internal id:

| Key | Registry name (IDL constant) | Manifest `name` | Library | Entry points | Depends on |
| --- | --- | --- | --- | --- | --- |
| `projectmanager` | `com.iar.thrift.service.projectmanager` | `com.iar.ProjectManager` | `ProjectManagerHandler` | `StartProjectManager` / `StopProjectManager` | — |
| `options` | `com.iar.optionsservice` | `com.iar.OptionsService` | `OptionsService` | `OptionsServiceStart` / `OptionsServiceStop` | `projectmanager` |

`OptionsServiceHandler` reaches the project manager by linkage
(`GetProjectManager()`), not over Thrift, so **ProjectManager must be hosted in
the same process**. Starting `options` alone and then calling `CreateSession`
takes the whole host process down rather than returning a failure. The bridge
pulls the dependency in automatically, so `THRIFT_IDE_SERVICES=options` and
`ide_services_ensure("options")` both start ProjectManager first.

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `THRIFT_CSPYSERVER_MODE` | `managed` | `managed`, `external` or `launcher`. |
| `THRIFT_SERVICE_LAUNCHER_EXE` | — | Path to `IarServiceLauncher`. Required in launcher mode. |
| `THRIFT_SERVICE_LAUNCHER_START_TIMEOUT_MS` | `40000` | Wait for the launcher's registry banner. Higher than CSpyServer2's because the launcher dlopens the service libraries. |
| `THRIFT_SERVICE_LAUNCHER_RESTART_ON_FAILURE` | `1` | Retry launcher startup once. |
| `THRIFT_SERVICE_BIN_DIR` | dir of the launcher, else of CSpyServer2 | Where the service libraries and stock manifests live. |
| `THRIFT_IDE_SERVICES` | both | Comma-separated subset: `projectmanager`, `options`. |
| `THRIFT_AUTO_IDE_SERVICES` | `1` | Auto-start a missing service on the first `project_*`/`options_*` call. |
| `THRIFT_PROJECTMANAGER_SERVICE_NAME` | `com.iar.thrift.service.projectmanager` | Registry name override. |
| `THRIFT_OPTIONSSERVICE_SERVICE_NAME` | `com.iar.optionsservice` | Registry name override. |

## Using the OptionsService tools

`OptionsService` is the *presentation* view of a configuration's options: it
serves the same category/option tree the IDE's options dialog renders, as XML,
and validates edits before they are committed. It is session based:

```
options_create_session(project_path, config_name)        → session_id
options_get_category_tree(session_id)                    → <pages> XML, page ids
options_get_option_tree(session_id, "General-GEN-TARGET")→ option widgets + values
options_update_state(session_id, tree_id, updated_json)  → validated tree, verification errors
options_commit(session_id)                               → into the configuration
options_destroy_session(session_id)
```

`options_commit` marks the project modified in the project manager; persist it
to disk with `projectmanager_call("SaveEwpFile", ...)`.

The project must already be loaded in the project manager — OptionsService
resolves `projectPath` through it, so passing a path is not by itself enough.
Call `project_load_workspace("/abs/path/p.ewp")` first; otherwise
`options_create_session` fails with `Project not found: <path>` plus that hint.

A worked round trip on a Cortex-M3 project, reading a real option and changing
it:

```
project_load_workspace("/abs/path/test.ewp")
options_create_session("/abs/path/test.ewp", "Debug")      → session_id "0"
options_get_category_tree("0")                             → 71 pages
options_get_option_tree("0", "General-GEN-TARGET")         → <property id="General.@OGCoreOrChip" …><value>Core</value>
options_update_state("0", "General-GEN-TARGET",
    '[{"optionDefinitionId": "General.@OGCoreOrChip", "data": "Chip"}]')
                                                           → same property now <value>Chip</value>
options_destroy_session("0")                               → change discarded (no options_commit)
```

Option ids come from the `id` attributes of the option tree XML
(`General.@OGCoreOrChip`, `ICCARM.…`); an id the backend does not recognise is
reported as `unrecognized id. Operation discarded.` rather than silently
dropped.

Empty `project_path`/`config_name` mean "the current project" / "its current
configuration", same as the `project_*` tools. `options_call` is the generic
fallback for the raw RPCs.

**For plain option reading and writing, prefer ProjectManager.** It exposes a
flat list of option ids and values with no session or XML involved:

```
projectmanager_call("GetOptionsForConfiguration",   "[{\"filename\": \"/abs/p.ewp\"}, \"Debug\", []]")
projectmanager_call("ApplyOptionsForConfiguration", "...")
```

Reach for `options_*` when you want what the GUI would show — grouping, page
structure, presentation metadata — or the backend's validation of a proposed
value.

Two behaviours worth knowing, both inherited from the backend:

- Every OptionsService response carries a `shared.Success { value,
  failureMessage }` instead of throwing. The wrapped `options_*` tools check it
  and raise; `options_call` does not, so check it yourself.
- *Reading* options can mutate the configuration — some target options persist
  derived values as a side effect of being read. Treat a session as a
  transaction: do the reads and writes you need, then commit or destroy it.

## Known quirks

**`OptionsService.json` is missing from the stage.** The library
`libOptionsService.so` is installed, but `OptionsService/CMakeLists.txt` has an
`install(FILES DESTINATION $<CONFIG>/common/bin)` whose file list is empty, so
the manifest never lands next to it. The bridge works around this by generating
an equivalent manifest in a temp directory; `ide_services_status()` shows the
path under `loaded_manifests`. If a future stage ships the file, the stock one
is preferred automatically.

**Manifest `libraryName` is always relative to the manifest file.**
`CSpyServiceManagerHandler::GetServiceConfigsFromJsonFile` prepends the
manifest's own directory to `libraryName` unconditionally — an absolute
`libraryName` gets that directory prepended too and becomes a nonexistent path
such as `/tmp/manifests/home/user/install/common/bin/libOptionsService.so`.
Generated manifests therefore use a relative path. This is also why passing a
stock manifest by absolute path works fine: the `libraryName` inside it is
relative.

**Stopping a debug session can take the backend down.** Unrelated to this
wiring, but visible here too: `stopSession` in `-standalone -sockets` mode hits
a known backend assertion. Managed/launcher mode restarts the process; the IDE
services are hosted by the launcher rather than CSpyServer2, so a project or
options session survives a debugger restart.
