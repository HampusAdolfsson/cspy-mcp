# CLAUDE.md

## Project Summary
This repository is `iar-cspy-mcp` (import `iar_cspy_mcp`), an MCP server that
exposes the IAR C-SPY debugger (and the Embedded Workbench
ProjectManager/OptionsService) to AI assistants as tools.

It is a thin adapter over the `iar-cspy` Python API (import `iar_cspy`), which
lives in its own repository: https://github.com/iarsystems/cspy-py, usually
checked out next to this one as `../cspy-py`. `iar_cspy` starts or connects to
the backend, makes the Thrift calls and holds all backend behavior; this
repository only turns it into MCP tools. The dependency goes one way only:
`iar_cspy` never imports `mcp` or `iar_cspy_mcp`.

## Key Files
`src/iar_cspy_mcp/`:
- `_app.py`: the FastMCP app, the server's one `Client`, the session guard, the envelope.
- `tools/*.py`: MCP tools, one module per area. Keep them thin: parse JSON
  args, call the API, shape the result.
- `server.py`: imports all tool modules. `cli.py`: arguments, transports, signal handling.

`tests/`: tool tests against `iar_cspy.testing.fake_client()` (`FakeRpc`),
and `test_live.py` against a real backend. The `cspy_client`/`cspy_session`
fixtures and `--cspy-*` options come from `iar_cspy.pytest_plugin`.

Also: `mcp_thrift_server/` is a deprecated shim so old `python -m mcp_thrift_server`
configs keep working. `examples/firmware/` is the simulator program + launch.json
used by the live tests. `docs/ide-services.md` covers the project and options
tools; the API guide, the hosting guide and the backend notes are in cspy-py.

## Dependency and Compatibility Notes
- `mcp` must stay below 2.0.0 because the server uses the `FastMCP` API.
- `iar-cspy` is a direct git dependency (`pyproject.toml`), not on PyPI. To
  change both at once, `pip install -e ../cspy-py` into the same environment,
  after installing this package: pip fetches the git dependency again on
  every install of this one, replacing the local checkout.
- MCP tool names, arguments and result shapes are a public contract for AI
  callers: change them deliberately, not as a side effect of API changes.

## Environment Contract
Everything has defaults; the IDL is bundled. The usual inputs:
- `IAR_INSTALL_PATH` / `--iar-path`: IAR installation (managed mode, default).
- `THRIFT_REGISTRY_HOST` / `THRIFT_REGISTRY_PORT` (+ `THRIFT_CSPYSERVER_MODE=standalone`):
  connect to a running backend.
- `MCP_TRANSPORT=stdio|streamable-http`, `MCP_HOST`, `MCP_PORT` for the server.
See `.env.example` for the rest.

## Runtime Architecture
1. An MCP tool call (or API call) reaches `Client.call`.
2. `Backend.registry_endpoint()` ensures the managed processes are up and returns the registry.
3. The service is resolved through the registry, and the RPC is made on a
   fresh connection.
4. Failures become `CSpyError`/`BackendError`, with backend diagnostics
   attached when they look like crashes. MCP tools return plain JSON, with
   `{"ok", "tool", "data", "error"}` envelopes for the AI-first tools.

Important behavior:
- The registry endpoint is not the debugger endpoint itself.
- The event handler and libsupport services are hosted by this process and
  registered on configure/start.
- One session per CSpyServer2 process: managed mode restarts it on stop.
- `go_and_wait`/`run_to` wait for the backend's target-stopped event.
  `runToULE` returns early, and the core reads "stopped" before it starts.

## Breakpoint API Guidance (Important)
`breakpoints_set_on_ule(ule, access_type)` / `client.breakpoints.add(ule, access)`
is the primary way to create breakpoints and watchpoints.
- `access_type`: `1` execute/fetch (code), `2` read, `3` write, `4` read/write
  (`iar_cspy.AccessType`).
- ULE forms: `main`, `func+4`, `0x100`, `{/abs/path/file.c}.123.1`
  (reliable source form), `<ule>@<size>`. `main()` and `file.c:123` are
  backend-dependent.
- `breakpoints_set_from_descriptor` / `breakpoints.restore()` only take
  descriptors returned by a listing. Descriptors are opaque, round-trip only.

## Known Backend Issues
See `docs/backend-notes.md` in cspy-py. In short:
- Connection resets (`WinError 10054`) and the backend exiting around
  configure/start/stopSession; stopSession can assert in `-standalone -sockets` mode.
- Headless: `ContextManager.getStack`/`getContextInfo` fail with "No such
  service: frontend" (no IDE frontend).
- Flash loading and license (`kDcLicenseViolation`) failures during start.
Tool failures may be backend lifecycle failures rather than API misuse.

## Useful Local Commands
```sh
pip install -r requirements-dev.txt                    # the server editable + pytest
pip install -e ../cspy-py                              # optional, afterwards: iar-cspy from a checkout
pytest                                                 # unit tests (live tests skip)
pytest -m live --cspy-iar-path=/opt/iar/ewarm          # live: managed, with IDE services
pytest -m live --cspy-cspyserver2=/path/CSpyServer2    # live: CSpyServer2 only
python -m compileall -q src tests examples mcp_thrift_server
```
Use the `--opt=value` form for the `--cspy-*` paths: a separate path argument
confuses pytest's rootdir detection.

## Current Priority and Next Work
- Keep the MCP tool surface stable and its errors actionable for AI callers.
- Keep behavior in `iar_cspy` and the MCP tools thin, so scripts and tools
  behave the same.
- Preserve robust registry/eventhandler behavior across backend restarts.
- Open items are in `TODO.md` (launch.json schema, breakpoint categories on
  emulators, zone discovery tool, stopSession crash reporting).

## Notes for Future Agents
- Do not assume backend stability; validate with short, focused call sequences.
- New backend behavior goes into `iar_cspy` (in cspy-py) with its tests
  there; this repository gets the tool change and a test pinning what MCP
  clients see.
- Treat descriptor inputs as opaque round-trip values only.
