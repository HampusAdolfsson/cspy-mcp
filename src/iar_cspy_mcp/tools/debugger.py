"""Debugger tools: connection info, session lifecycle, run control, expressions."""

from __future__ import annotations

from typing import Any

from iar_cspy import StopResult, to_plain
from iar_cspy.debugger import PRE_SESSION_METHODS
from iar_cspy.errors import DC_RESULTS, ERROR_CODES, FOCUSED_DC_RESULTS, error_entry
from iar_cspy.ide_services import IDE_SERVICES

from .._app import envelope, get_client, mcp, require_session
from ._args import call_with_json_args, context_from_json


@mcp.tool()
def thrift_connection_info() -> dict[str, Any]:
    """Return active server connection settings.

    Returns:
        Dictionary with host/port defaults, timeout, thrift file path, and include dirs.

    Notes:
        This reports local server configuration only and does not verify that backend
        services are reachable.
    """
    client = get_client()
    cfg = client.config
    endpoint = client.backend.current_registry_endpoint()
    info = {
        "host": cfg.host,
        "port": cfg.port,
        "timeout_ms": cfg.timeout_ms,
        "thrift_file": str(cfg.thrift_file),
        "include_dirs": list(cfg.include_dirs),
        "registry_host": endpoint[0] if endpoint else cfg.registry_host,
        "registry_port": endpoint[1] if endpoint else cfg.registry_port,
        "registry_service_name": cfg.registry_service_name,
        "cspy_mode": cfg.mode,
        "cspy_executable": str(cfg.cspy_executable) if cfg.cspy_executable else None,
        "cspy_args": list(cfg.cspy_args),
        "cspy_start_timeout_ms": cfg.cspy_start_timeout_ms,
        "cspy_restart_on_failure": cfg.cspy_restart_on_failure,
        "iar_path": str(cfg.iar_path) if cfg.iar_path else None,
        "service_launcher_exe": str(cfg.launcher_executable) if cfg.launcher_executable else None,
        "service_bin_dir": str(cfg.service_bin_dir) if cfg.service_bin_dir else None,
        "ide_services": list(cfg.ide_services) or sorted(IDE_SERVICES),
        "auto_ide_services": cfg.auto_ide_services,
    }
    info.update(client.backend.process_status())
    return info


@mcp.tool()
def debugger_list_methods() -> list[str]:
    """List RPC method names on the Debugger service.

    Returns:
        Sorted list of method names from the loaded thrift service metadata.

    Preconditions:
        THRIFT_FILE and THRIFT_INCLUDE_DIRS must resolve a valid cspy.thrift model.

    Failure cases:
        Raises an error if the Debugger service cannot be loaded.
    """
    return get_client().debugger.methods()


@mcp.tool()
def debugger_get_version() -> str:
    """Get debugger version string via Debugger.getVersionString().

    Returns:
        Backend version string, for example 9.5.4.0.
    """
    return get_client().debugger.version()


@mcp.tool()
def debugger_is_online() -> bool:
    """Check whether target is online via Debugger.isOnline().

    Returns:
        True when target is online, otherwise False.
    """
    return get_client().debugger.is_online()


@mcp.tool()
def debugger_get_number_of_cores() -> int:
    """Get number of available cores via Debugger.getNumberOfCores()."""
    require_session("debugger_get_number_of_cores")
    return get_client().debugger.core_count()


@mcp.tool()
def debugger_get_core_state(core: int = 0) -> int:
    """Get state of one core via Debugger.getCoreState(core).

    States: 0 = stopped (halted), 1 = running, 2 = sleeping, 3 = unknown, 4 = no power.
    """
    require_session("debugger_get_core_state")
    return int(get_client().debugger.core_state(int(core)))


@mcp.tool()
def debugger_session_status() -> dict[str, Any]:
    """Return local lifecycle flags and lightweight backend state hints."""
    return envelope(ok=True, tool="debugger_session_status", data=get_client().debugger.status())


