# Hosting the IDE services: ProjectManager and OptionsService

The `project_*` and `options_*` tools talk to two IDE platform Thrift services
that **`CSpyServer2` cannot host**. This document explains why, how to start
them, and how to verify the result.

- [Why CSpyServer2 is not enough](#why-cspyserver2-is-not-enough)
- [Option A: let the bridge host them (managed mode)](#option-a-let-the-bridge-host-them-managed-mode)
- [Option B: connect to a backend that already hosts them (standalone mode)](#option-b-connect-to-a-backend-that-already-hosts-them-standalone-mode)
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

## Option A: let the bridge host them (managed mode)

Managed mode - the default - makes the MCP server own the whole backend:

```
MCP server
 ├── spawns IarServiceLauncher -standalone -sockets   → owns the registry
 │      └── loads projectmanager.json / OptionsService.json on demand
 └── spawns CSpyServer2 -sockets -registry <port>     → joins that registry
```

Give it one path: the IAR installation or build stage, meaning the directory
with `common/bin` under it. Both programs are taken from there.

```sh
python -m mcp_thrift_server --iar-path /path/to/install
```

or via the environment:

```sh
export IAR_INSTALL_PATH=/path/to/install
python -m mcp_thrift_server
```

The launcher comes up first and the registry it publishes is what everything
resolves through, so ordering is handled for you. The individual IDE services
are started lazily: the first `project_*` call loads the ProjectManager, the
first `options_*` call loads the OptionsService. Call `ide_services_ensure()` to
warm them up front, or set `THRIFT_AUTO_IDE_SERVICES=0` to require it
explicitly.

Programs that installation does not ship are simply not started. A compiler-only
toolchain has no `IarServiceLauncher`, and managed mode then runs `CSpyServer2`
on its own with the `project_*`/`options_*` tools unavailable. Pass
`--no-ide-services` to choose that deliberately.

Both processes are supervised the same way: stdout goes to a log file, a recent
tail is attached to failure envelopes (`service_launcher_diagnostics`), and both
are stopped on exit - CSpyServer2 first, then the launcher.

`--cspyserver2` and `--service-launcher` still take individual program paths and
override what the stage provides, but you should not normally need them.

### As an MCP client entry

`run_headless.sh` wraps this for stdio, which is what an MCP host wants:

```json
{
  "mcpServers": {
    "cspy-debugger": {
      "command": "/abs/path/to/cspy-mcp/run_headless.sh",
      "args": ["/path/to/install"]
    }
  }
}
```

Or without the wrapper:

```json
{
  "mcpServers": {
    "cspy-debugger": {
      "command": "python",
      "args": ["-m", "mcp_thrift_server", "--iar-path", "/path/to/install"],
      "cwd": "/abs/path/to/cspy-mcp"
    }
  }
}
```

## Option B: connect to a backend that already hosts them (standalone mode)

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

Then point the bridge at that registry port, which selects standalone mode:

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
  "cspy_mode": "managed",
  "iar_path": "/path/to/install",
  "service_bin_dir": "/path/to/install/common/bin",
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
| `... has no com.iar.thrift.service.manager service to start it with` | Only a bare `CSpyServer2` is running. Pass `--iar-path` so the bridge hosts the services, or point at a launcher/iaride registry. The error lists the services the registry *did* have. |
| `Cannot reach IDE service ...: no service registry configured` | No registry at all: pass `--iar-path`, or set `THRIFT_REGISTRY_HOST`/`THRIFT_REGISTRY_PORT`. |
| `Cannot host IDE services: no IarServiceLauncher path` | No `--iar-path`/`IAR_INSTALL_PATH`, or that installation does not ship the launcher. |
| `neither the manifest ... nor the service library ... exists` | Wrong path. Check `--iar-path`/`IAR_INSTALL_PATH`. |
| `CSpyServer2 was asked to join registry port N but reported port M` | CSpyServer2 did not attach to the launcher's registry; check its log path in the error. |
| `Timed out waiting for the IarServiceLauncher registry port` | Raise `THRIFT_SERVICE_LAUNCHER_START_TIMEOUT_MS`; the error includes the launcher log path. |
| `OptionsService CreateSession failed: Project not found: <path>` | The project is not loaded in the project manager. Call `project_load_workspace(<path>)` first. |
| The host process dies on the first `options_*` call | OptionsService was started without ProjectManager in the same process. Let the bridge resolve the dependency (do not bypass it by loading only `OptionsService.json` by hand). |

`ide_services_ensure(force=true)` re-checks the registry after a backend
restart, and `ide_services_stop_launcher()` tears down a launcher this bridge
started. Note that the launcher owns the registry, so stopping it also takes the
managed CSpyServer2's registry away — expect to restart both.

## Reference: services, manifests, environment

A service's registry name is the `SERVICE_ID`/`*_ID` constant in its IDL, which
is *not* the `name` field in its manifest — that one is only a
ServiceManager-internal id:

| Key | Registry name (IDL constant) | Manifest `name` | Library | Entry points | Depends on |
| --- | --- | --- | --- | --- | --- |
| `projectmanager` | `com.iar.thrift.service.projectmanager` | `com.iar.ProjectManager` | `ProjectManagerHandler` | `StartProjectManager` / `StopProjectManager` | — |
| `options` | `com.iar.optionsservice` | `com.iar.OptionsService` | `OptionsService` | `OptionsServiceStart` / `OptionsServiceStop` | `projectmanager` |

OptionsService reaches the project manager directly rather than over Thrift, so
**ProjectManager must be hosted in the same process**. Starting `options` alone
and then calling `CreateSession` takes the whole host process down rather than
returning a failure. The bridge pulls the dependency in automatically, so
`THRIFT_IDE_SERVICES=options` and `ide_services_ensure("options")` both start
ProjectManager first.

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `IAR_INSTALL_PATH` | — | The IAR installation or build stage, i.e. the directory with `common/bin` under it. Normally the only path needed. |
| `THRIFT_CSPYSERVER_MODE` | `managed` | `managed` (the bridge starts the backend) or `standalone` (connect to a running one). `external` and `launcher` are accepted as the names these used before. |
| `THRIFT_HOST_IDE_SERVICES` | `1` | `0` runs the debugger alone, with no IarServiceLauncher. Same as `--no-ide-services`. |
| `THRIFT_IDE_SERVICES` | both | Comma-separated subset: `projectmanager`, `options`. |
| `THRIFT_AUTO_IDE_SERVICES` | `1` | Auto-start a missing service on the first `project_*`/`options_*` call. |
| `THRIFT_SERVICE_LAUNCHER_EXE` | from `IAR_INSTALL_PATH` | Path to `IarServiceLauncher`, overriding `IAR_INSTALL_PATH`. |
| `THRIFT_CSPYSERVER_EXE` | from `IAR_INSTALL_PATH` | Path to `CSpyServer2`, overriding `IAR_INSTALL_PATH`. |
| `THRIFT_SERVICE_BIN_DIR` | `<IAR_INSTALL_PATH>/common/bin` | Where the service libraries and shipped manifests live. |
| `THRIFT_SERVICE_LAUNCHER_START_TIMEOUT_MS` | `40000` | Wait for the launcher's registry banner. Higher than CSpyServer2's because the launcher loads the service libraries. |
| `THRIFT_SERVICE_LAUNCHER_RESTART_ON_FAILURE` | `1` | Retry launcher startup once. |
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

**`OptionsService.json` may be missing from the installation.** The
OptionsService library is installed but its service manifest is not always
shipped alongside it. The bridge handles this by generating an equivalent
manifest in a temp directory; `ide_services_status()` shows the path under
`loaded_manifests`. When the installation does ship the file, that one is used
instead, so nothing needs changing if a later release adds it.

**Manifest `libraryName` is always relative to the manifest file.** The
service manager resolves `libraryName` against the manifest's own directory,
unconditionally, so an absolute `libraryName` ends up with that directory
prepended and points at a path that does not exist. Generated manifests
therefore use a relative path. This is also why passing a shipped manifest by
absolute path works fine: the `libraryName` inside it is relative.

**Stopping a debug session can take the backend down.** Unrelated to this
wiring, but visible here too: `stopSession` can hit a backend assertion and end
the process. Managed mode restarts it, and because the IDE services live in the
IarServiceLauncher rather than in CSpyServer2, a project or options session
survives a debugger restart.

**Transport is loopback TCP, and that is not configurable here.** The IDE
platform picks named pipes by default on Windows and TCP sockets on Linux; the
bridge passes `-sockets` so both end up on TCP, because the Python Thrift
runtime cannot speak Windows named pipes. Those sockets bind to `127.0.0.1` on
an ephemeral port, so the Thrift traffic never leaves the machine.
