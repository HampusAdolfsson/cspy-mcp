"""The MCP tool surface: names, argument parsing, result shapes and envelopes.

The behavior behind the tools is tested with the API (in iar-cspy, cspy-py);
these tests pin down what MCP clients see.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from iar_cspy import Config, CSpyError, SessionError
from iar_cspy.testing import cspy_exception, fake_client

LAUNCH_JSON = '{"request":"launch"}'


def lifecycle(rpc, **overrides):
    rpc.on("debugger", "resolveLaunchConfiguration", {"resolved": True})
    for method in ("configureSession", "startSMPSession", "stopSession", "runToULE"):
        rpc.on("debugger", method, None)
    rpc.on("debugger", "isOnline", False)
    rpc.on("debugger", "getCoreState", 0)  # run_to(stopOnSymbol) waits for the stop
    for method, response in overrides.items():
        rpc.on("debugger", method, response)
    return rpc


def test_tool_inventory(server):
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    expected = {
        "thrift_connection_info", "debugger_list_methods", "debugger_get_version", "debugger_is_online",
        "debugger_get_number_of_cores", "debugger_get_core_state", "debugger_session_status",
        "debugger_configure_session", "debugger_start_smp_session", "debugger_configure_and_start_session",
        "libsupport_get_output", "libsupport_clear_output", "libsupport_push_input",
        "libsupport_request_input_binary", "libsupport_request_input", "listwindow_list_services",
        "listwindow_get_overview", "listwindow_get_rows", "listwindow_sliding_navigate",
        "listwindow_get_notifications", "listwindow_trace_status", "listwindow_trace_set_enabled",
        "listwindow_trace_clear", "debugger_stop_session", "breakpoints_get_all", "breakpoints_get",
        "breakpoints_set_from_descriptor", "breakpoints_set_on_ule", "breakpoints_set_on_ule_with_category",
        "breakpoints_enable", "breakpoints_remove", "breakpoints_recently_hit", "contextmanager_get_stack",
        "contextmanager_get_stack_depth", "contextmanager_get_context_info", "contextmanager_get_locals",
        "contextmanager_get_parameters", "symbols_list_visible", "symbols_lookup", "memory_read",
        "memory_write_hex", "disassembly_disassemble_range", "sourcelookup_get_source_ranges",
        "debugger_load_module", "debugger_get_modules", "debugger_go", "debugger_stop", "debugger_reset",
        "debugger_step_over", "debugger_get_thread_list", "debugger_get_cycle_counter",
        "debugger_eval_expression", "debugger_wait_for_core_state", "debugger_go_and_wait_for_core_state",
        "debugger_strict_cleanup", "debugger_capabilities", "debugger_error_taxonomy",
        "debugger_register_snapshot", "debugger_call", "project_load_workspace", "project_status",
        "project_get_files", "project_build", "project_get_launch_config", "project_configure_and_start_debug",
        "projectmanager_call", "ide_services_status", "ide_services_ensure", "ide_services_stop_launcher",
        "options_create_session", "options_destroy_session", "options_get_category_tree",
        "options_get_option_tree", "options_update_state", "options_commit", "options_call",
    }  # fmt: skip
    assert names == expected


def test_session_gate(server, fake):
    client, _ = fake
    client.debugger.set_state(configured=False, started=False)
    with pytest.raises(SessionError, match=r"debugger_go requires an active debug session.*debugger_configure_session"):
        server.debugger_go()


def test_thrift_connection_info(server, fake):
    info = server.thrift_connection_info()
    assert info["registry_port"] == 4711
    assert info["cspy_mode"] == "standalone"
    assert info["ide_services"] == ["options", "projectmanager"]
    assert "managed_server" not in info
    json.dumps(info)


def test_debugger_wrappers(server, fake):
    _, rpc = fake
    for method, response in {
        "getVersionString": "9.5.4.0",
        "isOnline": True,
        "getNumberOfCores": 2,
        "getCoreState": lambda core: core,
        "getModules": [{"name": "mod"}],
        "getThreadList": [{"id": 1}],
        "getCycleCounter": 123,
        "go": None,
        "stop": None,
        "reset": None,
        "stepOver": None,
        "loadModule": None,
    }.items():
        rpc.on("debugger", method, response)

    assert server.debugger_get_version() == "9.5.4.0"
    assert server.debugger_is_online() is True
    assert server.debugger_get_number_of_cores() == 2
    assert server.debugger_get_core_state(1) == 1
    assert server.debugger_get_thread_list() == [{"id": 1}]
    assert server.debugger_get_cycle_counter(0) == 123
    assert server.debugger_go() == {"ok": True}
    assert server.debugger_stop() == {"ok": True}
    assert server.debugger_reset() == {"ok": True}
    assert server.debugger_step_over() == {"ok": True}
    assert server.debugger_load_module("a.out") == {"ok": True, "filename": "a.out"}
    assert server.debugger_get_modules() == [{"name": "mod"}]

    status = server.debugger_session_status()
    assert (status["ok"], status["tool"], status["error"]) == (True, "debugger_session_status", None)
    data = status["data"]
    assert (data["configured"], data["started"], data["backend_online"]) == (True, True, True)
    assert (data["core_count"], data["core_states"]) == (2, [0, 1])


def test_debugger_list_methods(server, fake):
    assert "configureSession" in server.debugger_list_methods()


def test_configure_and_start_flow(server, fake):
    client, rpc = fake
    client.debugger.set_state(configured=False, started=False)
    lifecycle(rpc)

    out = server.debugger_configure_session(LAUNCH_JSON)
    assert out == {"ok": True, "tool": "debugger_configure_session", "data": {"ok": True}, "error": None}
    out = server.debugger_start_smp_session()
    assert out == {"ok": True, "tool": "debugger_start_smp_session", "data": {"ok": True}, "error": None}
    assert rpc.methods() == ["resolveLaunchConfiguration", "configureSession", "startSMPSession"]


def test_configure_and_start_session(server, fake):
    _, rpc = fake
    lifecycle(rpc)
    out = server.debugger_configure_and_start_session(json.dumps({"request": "launch", "stopOnSymbol": "main"}))
    assert out["ok"] is True and out["tool"] == "debugger_configure_and_start_session"
    data = out["data"]
    assert (data["ok"], data["configured"], data["started"]) == (True, True, True)
    assert data["handoff"]["had_active_session"] is True
    assert data["handoff"]["stop_ok"] is True
    assert data["handoff"]["strict_cleanup_used"] is False
    assert (data["stopOnSymbol"], data["ranToSymbol"]) == ("main", True)
    assert "stopOnSymbolError" not in data


def test_configure_and_start_session_reports_stop_on_symbol_error(server, fake):
    _, rpc = fake
    lifecycle(rpc, runToULE=CSpyError("symbol not found: does_not_exist"))
    data = server.debugger_configure_and_start_session('{"stopOnSymbol": "does_not_exist"}')["data"]
    assert data["started"] is True
    assert data["ranToSymbol"] is False
    assert "does_not_exist" in data["stopOnSymbolError"]


def test_configure_and_start_session_without_stop_on_symbol(server, fake):
    _, rpc = fake
    lifecycle(rpc)
    data = server.debugger_configure_and_start_session(LAUNCH_JSON)["data"]
    assert (data["stopOnSymbol"], data["ranToSymbol"]) == (None, False)
    assert "runToULE" not in rpc.methods()


def test_stop_session_when_already_stopped(server, fake):
    client, rpc = fake
    client.debugger.set_state(configured=False, started=False)
    out = server.debugger_stop_session()
    assert out["tool"] == "debugger_stop_session"
    assert out["data"] == {"ok": True, "already_stopped": True, "managed_backend_restarted_on_next_start": False}
    assert rpc.calls == []


def test_stop_session_idempotent_recovery(server, fake):
    _, rpc = fake
    rpc.on("debugger", "stopSession", cspy_exception())
    rpc.on("debugger", "isOnline", True).on("debugger", "getNumberOfCores", 1).on("debugger", "getCoreState", 0)
    data = server.debugger_stop_session()["data"]
    assert data["ok"] is True
    assert data["managed_backend_restarted_on_next_start"] is False
    assert data["stop_idempotent_recovered"] is True
    assert data["stop_idempotent_details"]["probe"]["already_stopped"] is True


def test_strict_cleanup(server, fake):
    client, rpc = fake
    rpc.on("debugger", "reset").on("debugger", "stopSession")
    out = server.debugger_strict_cleanup(reset_target=True)
    assert (out["ok"], out["tool"], out["error"]) == (True, "debugger_strict_cleanup", None)
    assert out["data"]["before"] == {"configured": True, "started": True}
    assert out["data"]["after"] == {"configured": False, "started": False}
    assert out["data"]["managed_server_shutdown"] is True


def test_strict_cleanup_reports_errors(server, fake):
    _, rpc = fake
    rpc.on("debugger", "reset", CSpyError("boom")).on("debugger", "stopSession", CSpyError("boom"))
    out = server.debugger_strict_cleanup(reset_target=True)
    assert out["ok"] is False
    assert out["error"]["code"] == "PARTIAL_CLEANUP_FAILED"
    assert {e["message"] for e in out["data"]["errors"]} == {"reset failed", "stopSession failed"}
    assert all(e["code"] == "CLEANUP_STEP_FAILED" for e in out["data"]["errors"])


def test_capabilities(server, fake):
    client, rpc = fake
    client.registry.set(["debugger"])
    rpc.on("debugger", "isOnline", True).on("debugger", "getNumberOfCores", 4)
    data = server.debugger_capabilities()["data"]
    assert data["backend_mode"] == "standalone"
    assert data["session"] == {"configured": True, "started": True}
    assert (data["backend_online"], data["core_count"]) == (True, 4)
    assert data["services"][0]["name"] == "debugger"
    assert "go" in data["debugger_methods"]


def test_capabilities_collects_structured_errors(server, fake, monkeypatch):
    client, rpc = fake
    client.debugger.set_state(configured=False, started=False)
    monkeypatch.setattr(client.backend, "diagnostics", lambda: {"backend_diagnostics": "managed diagnostics tail"})
    rpc.on("debugger", "isOnline", CSpyError("Connection reset (WinError 10054)"))
    monkeypatch.setattr(client.debugger, "methods", lambda: (_ for _ in ()).throw(CSpyError("connection refused")))

    data = server.debugger_capabilities()["data"]
    assert [e["code"] for e in data["errors"]] == ["CAPABILITY_CHECK_FAILED"] * 2
    assert data["errors"][0]["details"]["code"] == "TRANSPORT_CONNECTION_RESET"
    assert data["errors"][0]["details"]["backend_diagnostics"] == "managed diagnostics tail"
    assert data["errors"][1]["details"]["code"] == "TRANSPORT_CONNECTION_REFUSED"


def test_wait_for_core_state(server, fake):
    _, rpc = fake
    states = iter([1, 1, 0])
    rpc.on("debugger", "getCoreState", lambda core: next(states))
    out = server.debugger_wait_for_core_state(desired_state=0, timeout_ms=500, poll_interval_ms=0)
    assert (out["ok"], out["tool"]) == (True, "debugger_wait_for_core_state")
    assert (out["data"]["timed_out"], out["data"]["state"], out["data"]["attempts"]) == (False, 0, 3)


def test_wait_for_core_state_timeout(server, fake):
    _, rpc = fake
    rpc.on("debugger", "getCoreState", 2)
    out = server.debugger_wait_for_core_state(desired_state=0, timeout_ms=0, poll_interval_ms=0)
    assert out["ok"] is False
    assert out["error"]["code"] == "TIMEOUT"
    assert (out["data"]["timed_out"], out["data"]["state"]) == (True, 2)


def test_go_and_wait_for_core_state(server, fake):
    _, rpc = fake
    states = iter([1, 0])
    rpc.on("debugger", "go").on("debugger", "getCoreState", lambda core: next(states))
    out = server.debugger_go_and_wait_for_core_state(timeout_ms=500, poll_interval_ms=0)
    assert (out["ok"], out["tool"]) == (True, "debugger_go_and_wait_for_core_state")
    assert out["data"]["started_running"] is True


def test_error_taxonomy(server):
    data = server.debugger_error_taxonomy()["data"]
    codes = {item["code"] for item in data["codes"]}
    assert {"TIMEOUT", "PARTIAL_CLEANUP_FAILED", "CSPY_KDCLICENSEVIOLATION"} <= codes
    assert {item["value"] for item in data["dc_result_focus"]} == {0, 5, 6, 7, 8, 9, 10, 15}
    assert {item["name"]: item["value"] for item in data["dc_result_constants"]}["kDcLicenseViolation"] == 8


def test_eval_expression(server, fake):
    _, rpc = fake
    rpc.on("debugger", "evalExpression", {"value": "7"})
    assert server.debugger_eval_expression("x + 1", '{"type": "Stack", "level": 1}')["value"] == "7"
    ref = rpc.args("debugger", "evalExpression")[0][0]
    assert (ref.type, ref.level) == (2, 1)
    with pytest.raises(CSpyError):
        server.debugger_eval_expression("  ")
    with pytest.raises(CSpyError, match="context_json must be a JSON object"):
        server.debugger_eval_expression("x", "[]")


def test_register_snapshot(server, fake, monkeypatch):
    client, _ = fake
    from iar_cspy.types import Register, RegisterSnapshot

    snapshot = RegisterSnapshot("CPU", "CPU", ["CPU"], 3)
    snapshot.registers = [
        Register("r0", "R0", "", 32, {"address": 0}, False, False, raw=b"\x34\x12\x00\x00"),
        Register("r1", "R1", "", 32, {"address": 4}, False, False, read_error="nope"),
    ]
    monkeypatch.setattr(client.debugger, "registers", lambda group, limit: snapshot)
    out = server.debugger_register_snapshot(limit=2)
    assert (out["returned"], out["total_in_group"]) == (2, 3)
    assert out["registers"][0]["raw_hex"] == "34120000"
    assert out["registers"][0]["value_unsigned_le"] == 0x1234
    assert out["registers"][1]["read_error"] == "nope"


def test_debugger_call_parses_args(server, fake):
    _, rpc = fake
    for method in ("foo", "bar", "baz"):
        rpc.on("debugger", method, {"ok": True})
    server.debugger_call("foo", "[1,2]")
    server.debugger_call("bar", '{"x":1}')
    server.debugger_call("baz", "3")
    assert [(m, a, k) for _, m, a, k in rpc.calls] == [("foo", (1, 2), {}), ("bar", (), {"x": 1}), ("baz", (3,), {})]
    with pytest.raises(CSpyError, match="Invalid JSON"):
        server.debugger_call("bad", "{not json}")


def test_debugger_call_gates_all_but_lifecycle_methods(server, fake):
    client, rpc = fake
    client.debugger.set_state(configured=False, started=False)
    rpc.on("debugger", "getVersionString", "9")
    assert server.debugger_call("getVersionString") == "9"
    with pytest.raises(SessionError):
        server.debugger_call("go")


def test_debugger_call_lifecycle_state_sync(server, fake):
    client, rpc = fake
    for method in ("configureSession", "startSMPSession", "stopSession"):
        rpc.on("debugger", method)
    client.debugger.set_state(configured=False, started=False)
    server.debugger_call("configureSession", "[{}]")
    assert client.debugger.state == {"configured": True, "started": False}
    server.debugger_call("startSMPSession", "[]")
    assert client.debugger.state == {"configured": True, "started": True}
    server.debugger_call("stopSession", "[]")
    assert client.debugger.state == {"configured": False, "started": False}


def test_debugger_call_stop_session_idempotent_recovery(server, fake):
    client, rpc = fake
    rpc.on("debugger", "stopSession", cspy_exception())
    rpc.on("debugger", "isOnline", True).on("debugger", "getNumberOfCores", 1).on("debugger", "getCoreState", 0)
    out = server.debugger_call("stopSession", "[]")
    assert (out["ok"], out["already_stopped"], out["stop_idempotent_recovered"]) == (True, True, True)
    assert client.debugger.state == {"configured": False, "started": False}


def test_breakpoint_tools(server, fake):
    _, rpc = fake
    rpc.on("breakpoints", "setBreakpointOnUle", {"id": 11, "valid": True})
    rpc.on("breakpoints", "setBreakpointFromDescriptor", {"id": 0, "valid": False})
    rpc.on("breakpoints", "setBreakpointOnUleWithCategory", {"id": 12, "valid": True})
    rpc.on("breakpoints", "getBreakpoint", lambda id: {"id": id, "valid": True, "enabled": True})
    rpc.on("breakpoints", "getBreakpoints", [{"id": 11}])
    rpc.on("breakpoints", "enableBreakpoint", True).on("breakpoints", "removeBreakpoint", True)
    rpc.on("breakpoints", "getRecentlyHitBreakpoints", [{"id": 11}])

    bp = server.breakpoints_set_on_ule("main", 1)
    assert (bp["id"], bp["enabled"]) == (11, True)
    with pytest.raises(CSpyError, match="breakpoints_get_all"):
        server.breakpoints_set_from_descriptor('{"ule":"main"}')
    assert server.breakpoints_set_on_ule_with_category("main", 1, "STD_CODE")["id"] == 12
    assert server.breakpoints_get_all() == [{"id": 11}]
    assert server.breakpoints_get(11)["id"] == 11
    assert server.breakpoints_enable(11, True) is True
    assert server.breakpoints_remove(11) is True
    assert server.breakpoints_recently_hit() == [{"id": 11}]


def test_inspection_tools(server, fake):
    _, rpc = fake
    rpc.on("contextmanager", "getStack", [{"fn": "main"}]).on("contextmanager", "getStackDepth", 3)
    rpc.on("contextmanager", "getContextInfo", {"name": "ctx"})
    rpc.on("contextmanager", "getLocals", [{"name": "a"}, {"name": "x"}])
    rpc.on("contextmanager", "getParameters", [{"name": "x"}, {"name": "y"}])
    rpc.on("debugger", "evalExpression", {"value": "42"})
    rpc.on("memory", "readMemory", b"\x01\x02").on("memory", "writeMemory")
    rpc.on("disassembly", "disassembleRange", [{"asm": "nop"}])
    rpc.on("sourcelookup", "getSourceRanges", [{"file": "main.c"}])

    assert server.contextmanager_get_stack("", 0, 10)[0]["fn"] == "main"
    assert server.contextmanager_get_stack_depth("", 256) == 3
    assert server.contextmanager_get_context_info("")["name"] == "ctx"
    assert server.contextmanager_get_locals("")[0]["name"] == "a"
    assert server.contextmanager_get_parameters("")[0]["name"] == "x"
    assert server.symbols_list_visible("") == {
        "locals": ["a", "x"],
        "parameters": ["x", "y"],
        "all": ["a", "x", "y"],
        "count": 3,
    }
    lookup = server.symbols_lookup("x", prefix=False)
    assert lookup["matches"] == ["x"]
    assert lookup["evaluations"][0]["value"]["value"] == "42"

    assert server.memory_read(1, 0x100, 1, 8, 2) == {
        "zone_id": 1, "address": 0x100, "wordsize": 1, "bitsize": 8, "count": 2, "hex": "0102", "byte_len": 2,
    }  # fmt: skip
    written = server.memory_write_hex(1, 0x100, "aabb", 1, 8, None)
    assert (written["written_bytes"], written["count"]) == (2, 2)
    assert server.disassembly_disassemble_range(1, 0, 1, 4, "")[0]["asm"] == "nop"
    assert server.sourcelookup_get_source_ranges(1, 0x20)[0]["file"] == "main.c"


def test_libsupport_tools(server, fake):
    client, rpc = fake
    assert server.libsupport_push_input("abc", append_newline=True) == {
        "ok": True,
        "queued_bytes": 4,
        "added_bytes": 4,
    }
    rpc.on("libsupport", "requestInputBinary", lambda n: b"xy"[:n]).on("libsupport", "requestInput", lambda n: "x")
    assert server.libsupport_request_input_binary(2)["data_hex"] == "7879"
    assert server.libsupport_request_input(1) == "x"

    client.terminal.server.handler.printOutputBinary(b"hello")
    out = server.libsupport_get_output(clear=True, max_chars=3)
    assert (out["text"], out["text_len"], out["bytes_len"]) == ("llo", 5, 5)
    assert server.libsupport_get_output()["text_len"] == 0
    assert server.libsupport_clear_output() == {"ok": True}


def test_listwindow_tools(server, fake, monkeypatch):
    client, _ = fake
    client.registry.set(["WIN_SLIDING_TRACE_WINDOW"])

    def call(service_name, method, *args, trace=False):
        if trace:
            return {
                "isEnabled": True, "canEnable": True, "canClear": True, "canBrowse": True, "isBrowsing": False,
                "supportsTraceSettings": True, "getProgress": {"state": "ready"},
            }.get(method)  # fmt: skip
        return {
            "getDisplayName": "Trace",
            "getColumnInfo": [{"title": "PC"}],
            "getListSpec": {"canSort": True},
            "isSliding": True,
            "getChunkInfo": {"numberOfRows": 2},
            "getRow": {"cells": [{"text": f"row{args[0] if args else ''}"}]},
            "navigateToFraction": {"ok": True},
        }.get(method)

    monkeypatch.setattr(client.listwindows, "call", call)
    name = "WIN_SLIDING_TRACE_WINDOW"
    assert server.listwindow_list_services("trace")[0]["name"] == name
    overview = server.listwindow_get_overview(name)
    assert (overview["is_sliding"], overview["row_count"], overview["display_name"]) == (True, 2, "Trace")
    rows = server.listwindow_get_rows(name, 0, 10)
    assert rows["returned"] == 2
    assert rows["rows"][0]["row"]["cells"][0]["text"] == "row0"
    assert server.listwindow_sliding_navigate(name)["row_count"] == 2
    assert server.listwindow_trace_status(name)["is_enabled"] is True
    assert server.listwindow_trace_set_enabled(name, True) == {"ok": True, "service_name": name, "enabled": True}
    assert server.listwindow_trace_clear(name) == {"ok": True, "service_name": name}


def test_listwindow_notifications(server, fake):
    client, _ = fake
    client.listwindows.server.handler.notify({"kind": "n"})
    client.listwindows.server.handler.notifyToolbar({"kind": "t"})
    out = server.listwindow_get_notifications(clear=True)
    assert (out["note_count"], out["toolbar_note_count"]) == (1, 1)
    assert server.listwindow_get_notifications()["note_count"] == 0


def test_managed_mode_status_includes_process_state(server, monkeypatch):
    client, rpc = fake_client(Config(mode="managed"))
    server.set_client(client)
    try:
        rpc.on("debugger", "isOnline", True)
        data = server.debugger_session_status()["data"]
        assert data["backend_mode"] == "managed"
        assert data["managed_server"]["mode"] == "managed"
        assert data["managed_server"]["running"] is False
    finally:
        server.set_client(None)
