# IDE services: the project and options tools

The `project_*` and `options_*` tools talk to two IDE platform Thrift
services, ProjectManager and OptionsService, that **`CSpyServer2` cannot
host**. `iar-cspy` hosts them in an `IarServiceLauncher`; why, and how that
works, is in its guide:
[Hosting the IDE services](https://github.com/iarsystems/cspy-py/blob/main/docs/ide-services.md).

## Hosting them from the server

- **Managed mode** (the default): `iar-cspy-mcp --iar-path /path/to/install`
  (or `IAR_INSTALL_PATH`) starts an `IarServiceLauncher` owning the service
  registry and a `CSpyServer2` joining it. The services start on the first
  `project_*`/`options_*` call; `ide_services_ensure()` warms them up front.
  `--no-ide-services` runs the debugger alone, and `--ide-services
  projectmanager` hosts only the ProjectManager.
- **Standalone mode**: `iar-cspy-mcp --registry-port <port>` connects to a
  backend that hosts them already, such as a Thrift-enabled `iaride` or a
  hand-started `IarServiceLauncher -standalone -sockets`.

`run_headless.sh` wraps managed mode for stdio, which is what an MCP host
wants:

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

When a `project_*` or `options_*` tool cannot reach its service, call
`ide_services_status()` first: it reports the mode, the registry, the launcher
process, everything registered, and the manifest that would be used for each
missing service. The error messages and their fixes are listed under
[Verifying and troubleshooting](https://github.com/iarsystems/cspy-py/blob/main/docs/ide-services.md#verifying-and-troubleshooting).
`ide_services_ensure(force=true)` re-checks the registry after a backend
restart, and `ide_services_stop_launcher()` tears down a launcher this server
started (which also takes the managed CSpyServer2's registry away).

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
