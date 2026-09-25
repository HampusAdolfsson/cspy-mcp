# AI Usability Roadmap

This roadmap tracks major improvements to make the MCP server (and the `iar_cspy` API under it) easier and safer for AI agents to use.
The `iar_cspy` API lives in https://github.com/iarsystems/cspy-py; items that
need API or backend work are tracked in its TODO.md as well.

## Status Legend
- [ ] Not started
- [~] In progress
- [x] Done

## Major Bullet Points

1. [x] Add one happy-path orchestration tool
- Add a first-class configure-and-start tool that enforces resolve -> configure -> start.
- Return structured lifecycle state in one response.

2. [x] Add a universal status tool
- Return lifecycle flags, online state, core count/states, backend mode, and diagnostics hints.

3. [x] Standardize tool response shape
- Introduce a predictable envelope for success/error responses where practical.

4. [x] Make tools idempotent where possible
- Ensure repeated stop/ensure operations are safe and low-noise.

5. [x] Promote common debugger_call operations to first-class tools
- Prioritize thread list, cycle counter, eval expression, and similar high-frequency calls.

6. [x] Add explicit wait tools
- Add timeout-based wait operations for run-state transitions.

7. [x] Improve error taxonomy
- Add stable, machine-readable error categories/codes for recovery.

8. [x] Include backend diagnostics for transport failures consistently
- Ensure recent backend output is attached broadly on failures.

9. [x] Add capability discovery
- Expose backend/service capability summary and feature availability.

10. [x] Provide AI-first workflow docs
- Add compact canonical playbooks for common flows.

11. [x] Keep live harness self-contained and easy to run
- Preserve bundled assets and provide one-command validation guidance.

12. [x] Add strict cleanup/reset tool
- One tool to force teardown and return to a known-good baseline.

## Feedback from AI black-box usability test (2026-08-10, J-Link + sim on live E31 Arty)

The whole session was driven without reading the MCP source. What worked well:
the happy-path configure-and-start tool, the error envelopes (specific enough to
recover from every failure), managed-mode auto-restart after backend crashes,
and libsupport stdout capture working without any plugin configuration.

New items, roughly in priority order:

13. [~] Document the launch_json schema on the tool surface
- `debugger_configure_and_start_session` / `debugger_configure_session` accept a
  `launch_json` string but nothing describes its shape. The agent had to learn it
  from a stray example file plus the backend error "Missing JSON member: name".
- It is a *single* configuration object (one entry of a C-SPY VS Code
  launch.json), NOT the `{"configurations":[...]}` wrapper. Either accept both
  forms or state this clearly.
- Include a minimal working example (sim and one hardware driver) in the tool
  docstring or a schema-discovery tool.
- The IAR tooling already ships a formal launch.json schema (used by the
  VS Code debug extension). Reference or embed it.
- Done: the configure tools' descriptions now show the shape and a minimal
  simulator example, and a whole launch.json (`configurations` wrapper) is
  accepted, using its first entry. Still open: a hardware example and a
  reference to the extension's formal schema.

14. [ ] Breakpoint tools are effectively sim-only against emulator drivers
- Root cause is two known backend bugs in breakpoint category handling
  (default categories only cover STD_CODE*/STD_DATA*; explicit category ids
  outside the backend translation map are silently dropped, so even the
  correct `EMUL_CODE` fails). Backend fixes are tracked separately, but until
  they land:
- The breakpoint tool errors should mention the working alternatives:
  `debugger_call runToULE ["<ule>"]`, or eval `__setCodeBreak("main", 0, "1",
  "TRUE", "")`. Consider first-class `debugger_run_to_ule` and a macro-based
  breakpoint fallback tool.

15. [ ] Clarify the access_type enum
- Docstring says 1 = execute/fetch, but created breakpoints report
  `accessType: 0` and the sim accepts both 0 and 1. Verify against
  shared.AccessType and make the docstring, argument, and result consistent.

16. [ ] Fix cycle counter signedness
- `debugger_get_cycle_counter` returned -821365371 on hardware (i32 truncation
  of the 64-bit CYCLECOUNTER). Return unsigned 64-bit.

17. [ ] Surface the known stopSession backend crash better
- Every `stopSession` in `-standalone -sockets` mode hits a known backend
  assertion and aborts the process; managed mode recovers but the error
  payload embeds the same ~30-line stack trace twice (~30 KB).
  Deduplicate/truncate, and tag it as a known backend issue with
  "retry configure_and_start once" as the recovery hint.

18. [x] Implement stopOnSymbol in the MCP server (it is a frontend contract)
- With `stopOnSymbol: "main"` the session halts at `__iar_program_start`.
  Verified on both sim and J-Link: generic, not driver-specific.
- Root cause: CSpyServer2.startSession() never reads stopOnSymbol; it is the
  *frontend's* job to run to the symbol after start (both the IAR IDE and the
  VS Code DAP adapter do this). The MCP server is the frontend in this flow,
  so configure-and-start should call runToULE(stopOnSymbol) after start when
  the field is non-empty.
- Fixed: `debugger_configure_and_start_session` now calls
  `runToULE(stopOnSymbol, True)` after start when the field is present, and
  reports the outcome via `stopOnSymbol`/`ranToSymbol`/`stopOnSymbolError` in
  the response. Verified on a Cortex-M3 Simulator session.
- `runToULE` returns before the target has arrived, so `ranToSymbol` is now
  only reported once the target-stopped event has come in (see
  `Debugger.run_to` in iar_cspy).

19. [~] Add zone discovery
- `memory_read`/`memory_write_hex`/`disassemble_range` require a `zone_id` but
  there is no tool to list zones (agent guessed 0 = Memory, which worked).
  Promote `getAllZones` to a first-class tool or document common zone ids
  (incl. the CSR zone for RISC-V).
- Partly done: the Python API has `client.debugger.zones()`, and the
  `memory_read` description points at `debugger_call("getAllZones")`. No
  first-class MCP tool yet.

20. [x] Document core state values
- Wait tools say "for example halted=0" but there is no enum reference.
  A one-line table (0=halted, ...) in the docstrings would remove guesswork.
- Done: the core-state tools list 0 = stopped, 1 = running, 2 = sleeping,
  3 = unknown, 4 = no power; the API has `iar_cspy.CoreState`.

21. [ ] Host a headless `frontend` service
- Without an IDE, `getStack`, `getContextInfo`, source lookup and disassembly
  at the PC fail with "No such service, serviceName=frontend": the backend calls
  back into the IDE's UI service (`frontend.thrift`: message boxes, file
  dialogs). A stand-in hosted like the event handler would likely unblock them,
  but it has to answer the backend's prompts on the user's behalf, so decide
  on a policy first (e.g. default answer + log every prompt).
