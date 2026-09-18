from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


class DummyContextType:
    CurrentBase = 1
    CurrentInspection = 2
    Stack = 3
    Target = 4
    Task = 5
    Unknown = 6


class DummyContextRef:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class DummyZone:
    def __init__(self, id: int):
        self.id = id


class DummyLocation:
    def __init__(self, zone, address: int):
        self.zone = zone
        self.address = address


class DummyExprFormat:
    kDefault = 0


def dummy_shared_module():
    return SimpleNamespace(
        ContextType=DummyContextType,
        ContextRef=DummyContextRef,
        Zone=DummyZone,
        Location=DummyLocation,
        ExprFormat=DummyExprFormat,
    )


def test_helper_safe_repr_truncates(server_module):
    text = server_module._safe_repr("x" * 500, max_len=20)
    assert text.endswith("...")
    assert len(text) == 20


def test_should_attach_backend_diagnostics_filters_client_side_shape_errors(server_module):
    assert (
        server_module._should_attach_backend_diagnostics(
            "Field 'ref(1)' of 'evalExpression_args' needs type 'ContextRef'"
        )
        is False
    )


def test_should_attach_backend_diagnostics_allows_transport_errors(server_module):
    assert server_module._should_attach_backend_diagnostics("WinError 10054 connection reset") is True


def test_invoke_with_trace_does_not_append_backend_diag_for_client_side_errors(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "managed_server_crash_diagnostics", lambda: "backend diagnostics")

    def bad_call():
        raise TypeError("Field 'ref(1)' of 'evalExpression_args' needs type 'ContextRef'")

    with pytest.raises(TypeError) as exc:
        server_module._invoke_with_trace("debugger", "evalExpression", bad_call)

    assert "backend diagnostics" not in str(exc.value)


def test_invoke_with_trace_appends_backend_diag_for_transport_errors(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "managed_server_crash_diagnostics", lambda: "backend diagnostics")

    def bad_call():
        raise ConnectionResetError("WinError 10054")

    with pytest.raises(server_module.ThriftBridgeError) as exc:
        server_module._invoke_with_trace("debugger", "go", bad_call)

    assert "backend diagnostics" in str(exc.value)