@mcp.tool()
def debugger_configure_session(launch_json: str) -> dict[str, Any]:
    """Resolve and configure a debug session from one configuration JSON object.

    Equivalent to:
    1) resolveLaunchConfiguration(launch_json)
    2) configureSession(config)

    Important:
        This tool does not start execution/session context. Call
        debugger_start_smp_session() after this tool succeeds.

    Stability note:
        This wrapper intentionally does not call stopSession() first. Some
        backend builds can assert/crash when stopSession() is invoked during
        certain lifecycle states; use the explicit debugger_stop_session() tool
        only when you intentionally want to terminate the active session.

    Multicore:
        With "--multicore_nr_of_cores=N" (N > 1) in driverOptions, CSpyServer2
        needs the core count on its command line too. A managed backend is
        restarted with it before configuring; against a standalone backend,
        the error says what is missing.

    Args:
        launch_json: JSON object string for a single launch configuration: one
            entry of a C-SPY VS Code launch.json. A whole launch.json with
            {"configurations": [...]} is also accepted; its first entry is used.
            Minimal simulator example:
            {"type": "cspy", "request": "launch", "name": "Sim", "target": "arm",
             "program": "/abs/path/app.out", "driver": "Simulator",
             "driverOptions": ["--cpu=Cortex-M3", "--semihosting"],
             "stopOnSymbol": "main"}

    Returns:
        {"ok": True} on success.

    Preconditions:
        Requires service registry access when THRIFT_AUTO_EVENTHANDLER is enabled
        (default), so the server can auto-register debugger.eventhandler if needed.

    Side effects:
        Configures backend session state and may load target/plugin context.
    """
    get_client().debugger.configure(launch_json)
    return envelope(ok=True, tool="debugger_configure_session", data={"ok": True})


@mcp.tool()
def debugger_start_smp_session() -> dict[str, Any]:
    """Start session execution context via Debugger.startSMPSession().

    Returns:
        {"ok": True} on success.

    Side effects:
        Starts an active debug session in the backend and auto-ensures
        debugger.eventhandler when enabled.

    Expected call order:
        1) resolveLaunchConfiguration -> configureSession
           (or the wrapper debugger_configure_session(launch_json))
        2) debugger_start_smp_session()
    """
    get_client().debugger.start()
    return envelope(ok=True, tool="debugger_start_smp_session", data={"ok": True})


@mcp.tool()
def debugger_configure_and_start_session(launch_json: str) -> dict[str, Any]:
    """Run the full happy-path lifecycle: configure then start session.

    Equivalent to:
    1) debugger_configure_session(launch_json)
    2) debugger_start_smp_session()
    3) run to the configuration's stopOnSymbol, if set (reported in
       stopOnSymbol / ranToSymbol / stopOnSymbolError)

    Managed-mode behavior:
    - Always performs a strict cleanup first (including managed backend shutdown)
      to force a fresh backend process before resolve/configure/start.
    - No backend session/runtime state is expected to carry over between calls.

    Args:
        launch_json: One launch configuration, as for debugger_configure_session.
    """
    result = get_client().debugger.configure_and_start(launch_json)
    out: dict[str, Any] = {
        "ok": True,
        "configured": True,
        "started": True,
        "handoff": result.handoff,
        "stopOnSymbol": result.stop_on_symbol,
        "ranToSymbol": result.ran_to_symbol,
    }
    if result.stop_on_symbol_error is not None:
        out["stopOnSymbolError"] = result.stop_on_symbol_error
    return envelope(ok=True, tool="debugger_configure_and_start_session", data=out)


@mcp.tool()
def debugger_capabilities() -> dict[str, Any]:
    """Return a compact capability snapshot for the current backend/session."""
    return envelope(ok=True, tool="debugger_capabilities", data=get_client().capabilities())


@mcp.tool()
def debugger_stop_session() -> dict[str, Any]:
    """Stop the active debug session.

    Returns:
        {"ok": True} on success.

    Managed-mode behavior:
    - Asks the managed CSpyServer2 process to exit via Debugger.exit(), which
      ends the session (stopSession() is deprecated for clients and is not
      called), so the next startup uses a fresh backend process.
    - No backend session/runtime state is expected to carry over after this call.

    Standalone-mode behavior:
    - Stops the session via Debugger.stopSession(); a DkStop "failed to
      suspend" error on an already halted target counts as success
      (stop_idempotent_recovered).
    - A target stdin read that is waiting for input is answered with
      end-of-file first.
    """
    result = get_client().debugger.stop_session()
    out: dict[str, Any] = {"ok": True}
    if result.already_stopped:
        out["already_stopped"] = True
    out["managed_backend_restarted_on_next_start"] = result.backend_restarted
    if result.stop_idempotent_recovered:
        out["stop_idempotent_recovered"] = True
        out["stop_idempotent_details"] = result.stop_idempotent_details
    return envelope(ok=True, tool="debugger_stop_session", data=out)


@mcp.tool()
def debugger_strict_cleanup(reset_target: bool = False) -> dict[str, Any]:
    """Force cleanup to restore a known-good baseline.

    Best-effort steps:
    1) optional target reset
    2) stop session
    3) clear local lifecycle/cache buffers
    4) stop managed CSpyServer2 process (if active)

    Returns structured cleanup results and does not raise on partial failures.
    """
    result = get_client().debugger.cleanup(reset_target=bool(reset_target))
    data = {
        "ok": result.ok,
        "errors": result.errors,
        "before": result.before,
        "after": result.after,
        "managed_server_shutdown": True,
    }
    error = None
    if not result.ok:
        error = error_entry(
            code="PARTIAL_CLEANUP_FAILED",
            category="cleanup",
            message="Cleanup finished with one or more step failures.",
            retryable=True,
        )
    return envelope(ok=result.ok, tool="debugger_strict_cleanup", data=data, error=error)