def test_context_ref_default_and_json(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_shared_module", dummy_shared_module)

    default_ctx = server_module._context_ref_from_json("")
    assert default_ctx.type == DummyContextType.CurrentBase
    assert default_ctx.level == 0

    parsed = server_module._context_ref_from_json(
        json.dumps({"type": "Task", "level": 2, "core": 1, "task": 9})
    )
    assert parsed.type == DummyContextType.Task
    assert parsed.level == 2
    assert parsed.core == 1
    assert parsed.task == 9


def test_context_ref_invalid_json_type_raises(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_shared_module", dummy_shared_module)
    with pytest.raises(server_module.ThriftBridgeError):
        server_module._context_ref_from_json("[]")


def test_location_builder(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_shared_module", dummy_shared_module)
    loc = server_module._location(7, 0x1234)
    assert loc.zone.id == 7
    assert loc.address == 0x1234


def test_thrift_connection_info(server_module, monkeypatch):
    cfg = SimpleNamespace(
        host="127.0.0.1",
        port=0,
        timeout_ms=1234,
        thrift_file=Path("e:/mcp/thrift/cspy.thrift"),
        include_dirs=["e:/mcp/thrift"],
        registry_host="127.0.0.1",
        registry_port=49820,
        registry_service_name="debugger",
        cspy_mode="standalone",
        iar_path=None,
        cspy_executable=None,
        cspy_args="-standalone -sockets",
        cspy_start_timeout_ms=20000,
        cspy_restart_on_failure=True,
        launcher_executable=None,
        launcher_start_timeout_ms=40000,
        launcher_restart_on_failure=True,
        service_bin_dir=None,
        ide_services=[],
        auto_ide_services=True,
        host_ide_services=True,
    )
    monkeypatch.setattr(server_module, "load_config", lambda: cfg)
    info = server_module.thrift_connection_info()
    assert info["registry_port"] == 49820
    assert info["cspy_mode"] == "standalone"
    assert info["ide_services"] == ["options", "projectmanager"]


def test_debugger_wrappers(server_module, monkeypatch):
    calls = []

    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "getVersionString":
            return "9.5.4.0"
        if method == "isOnline":
            return True
        if method == "getNumberOfCores":
            return 2
        if method == "getCoreState":
            return int(args[0])
        if method == "getModules":
            return [{"name": "mod"}]
        if method == "getThreadList":
            return [{"id": 1}]
        if method == "getCycleCounter":
            return 123
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    assert server_module.debugger_get_version() == "9.5.4.0"
    assert server_module.debugger_is_online() is True
    assert server_module.debugger_get_number_of_cores() == 2
    assert server_module.debugger_get_core_state(1) == 1
    status = server_module.debugger_session_status()
    assert status["ok"] is True
    assert status["tool"] == "debugger_session_status"
    assert status["data"]["configured"] is True
    assert status["data"]["started"] is True
    assert status["data"]["backend_online"] is True
    assert status["data"]["core_count"] == 2
    assert status["data"]["core_states"] == [0, 1]
    assert server_module.debugger_get_thread_list() == [{"id": 1}]
    assert server_module.debugger_get_cycle_counter(0) == 123
    assert server_module.debugger_go() == {"ok": True}
    assert server_module.debugger_stop() == {"ok": True}
    assert server_module.debugger_reset() == {"ok": True}
    assert server_module.debugger_step_over() == {"ok": True}
    assert server_module.debugger_load_module("a.out") == {"ok": True, "filename": "a.out"}
    assert server_module.debugger_get_modules() == [{"name": "mod"}]

    methods = [name for name, _, _ in calls]
    assert "go" in methods
    assert "stop" in methods
    assert "reset" in methods
    assert "stepOver" in methods
    assert "loadModule" in methods


def test_debugger_session_status_when_not_started(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_call_debugger", lambda method, *args, **kwargs: True)
    server_module._set_session_state(configured=False, started=False)
    out = server_module.debugger_session_status()
    assert out["ok"] is True
    assert out["tool"] == "debugger_session_status"
    assert out["data"]["configured"] is False
    assert out["data"]["started"] is False
    assert out["data"]["backend_online"] is True
    assert out["data"]["core_count"] is None
    assert out["data"]["core_states"] is None


def test_debugger_session_status_includes_managed_metadata(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="managed",
            registry_host="127.0.0.1",
            registry_port=60000,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "managed_server_status", lambda: {"running": True, "pid": 123})

    def fake_call(method, *args, **kwargs):
        if method == "isOnline":
            return True
        if method == "getNumberOfCores":
            return 1
        if method == "getCoreState":
            return 0
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)
    server_module._set_session_state(configured=True, started=True)
    out = server_module.debugger_session_status()
    assert out["ok"] is True
    assert out["tool"] == "debugger_session_status"
    assert out["data"]["backend_mode"] == "managed"
    assert out["data"]["registry_host"] == "127.0.0.1"
    assert out["data"]["registry_port"] == 60000
    assert out["data"]["managed_server"]["running"] is True


def test_debugger_eval_expression_wrapper(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_context_ref_from_json", lambda _: "CTX")
    monkeypatch.setattr(server_module, "_shared_module", dummy_shared_module)

    seen = []

    def fake_call(method, *args, **kwargs):
        seen.append((method, args, kwargs))
        if method == "evalExpression":
            return {"value": "7"}
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    out = server_module.debugger_eval_expression("x + 1")
    assert out["value"] == "7"
    assert seen[0][0] == "evalExpression"

    with pytest.raises(server_module.ThriftBridgeError):
        server_module.debugger_eval_expression("  ")


def test_debugger_capabilities_standalone_not_started(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "debugger_list_methods", lambda: ["isOnline", "go"])
    monkeypatch.setattr(server_module, "_call_debugger", lambda method, *args, **kwargs: True)

    server_module._set_session_state(configured=False, started=False)
    out = server_module.debugger_capabilities()
    assert out["ok"] is True
    assert out["tool"] == "debugger_capabilities"
    assert out["data"]["backend_mode"] == "standalone"
    assert out["data"]["session"] == {"configured": False, "started": False}
    assert out["data"]["backend_online"] is True
    assert out["data"]["core_count"] is None
    assert out["data"]["debugger_methods"] == ["isOnline", "go"]
    assert out["data"]["services"] == []


def test_debugger_capabilities_managed_started(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="managed",
            registry_host="127.0.0.1",
            registry_port=60000,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "managed_server_status", lambda: {"running": True})
    monkeypatch.setattr(server_module, "debugger_list_methods", lambda: ["isOnline", "getNumberOfCores"])
    monkeypatch.setattr(server_module, "_list_registry_services", lambda q="": [{"name": "debugger"}])

    def fake_call(method, *args, **kwargs):
        if method == "isOnline":
            return True
        if method == "getNumberOfCores":
            return 4
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module._set_session_state(configured=True, started=True)
    out = server_module.debugger_capabilities()
    assert out["ok"] is True
    assert out["tool"] == "debugger_capabilities"
    assert out["data"]["backend_mode"] == "managed"
    assert out["data"]["managed_server"]["running"] is True
    assert out["data"]["core_count"] == 4
    assert out["data"]["services"][0]["name"] == "debugger"


def test_debugger_capabilities_collects_structured_errors(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "managed_server_crash_diagnostics", lambda: "managed diagnostics tail")

    def fake_call(method, *args, **kwargs):
        if method == "isOnline":
            raise server_module.ThriftBridgeError("Connection reset (WinError 10054)")
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)
    monkeypatch.setattr(
        server_module,
        "debugger_list_methods",
        lambda: (_ for _ in ()).throw(server_module.ThriftBridgeError("connection refused")),
    )

    server_module._set_session_state(configured=False, started=False)
    out = server_module.debugger_capabilities()
    assert out["ok"] is True
    assert len(out["data"]["errors"]) == 2
    assert out["data"]["errors"][0]["code"] == "CAPABILITY_CHECK_FAILED"
    assert out["data"]["errors"][0]["details"]["code"] == "TRANSPORT_CONNECTION_RESET"
    assert out["data"]["errors"][0]["details"]["backend_diagnostics"] == "managed diagnostics tail"
    assert out["data"]["errors"][1]["details"]["code"] == "TRANSPORT_CONNECTION_REFUSED"


def test_debugger_wait_for_core_state_success(server_module, monkeypatch):
    states = iter([1, 1, 0])

    def fake_call(method, *args, **kwargs):
        if method == "getCoreState":
            return next(states)
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    out = server_module.debugger_wait_for_core_state(
        desired_state=0,
        core=0,
        timeout_ms=500,
        poll_interval_ms=0,
    )
    assert out["ok"] is True
    assert out["tool"] == "debugger_wait_for_core_state"
    assert out["data"]["timed_out"] is False
    assert out["data"]["state"] == 0
    assert out["data"]["attempts"] == 3


def test_debugger_wait_for_core_state_timeout(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_call_debugger", lambda method, *args, **kwargs: 2)

    out = server_module.debugger_wait_for_core_state(
        desired_state=0,
        core=0,
        timeout_ms=0,
        poll_interval_ms=0,
    )
    assert out["ok"] is False
    assert out["tool"] == "debugger_wait_for_core_state"
    assert out["error"]["code"] == "TIMEOUT"
    assert out["data"]["timed_out"] is True
    assert out["data"]["state"] == 2
    assert out["data"]["attempts"] >= 1


def test_debugger_go_and_wait_for_core_state(server_module, monkeypatch):
    states = iter([1, 0])

    def fake_call(method, *args, **kwargs):
        if method == "go":
            return None
        if method == "getCoreState":
            return next(states)
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    out = server_module.debugger_go_and_wait_for_core_state(
        desired_state=0,
        core=0,
        timeout_ms=500,
        poll_interval_ms=0,
    )
    assert out["ok"] is True
    assert out["tool"] == "debugger_go_and_wait_for_core_state"
    assert out["data"]["started_running"] is True
    assert out["data"]["state"] == 0


def test_debugger_configure_and_start_flow(server_module, monkeypatch):
    calls = []

    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    cfg_out = server_module.debugger_configure_session('{"request":"launch"}')
    assert cfg_out["ok"] is True
    assert cfg_out["tool"] == "debugger_configure_session"
    assert cfg_out["data"] == {"ok": True}

    start_out = server_module.debugger_start_smp_session()
    assert start_out["ok"] is True
    assert start_out["tool"] == "debugger_start_smp_session"
    assert start_out["data"] == {"ok": True}

    method_names = [m for m, _, _ in calls]
    assert method_names.count("resolveLaunchConfiguration") == 1
    assert method_names.count("configureSession") == 1
    assert method_names.count("startSMPSession") == 1


def test_debugger_configure_and_start_wrapper(server_module, monkeypatch):
    calls = []

    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    out = server_module.debugger_configure_and_start_session('{"request":"launch"}')
    assert out["ok"] is True
    assert out["tool"] == "debugger_configure_and_start_session"
    assert out["data"]["ok"] is True
    assert out["data"]["configured"] is True
    assert out["data"]["started"] is True
    assert out["data"]["handoff"]["had_active_session"] is True
    assert out["data"]["handoff"]["stop_attempted"] is True
    assert out["data"]["handoff"]["stop_ok"] is True
    assert out["data"]["handoff"]["strict_cleanup_used"] is False

    method_names = [m for m, _, _ in calls]
    assert method_names.count("stopSession") == 1
    assert method_names.count("resolveLaunchConfiguration") == 1
    assert method_names.count("configureSession") == 1
    assert method_names.count("startSMPSession") == 1


def test_debugger_configure_and_start_wrapper_runs_to_stop_on_symbol(server_module, monkeypatch):
    calls = []

    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    launch_json = json.dumps({"request": "launch", "stopOnSymbol": "main"})
    out = server_module.debugger_configure_and_start_session(launch_json)

    assert out["ok"] is True
    assert out["data"]["stopOnSymbol"] == "main"
    assert out["data"]["ranToSymbol"] is True
    assert "stopOnSymbolError" not in out["data"]

    run_to_ule_calls = [c for c in calls if c[0] == "runToULE"]
    assert len(run_to_ule_calls) == 1
    _, args, _ = run_to_ule_calls[0]
    assert args == ("main", True)

    # runToULE must happen after configure/start, not before.
    method_names = [m for m, _, _ in calls]
    assert method_names.index("startSMPSession") < method_names.index("runToULE")


def test_debugger_configure_and_start_wrapper_without_stop_on_symbol_skips_run_to_ule(
    server_module, monkeypatch
):
    calls = []

    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    out = server_module.debugger_configure_and_start_session('{"request":"launch"}')

    assert out["ok"] is True
    assert out["data"]["stopOnSymbol"] is None
    assert out["data"]["ranToSymbol"] is False
    assert "stopOnSymbolError" not in out["data"]
    assert all(m != "runToULE" for m, _, _ in calls)


def test_debugger_configure_and_start_wrapper_reports_stop_on_symbol_error(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    def fake_call(method, *args, **kwargs):
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        if method == "runToULE":
            raise server_module.ThriftBridgeError("symbol not found: does_not_exist")
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    launch_json = json.dumps({"request": "launch", "stopOnSymbol": "does_not_exist"})
    out = server_module.debugger_configure_and_start_session(launch_json)

    # A failure to run to the requested symbol is surfaced, not fatal to the
    # overall configure+start call: session is still reported as started.
    assert out["ok"] is True
    assert out["data"]["configured"] is True
    assert out["data"]["started"] is True
    assert out["data"]["stopOnSymbol"] == "does_not_exist"
    assert out["data"]["ranToSymbol"] is False
    assert "does_not_exist" in out["data"]["stopOnSymbolError"]


def test_debugger_configure_and_start_wrapper_uses_cleanup_when_stop_fails(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    calls = []

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "stopSession":
            raise server_module.ThriftBridgeError("stop failed")
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)
    monkeypatch.setattr(
        server_module,
        "shutdown_managed_server",
        lambda: None,
    )

    out = server_module.debugger_configure_and_start_session('{"request":"launch"}')
    assert out["ok"] is True
    assert out["data"]["handoff"]["had_active_session"] is True
    assert out["data"]["handoff"]["stop_ok"] is False
    assert out["data"]["handoff"]["strict_cleanup_used"] is True
    assert out["data"]["handoff"]["strict_cleanup_ok"] is True


def test_debugger_configure_and_start_wrapper_handles_stale_backend_online(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    stale_session = {"active": True}
    calls = []

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "isOnline":
            return bool(stale_session["active"])
        if method == "stopSession":
            stale_session["active"] = False
            return None
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        if method == "configureSession":
            return None
        if method == "startSMPSession":
            if stale_session["active"]:
                raise server_module.ThriftBridgeError("stale backend session still active")
            return None
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    # Reproduces observed flake pattern: local flags are stopped, but backend still alive/online.
    server_module._set_session_state(configured=False, started=False)
    out = server_module.debugger_configure_and_start_session('{"request":"launch"}')
    assert out["ok"] is True
    assert out["data"]["configured"] is True
    assert out["data"]["started"] is True

    method_names = [m for m, _, _ in calls]
    assert method_names.count("isOnline") >= 1
    assert method_names.count("stopSession") == 1
    assert method_names.count("startSMPSession") == 1


def test_debugger_configure_and_start_wrapper_managed_forces_fresh_cleanup(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="managed",
            registry_host="127.0.0.1",
            registry_port=60000,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    cleanup_calls = []

    def fake_cleanup(reset_target=False):
        cleanup_calls.append(bool(reset_target))
        return {
            "ok": True,
            "tool": "debugger_strict_cleanup",
            "data": {"ok": True},
            "error": None,
        }

    monkeypatch.setattr(server_module, "debugger_strict_cleanup", fake_cleanup)

    calls = []

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    out = server_module.debugger_configure_and_start_session('{"request":"launch"}')
    assert out["ok"] is True
    assert out["data"]["handoff"]["managed_fresh_process"] is True
    assert out["data"]["handoff"]["strict_cleanup_used"] is True
    assert out["data"]["handoff"]["strict_cleanup_ok"] is True
    assert cleanup_calls == [False]

    method_names = [m for m, _, _ in calls]
    assert method_names.count("resolveLaunchConfiguration") == 1
    assert method_names.count("configureSession") == 1
    assert method_names.count("startSMPSession") == 1
    assert "isOnline" not in method_names


def test_debugger_call_parses_args(server_module, monkeypatch):
    seen = []

    def fake_call(method, *args, **kwargs):
        seen.append((method, args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module.debugger_call("foo", "[1,2]")
    server_module.debugger_call("bar", '{"x":1}')
    server_module.debugger_call("baz", "3")

    assert seen[0] == ("foo", (1, 2), {})
    assert seen[1] == ("bar", (), {"x": 1})
    assert seen[2] == ("baz", (3,), {})

    with pytest.raises(server_module.ThriftBridgeError):
        server_module.debugger_call("bad", "{not json}")


def test_debugger_call_lifecycle_state_sync(server_module, monkeypatch):
    def fake_call(method, *args, **kwargs):
        if method == "configureSession":
            return None
        if method == "startSMPSession":
            return None
        if method == "stopSession":
            return None
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module._set_session_state(configured=False, started=False)
    server_module.debugger_call("configureSession", "[{}]")
    assert server_module._session_state() == {"configured": True, "started": False}

    server_module.debugger_call("startSMPSession", "[]")
    assert server_module._session_state() == {"configured": True, "started": True}

    server_module.debugger_call("stopSession", "[]")
    assert server_module._session_state() == {"configured": False, "started": False}


def test_debugger_call_stop_session_idempotent_recovery(server_module, monkeypatch):
    class FakeCSpyException(Exception):
        def __init__(self):
            self.code = 6
            self.method = "DkStop"
            self.message = "Failed to suspend debugger"
            self.culprit = ""
            super().__init__(str(self))

        def __str__(self):
            return (
                "CSpyException(code=6, method='DkStop', "
                "message='Failed to suspend debugger', culprit='')"
            )

    def fake_call(method, *args, **kwargs):
        if method == "stopSession":
            raise FakeCSpyException()
        if method == "isOnline":
            return True
        if method == "getNumberOfCores":
            return 1
        if method == "getCoreState":
            return 0
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module._set_session_state(configured=True, started=True)
    out = server_module.debugger_call("stopSession", "[]")
    assert out["ok"] is True
    assert out["already_stopped"] is True
    assert out["stop_idempotent_recovered"] is True
    assert server_module._session_state() == {"configured": False, "started": False}


def test_debugger_configure_failure_resets_state(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    def fake_call(method, *args, **kwargs):
        if method == "resolveLaunchConfiguration":
            return {"resolved": True}
        if method == "configureSession":
            raise server_module.ThriftBridgeError("configure failed")
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module._set_session_state(configured=True, started=True)
    with pytest.raises(server_module.ThriftBridgeError):
        server_module.debugger_configure_session('{"request":"launch"}')

    assert server_module._session_state() == {"configured": False, "started": False}


def test_debugger_stop_failure_still_resets_local_state(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )
    monkeypatch.setattr(
        server_module,
        "_call_debugger",
        lambda method, *args, **kwargs: (_ for _ in ()).throw(server_module.ThriftBridgeError("stop failed")),
    )

    server_module._set_session_state(configured=True, started=True)
    with pytest.raises(server_module.ThriftBridgeError):
        server_module.debugger_stop_session()

    assert server_module._session_state() == {"configured": False, "started": False}


def test_debugger_stop_session_idempotent_when_already_stopped(server_module, monkeypatch):
    called = []

    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )

    def fake_call(method, *args, **kwargs):
        called.append(method)
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module._set_session_state(configured=False, started=False)
    out = server_module.debugger_stop_session()
    assert out["ok"] is True
    assert out["tool"] == "debugger_stop_session"
    assert out["data"] == {
        "ok": True,
        "already_stopped": True,
        "managed_backend_restarted_on_next_start": False,
    }
    assert called == []


def test_debugger_stop_session_managed_stops_backend_even_when_locally_stopped(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="managed",
            registry_host="127.0.0.1",
            registry_port=60000,
            iar_path=None,
            launcher_executable=None,
        ),
    )

    calls = []

    monkeypatch.setattr(server_module, "_call_debugger", lambda method, *a, **k: calls.append(method))

    shutdown_calls = []
    monkeypatch.setattr(server_module, "shutdown_managed_server", lambda: shutdown_calls.append(True))

    server_module._set_session_state(configured=False, started=False)
    out = server_module.debugger_stop_session()
    assert out["ok"] is True
    assert out["data"]["already_stopped"] is True
    assert out["data"]["managed_backend_restarted_on_next_start"] is True
    assert calls == []
    assert shutdown_calls == [True]


def test_debugger_stop_session_managed_stops_then_shutdowns(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="managed",
            registry_host="127.0.0.1",
            registry_port=60000,
            iar_path=None,
            launcher_executable=None,
        ),
    )

    calls = []

    def fake_call(method, *args, **kwargs):
        calls.append(method)
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    shutdown_calls = []
    monkeypatch.setattr(server_module, "shutdown_managed_server", lambda: shutdown_calls.append(True))

    server_module._set_session_state(configured=True, started=True)
    out = server_module.debugger_stop_session()
    assert out["ok"] is True
    assert out["data"]["managed_backend_restarted_on_next_start"] is True
    assert calls == ["stopSession"]
    assert shutdown_calls == [True]


def test_debugger_stop_session_treats_dkstop_code6_as_idempotent(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "load_config",
        lambda: SimpleNamespace(
            cspy_mode="standalone",
            registry_host="127.0.0.1",
            registry_port=49820,
            iar_path=None,
            launcher_executable=None,
        ),
    )

    class FakeCSpyException(Exception):
        def __init__(self):
            self.code = 6
            self.method = "DkStop"
            self.message = "Failed to suspend debugger"
            self.culprit = ""
            super().__init__(str(self))

        def __str__(self):
            return (
                "CSpyException(code=6, method='DkStop', "
                "message='Failed to suspend debugger', culprit='')"
            )

    def fake_call(method, *args, **kwargs):
        if method == "stopSession":
            raise FakeCSpyException()
        if method == "isOnline":
            return True
        if method == "getNumberOfCores":
            return 1
        if method == "getCoreState":
            return 0
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)

    server_module._set_session_state(configured=True, started=True)
    out = server_module.debugger_stop_session()
    assert out["ok"] is True
    assert out["data"]["stop_idempotent_recovered"] is True
    assert out["data"]["stop_idempotent_details"]["probe"]["already_stopped"] is True
    assert server_module._session_state() == {"configured": False, "started": False}


def test_debugger_strict_cleanup_stops_and_resets_state(server_module, monkeypatch, reset_server_state):
    calls = []

    def fake_call(method, *args, **kwargs):
        calls.append(method)
        return None

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)
    monkeypatch.setattr(server_module, "shutdown_managed_server", lambda: None)

    server_module._set_session_state(configured=True, started=True)
    server_module._LISTWINDOW_CONNECTED.add("WIN_X")
    server_module._LISTWINDOW_NOTES.append({"k": "v"})
    server_module._LIBSUPPORT_INPUT_BYTES.extend(b"abc")

    out = server_module.debugger_strict_cleanup(reset_target=True)
    assert out["ok"] is True
    assert out["tool"] == "debugger_strict_cleanup"
    assert out["data"]["before"] == {"configured": True, "started": True}
    assert out["data"]["after"] == {"configured": False, "started": False}
    assert server_module._LISTWINDOW_CONNECTED == set()
    assert len(server_module._LISTWINDOW_NOTES) == 0
    assert len(server_module._LIBSUPPORT_INPUT_BYTES) == 0
    assert "reset" in calls
    assert "stopSession" in calls


def test_debugger_strict_cleanup_reports_errors(server_module, monkeypatch):
    def fake_call(method, *args, **kwargs):
        raise server_module.ThriftBridgeError("boom")

    monkeypatch.setattr(server_module, "_call_debugger", fake_call)
    monkeypatch.setattr(
        server_module,
        "shutdown_managed_server",
        lambda: (_ for _ in ()).throw(RuntimeError("shutdown failed")),
    )

    server_module._set_session_state(configured=True, started=True)
    out = server_module.debugger_strict_cleanup(reset_target=True)
    assert out["ok"] is False
    assert out["tool"] == "debugger_strict_cleanup"
    assert out["error"]["code"] == "PARTIAL_CLEANUP_FAILED"
    assert out["data"]["after"] == {"configured": False, "started": False}
    assert any(e["message"] == "reset failed" for e in out["data"]["errors"])
    assert any(e["message"] == "stopSession failed" for e in out["data"]["errors"])
    assert any(e["message"] == "shutdown_managed_server failed" for e in out["data"]["errors"])


def test_debugger_error_taxonomy_tool(server_module):
    out = server_module.debugger_error_taxonomy()
    assert out["ok"] is True
    assert out["tool"] == "debugger_error_taxonomy"
    codes = {item["code"] for item in out["data"]["codes"]}
    assert "TIMEOUT" in codes
    assert "PARTIAL_CLEANUP_FAILED" in codes
    assert "CSPY_KDCLICENSEVIOLATION" in codes
    focus_values = {item["value"] for item in out["data"]["dc_result_focus"]}
    assert focus_values == {0, 5, 6, 7, 8, 9, 10, 15}
    constants = {item["name"]: item["value"] for item in out["data"]["dc_result_constants"]}
    assert constants["kDcLicenseViolation"] == 8


def test_classify_error_maps_cspy_license_violation(server_module):
    class FakeCSpyException(Exception):
        def __init__(self):
            self.code = 8
            self.method = "DkLoadModule"
            self.message = "Failed to load module"
            self.culprit = "test.out"
            super().__init__(str(self))

        def __str__(self):
            return (
                "CSpyException(code=8, method='DkLoadModule', "
                "message='Failed to load module', culprit='test.out')"
            )

    out = server_module._classify_error(FakeCSpyException())
    assert out["code"] == "CSPY_KDCLICENSEVIOLATION"
    assert out["category"] == "backend"
    assert "license" in out["message"].lower()
    assert out["details"]["cspy"]["code"] == 8


def test_breakpoint_validation_and_wrappers(server_module, monkeypatch):
    def fake_breakpoints(method, *args, **kwargs):
        if method == "setBreakpointOnUle":
            return {"id": 11, "valid": True}
        if method == "setBreakpointFromDescriptor":
            return {"id": 0, "valid": False}
        if method == "setBreakpointOnUleWithCategory":
            return {"id": 12, "valid": True}
        if method == "getBreakpoint":
            return {"id": args[0], "valid": True, "enabled": True}
        if method == "getBreakpoints":
            return [{"id": 11}]
        if method == "enableBreakpoint":
            return True
        if method == "removeBreakpoint":
            return True
        if method == "getRecentlyHitBreakpoints":
            return [{"id": 11}]
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_breakpoints", fake_breakpoints)

    bp = server_module.breakpoints_set_on_ule("main", 1)
    assert bp["id"] == 11
    assert bp["enabled"] is True

    with pytest.raises(server_module.ThriftBridgeError):
        server_module.breakpoints_set_from_descriptor('{"ule":"main"}')

    bp2 = server_module.breakpoints_set_on_ule_with_category("main", 1, "STD_CODE")
    assert bp2["id"] == 12
    assert server_module.breakpoints_get_all() == [{"id": 11}]
    assert server_module.breakpoints_enable(11, True) is True
    assert server_module.breakpoints_remove(11) is True
    assert server_module.breakpoints_recently_hit() == [{"id": 11}]


def test_listwindow_tools(server_module, monkeypatch, reset_server_state):
    monkeypatch.setattr(
        server_module,
        "_list_registry_services",
        lambda name_filter="": [{"name": "WIN_SLIDING_TRACE_WINDOW"}] if "trace" in name_filter.lower() else [],
    )

    def fake_listwindow(service_name, method, *args, **kwargs):
        if method == "getDisplayName":
            return "Trace"
        if method == "getColumnInfo":
            return [{"title": "PC"}]
        if method == "getListSpec":
            return {"canSort": True}
        if method == "isSliding":
            return True
        if method == "getChunkInfo":
            return {"numberOfRows": 2}
        if method == "setVisibleRows":
            return None
        if method == "getRow":
            idx = int(args[0])
            return {"cells": [{"text": f"row{idx}"}]}
        if method == "navigateToFraction":
            return {"ok": True}
        raise AssertionError(method)

    def fake_trace_listwindow(service_name, method, *args, **kwargs):
        if method == "isEnabled":
            return True
        if method in {"canEnable", "canClear", "canBrowse", "supportsTraceSettings"}:
            return True
        if method == "isBrowsing":
            return False
        if method == "getProgress":
            return {"state": "ready"}
        if method in {"setEnabled", "clear"}:
            return None
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_listwindow", fake_listwindow)
    monkeypatch.setattr(server_module, "_call_trace_listwindow", fake_trace_listwindow)

    assert server_module.listwindow_list_services("trace")[0]["name"] == "WIN_SLIDING_TRACE_WINDOW"

    overview = server_module.listwindow_get_overview("WIN_SLIDING_TRACE_WINDOW")
    assert overview["is_sliding"] is True
    assert overview["row_count"] == 2

    rows = server_module.listwindow_get_rows("WIN_SLIDING_TRACE_WINDOW", 0, 10)
    assert rows["returned"] == 2
    assert rows["rows"][0]["row"]["cells"][0]["text"] == "row0"

    nav = server_module.listwindow_sliding_navigate("WIN_SLIDING_TRACE_WINDOW")
    assert nav["row_count"] == 2

    status = server_module.listwindow_trace_status("WIN_SLIDING_TRACE_WINDOW")
    assert status["is_enabled"] is True
    assert server_module.listwindow_trace_set_enabled("WIN_SLIDING_TRACE_WINDOW", True)["ok"] is True
    assert server_module.listwindow_trace_clear("WIN_SLIDING_TRACE_WINDOW")["ok"] is True


def test_listwindow_notifications_buffer(server_module, reset_server_state):
    server_module._LISTWINDOW_NOTES.append({"kind": "n"})
    server_module._LISTWINDOW_TOOLBAR_NOTES.append({"kind": "t"})
    out = server_module.listwindow_get_notifications(clear=True)
    assert out["note_count"] == 1
    assert out["toolbar_note_count"] == 1

    out2 = server_module.listwindow_get_notifications(clear=False)
    assert out2["note_count"] == 0
    assert out2["toolbar_note_count"] == 0


def test_libsupport_tools(server_module, monkeypatch, reset_server_state):
    assert server_module.libsupport_push_input("abc", append_newline=True)["added_bytes"] == 4

    class FakeLibsupport:
        @staticmethod
        def requestInputBinary(length):
            return b"xy"[: int(length)]

        @staticmethod
        def requestInput(length):
            return "xy"[: int(length)]

    monkeypatch.setattr(server_module, "_call_libsupport", lambda method, *a, **k: getattr(FakeLibsupport, method)(*a))

    out_bin = server_module.libsupport_request_input_binary(2)
    assert out_bin["data_hex"] == "7879"
    assert server_module.libsupport_request_input(1) == "x"

    # Feed captured output buffers.
    server_module._append_libsupport_output(b"hello")
    status = server_module.libsupport_get_output(clear=True, max_chars=32)
    assert status["text"].endswith("hello")
    assert status["bytes_len"] >= 5

    cleared = server_module.libsupport_get_output(clear=False, max_chars=32)
    assert cleared["text_len"] == 0


def test_context_symbols_memory_disassembly_and_source(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_context_ref_from_json", lambda _: "CTX")

    def fake_ctx(method, *args, **kwargs):
        if method == "getStack":
            return [{"fn": "main"}]
        if method == "getStackDepth":
            return 3
        if method == "getContextInfo":
            return {"name": "ctx"}
        if method == "getLocals":
            return [{"name": "a"}, {"name": "x"}]
        if method == "getParameters":
            return [{"name": "x"}, {"name": "y"}]
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_contextmanager", fake_ctx)
    monkeypatch.setattr(server_module, "_shared_module", dummy_shared_module)
    monkeypatch.setattr(
        server_module,
        "_call_debugger",
        lambda method, *a, **k: {"value": "42"} if method == "evalExpression" else None,
    )

    assert server_module.contextmanager_get_stack("", 0, 10)[0]["fn"] == "main"
    assert server_module.contextmanager_get_stack_depth("", 256) == 3
    assert server_module.contextmanager_get_context_info("")["name"] == "ctx"
    assert server_module.contextmanager_get_locals("")[0]["name"] == "a"
    assert server_module.contextmanager_get_parameters("")[0]["name"] == "x"

    vis = server_module.symbols_list_visible("")
    assert vis["all"] == ["a", "x", "y"]

    lookup = server_module.symbols_lookup("x", prefix=False)
    assert lookup["matches"] == ["x"]
    assert lookup["evaluations"][0]["value"]["value"] == "42"

    monkeypatch.setattr(server_module, "_location", lambda z, a: (z, a))
    monkeypatch.setattr(server_module, "_call_memory", lambda method, *a, **k: b"\x01\x02")
    rd = server_module.memory_read(1, 0x100, 1, 8, 2)
    assert rd["hex"] == "0102"

    calls = []

    def fake_mem_write(method, *args, **kwargs):
        calls.append((method, args))
        return None

    monkeypatch.setattr(server_module, "_call_memory", fake_mem_write)
    wr = server_module.memory_write_hex(1, 0x100, "aabb", 1, 8, None)
    assert wr["written_bytes"] == 2
    assert calls[0][0] == "writeMemory"

    monkeypatch.setattr(server_module, "_call_disassembly", lambda method, *a, **k: [{"asm": "nop"}])
    dis = server_module.disassembly_disassemble_range(1, 0, 1, 4, "")
    assert dis[0]["asm"] == "nop"

    monkeypatch.setattr(server_module, "_call_sourcelookup", lambda method, *a, **k: [{"file": "main.c"}])
    src = server_module.sourcelookup_get_source_ranges(1, 0x20)
    assert src[0]["file"] == "main.c"


def test_register_snapshot(server_module, monkeypatch):
    cfg = SimpleNamespace(include_dirs=["e:/mcp/thrift"], timeout_ms=1000)
    monkeypatch.setattr(server_module, "load_config", lambda: cfg)

    def fake_dbg(method, *args, **kwargs):
        if method == "getRegisterGroups":
            return ["CPU Registers (ABI)"]
        if method == "getLocationNamesInGroup":
            return ["r0", "r1"]
        if method == "getNamedLocation":
            name = args[0]
            return SimpleNamespace(
                name=name,
                nameAlias=name.upper(),
                description="reg",
                fullBitSize=32,
                readonly=False,
                writeonly=False,
                location=f"LOC:{name}",
            )
        raise AssertionError(method)

    monkeypatch.setattr(server_module, "_call_debugger", fake_dbg)
    monkeypatch.setattr(server_module, "_find_include_thrift", lambda dirs, fn: Path("e:/mcp/thrift/memory.thrift"))

    class DummyMemClient:
        def readMemory(self, location, wordsize, bitsize, count):
            return b"\x34\x12\x00\x00"

        def close(self):
            return None

    monkeypatch.setattr(server_module, "resolve_service_endpoint", lambda cfg, svc: ("127.0.0.1", 1))
    monkeypatch.setattr(server_module, "make_client", lambda service, host, port, timeout=0: DummyMemClient())

    class DummyMemService:
        pass

    monkeypatch.setattr(
        server_module,
        "load_thrift_module",
        lambda p, d: SimpleNamespace(CSpyMemory=DummyMemService),
    )

    out = server_module.debugger_register_snapshot(limit=2)
    assert out["returned"] == 2
    assert out["registers"][0]["raw_hex"] == "34120000"


def test_mcp_tool_inventory(server_module):
    expected = {
        "thrift_connection_info",
        "debugger_list_methods",
        "debugger_get_version",
        "debugger_is_online",
        "debugger_get_number_of_cores",
        "debugger_get_core_state",
        "debugger_session_status",
        "debugger_configure_session",
        "debugger_start_smp_session",
        "debugger_configure_and_start_session",
        "libsupport_get_output",
        "libsupport_clear_output",
        "libsupport_push_input",
        "libsupport_request_input_binary",
        "libsupport_request_input",
        "listwindow_list_services",
        "listwindow_get_overview",
        "listwindow_get_rows",
        "listwindow_sliding_navigate",
        "listwindow_get_notifications",
        "listwindow_trace_status",
        "listwindow_trace_set_enabled",
        "listwindow_trace_clear",
        "debugger_stop_session",
        "breakpoints_get_all",
        "breakpoints_get",
        "breakpoints_set_from_descriptor",
        "breakpoints_set_on_ule",
        "breakpoints_set_on_ule_with_category",
        "breakpoints_enable",
        "breakpoints_remove",
        "breakpoints_recently_hit",
        "contextmanager_get_stack",
        "contextmanager_get_stack_depth",
        "contextmanager_get_context_info",
        "contextmanager_get_locals",
        "contextmanager_get_parameters",
        "symbols_list_visible",
        "symbols_lookup",
        "memory_read",
        "memory_write_hex",
        "disassembly_disassemble_range",
        "sourcelookup_get_source_ranges",
        "debugger_load_module",
        "debugger_get_modules",
        "debugger_go",
        "debugger_stop",
        "debugger_reset",
        "debugger_step_over",
        "debugger_get_thread_list",
        "debugger_get_cycle_counter",
        "debugger_eval_expression",
        "debugger_wait_for_core_state",
        "debugger_go_and_wait_for_core_state",
        "debugger_strict_cleanup",
        "debugger_capabilities",
        "debugger_error_taxonomy",
        "debugger_register_snapshot",
        "debugger_call",
    }

    missing = [name for name in expected if not hasattr(server_module, name)]
    assert not missing, f"Missing tool wrappers: {missing}"