@mcp.tool()
def debugger_load_module(filename: str) -> dict[str, Any]:
    """Load a module/executable into debugger session.

    Args:
        filename: Path to executable/module file.

    Returns:
        {"ok": True, "filename": <input>} on success.
    """
    require_session("debugger_load_module")
    get_client().debugger.load_module(filename)
    return {"ok": True, "filename": filename}


@mcp.tool()
def debugger_get_modules() -> Any:
    """List currently loaded modules.

    Returns:
        JSON-serializable list of module records.
    """
    require_session("debugger_get_modules")
    return to_plain(get_client().debugger.modules())


@mcp.tool()
def debugger_go() -> dict[str, Any]:
    """Run target execution via Debugger.go().

    Returns:
        {"ok": True} on success.

    Side effects:
        Changes target execution state to running.
    """
    require_session("debugger_go")
    get_client().debugger.go()
    return {"ok": True}


@mcp.tool()
def debugger_stop() -> dict[str, Any]:
    """Stop target execution via Debugger.stop().

    Returns:
        {"ok": True} on success.

    A target stdin read that is waiting for input stays pending: input pushed
    later (libsupport_push_input) still reaches it once the target runs again.
    """
    require_session("debugger_stop")
    get_client().debugger.halt()
    return {"ok": True}


@mcp.tool()
def debugger_reset() -> dict[str, Any]:
    """Reset target via Debugger.reset().

    Returns:
        {"ok": True} on success.

    Side effects:
        Resets target hardware/session state per active reset style.

    Usage note:
        Call this when execution state is inconsistent before retrying
        start/go sequences.
    """
    require_session("debugger_reset")
    get_client().debugger.reset()
    return {"ok": True}


@mcp.tool()
def debugger_step_over() -> dict[str, Any]:
    """Step over one source statement via Debugger.stepOver().

    Returns:
        {"ok": True} on success.

    Side effects:
        Advances execution by one statement when halted.

    Preconditions:
        Session is configured/started and target is halted.
    """
    require_session("debugger_step_over")
    get_client().debugger.step_over(wait=False)
    return {"ok": True}


@mcp.tool()
def debugger_get_thread_list() -> Any:
    """Return debugger thread list via Debugger.getThreadList()."""
    require_session("debugger_get_thread_list")
    return to_plain(get_client().debugger.threads())


@mcp.tool()
def debugger_get_cycle_counter(core: int = 0) -> int:
    """Return cycle counter for one core via Debugger.getCycleCounter(core)."""
    require_session("debugger_get_cycle_counter")
    return get_client().debugger.cycle_counter(int(core))


@mcp.tool()
def debugger_eval_expression(
    expression: str,
    context_json: str = "",
    format: int = 0,
    dereference: bool = False,
) -> Any:
    """Evaluate an expression in context via Debugger.evalExpression().

    format is an ExprFormat: 0 = default, 1 = bin, 2 = oct, 3 = dec, 4 = hex,
    5 = char, 6 = str, 7 = no custom. dereference is passed as evalExpression's
    "prefix" flag: it adds the format's prefix (such as 0x) to the value text.
    """
    require_session("debugger_eval_expression")
    value = get_client().debugger.eval(
        expression, context=context_from_json(context_json), format=int(format), prefix=bool(dereference)
    )
    return to_plain(value)


def _wait(tool: str, go: bool, desired_state: int, core: int, timeout_ms: int, poll_interval_ms: int) -> dict[str, Any]:
    debugger = get_client().debugger
    wait = debugger.go_and_wait if go else debugger.wait_for_state
    result = wait(
        int(desired_state),
        core=int(core),
        timeout=max(0.0, float(timeout_ms) / 1000.0),
        poll_interval=max(0.0, float(poll_interval_ms) / 1000.0),
        check=False,
    )
    data = result.to_dict()
    if go:
        data["started_running"] = True
    error = None
    if result.timed_out:
        error = error_entry(
            code="TIMEOUT",
            category="timeout",
            message="Desired core state was not reached before timeout.",
            retryable=True,
        )
    return envelope(ok=result.ok, tool=tool, data=data, error=error)


@mcp.tool()
def debugger_wait_for_core_state(
    desired_state: int = 0,
    core: int = 0,
    timeout_ms: int = 5000,
    poll_interval_ms: int = 50,
) -> dict[str, Any]:
    """Wait until one core reaches the desired state or timeout expires.

    States: 0 = stopped (halted), 1 = running, 2 = sleeping, 3 = unknown, 4 = no power.
    Returns a structured result and does not raise on timeout.
    """
    require_session("debugger_wait_for_core_state")
    return _wait("debugger_wait_for_core_state", False, desired_state, core, timeout_ms, poll_interval_ms)


@mcp.tool()
def debugger_go_and_wait_for_core_state(
    desired_state: int = 0,
    core: int = 0,
    timeout_ms: int = 5000,
    poll_interval_ms: int = 50,
) -> dict[str, Any]:
    """Start execution and wait for a desired core state (for example halted=0).

    When waiting for a halt, the wait also covers the backend moving its
    inspection context to the stop location (at most 0.5 s more), so values
    read afterwards are current. A fatal backend error ends the wait at once
    and fails the tool with a SessionError: start a new session.
    """
    require_session("debugger_go_and_wait_for_core_state")
    return _wait("debugger_go_and_wait_for_core_state", True, desired_state, core, timeout_ms, poll_interval_ms)


@mcp.tool()
def debugger_error_taxonomy() -> dict[str, Any]:
    """Return stable machine-readable error codes used by AI-first tool envelopes."""
    return envelope(
        ok=True,
        tool="debugger_error_taxonomy",
        data={
            "codes": ERROR_CODES,
            "dc_result_focus": [{"value": v, "name": DC_RESULTS[v]} for v in FOCUSED_DC_RESULTS],
            "dc_result_constants": [{"value": v, "name": n} for v, n in sorted(DC_RESULTS.items())],
        },
    )


@mcp.tool()
def debugger_register_snapshot(group: str = "CPU Registers (ABI)", limit: int = 64) -> dict[str, Any]:
    """Read register metadata and values for a register group.

    Args:
        group: Preferred register group name. Falls back to first available group if
            the requested group is missing.
        limit: Maximum number of registers to return. Values less than 1 are clamped
            to 1.

    Returns:
        Dictionary containing selected group, available groups, counts, and register
        entries with metadata and value fields:
        - raw_hex: value bytes in hex
        - value_unsigned_le: unsigned little-endian integer
        - read_error: present when individual value read fails

    Preconditions:
        Requires debugger.memory service to be available in the Service Registry.
        Requires an active/usable debug context for meaningful values.
    """
    require_session("debugger_register_snapshot")
    snapshot = get_client().debugger.registers(group, int(limit))
    if not snapshot.available_groups:
        return {"group": group, "available_groups": [], "registers": []}

    registers = []
    for reg in snapshot.registers:
        item: dict[str, Any] = {
            "name": reg.name,
            "alias": reg.alias,
            "description": reg.description,
            "bit_size": reg.bit_size,
            "location": reg.location,
            "readonly": reg.readonly,
            "writeonly": reg.writeonly,
        }
        if reg.raw is not None:
            item["raw_hex"] = reg.raw.hex()
            item["value_unsigned_le"] = reg.value
        else:
            item["read_error"] = reg.read_error
        registers.append(item)
    return {
        "group": snapshot.group,
        "requested_group": snapshot.requested_group,
        "available_groups": snapshot.available_groups,
        "total_in_group": snapshot.total_in_group,
        "returned": len(registers),
        "registers": registers,
    }


@mcp.tool()
def debugger_call(method: str, args_json: str = "[]") -> Any:
    """Call an arbitrary Debugger RPC method.

    Args:
        method: RPC method name on Debugger.
        args_json: Arguments encoded as JSON. Supported forms:
            - JSON list for positional arguments
            - JSON object for keyword arguments
            - Any other JSON value treated as one positional argument

    Returns:
        RPC result converted to JSON-serializable structure.

    Struct and enum arguments:
        JSON objects are coerced (recursively) into the thrift struct the method
        expects, matched by field name — e.g. for evalExpression pass
        `[{"type": "CurrentInspection", "level": 0, "core": 0, "task": 0}, "argc", [], 0, false]`.
        Enum-typed fields accept the enum name as a string (with or without the
        leading "k") or the raw integer value.
    """
    if method not in PRE_SESSION_METHODS:
        require_session(f"debugger_call({method})")

    result = call_with_json_args(get_client().debugger.call, method, args_json)
    if isinstance(result, StopResult):  # stopSession failed only because it was already stopped
        return {
            "ok": True,
            "already_stopped": True,
            "stop_idempotent_recovered": True,
            "stop_idempotent_details": result.stop_idempotent_details,
        }
    return to_plain(result)
