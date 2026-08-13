from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class FakeProjectManager:
    """Dispatch table standing in for _call_projectmanager."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def __call__(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method not in self.responses:
            raise AssertionError(f"Unexpected ProjectManager call: {method}")
        value = self.responses[method]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(*args, **kwargs)
        return value

    def methods_called(self):
        return [name for name, _, _ in self.calls]


def test_resolve_project_and_config_explicit(server_module, monkeypatch):
    fake = FakeProjectManager()
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    ctx, cfg = server_module._resolve_project_and_config("/tmp/p.ewp", "Release")
    assert ctx == {"filename": "/tmp/p.ewp"}
    assert cfg == "Release"
    assert fake.calls == []


def test_resolve_project_and_config_defaults(server_module, monkeypatch):
    fake = FakeProjectManager(
        {
            "GetCurrentProject": {"filename": "/w/proj.ewp"},
            "GetCurrentConfiguration": {"name": "Debug", "toolchainId": "ARM"},
        }
    )
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    ctx, cfg = server_module._resolve_project_and_config("", "")
    assert ctx == {"filename": "/w/proj.ewp"}
    assert cfg == "Debug"
    assert fake.methods_called() == ["GetCurrentProject", "GetCurrentConfiguration"]


def test_resolve_project_and_config_no_current_project_raises(server_module, monkeypatch):
    fake = FakeProjectManager({"GetCurrentProject": {"filename": ""}})
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    with pytest.raises(server_module.ThriftBridgeError):
        server_module._resolve_project_and_config("", "")


def test_project_build_success_and_failure_envelopes(server_module, monkeypatch):
    result = {"succeded": True, "buildOutput": [f"line{i}" for i in range(10)]}
    fake = FakeProjectManager({"BuildProject": result})
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    out = server_module.project_build("/tmp/p.ewp", "Debug", max_output_lines=3)
    assert out["ok"] is True
    assert out["data"]["succeeded"] is True
    assert out["data"]["output_lines_total"] == 10
    assert out["data"]["output_tail"] == ["line7", "line8", "line9"]

    method, args, _ = fake.calls[0]
    assert method == "BuildProject"
    assert args == ({"filename": "/tmp/p.ewp"}, "Debug", 4)

    fake.responses["BuildProject"] = {"succeded": False, "buildOutput": ["error: boom"]}
    out = server_module.project_build("/tmp/p.ewp", "Debug")
    assert out["ok"] is False
    assert out["error"]["code"] == "BUILD_FAILED"
    assert out["data"]["output_tail"] == ["error: boom"]


def test_project_get_launch_config(server_module, monkeypatch):
    launch = {"program": "/w/Debug/Exe/a.out", "driverNameOrEmpty": "Simulator"}
    fake = FakeProjectManager({"GetLaunchConfigurationForConfiguration": launch})
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    out = server_module.project_get_launch_config("/w/p.ewp", "Debug")
    assert out["ok"] is True
    assert out["data"]["launch_configuration"] == launch
    method, args, _ = fake.calls[0]
    assert method == "GetLaunchConfigurationForConfiguration"
    assert args == ({"filename": "/w/p.ewp"}, "Debug")


def test_project_configure_and_start_debug_happy_path(server_module, monkeypatch):
    launch = {"program": "/w/Debug/Exe/a.out"}
    fake = FakeProjectManager(
        {
            "BuildProject": {"succeded": True, "buildOutput": ["ok"]},
            "GetLaunchConfigurationForConfiguration": launch,
        }
    )
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    debugger_calls = []
    monkeypatch.setattr(
        server_module,
        "_call_debugger",
        lambda method, *args, **kwargs: debugger_calls.append((method, args)),
    )

    server_module._set_session_state(configured=False, started=False)
    out = server_module.project_configure_and_start_debug("/w/p.ewp", "Debug")

    assert out["ok"] is True
    assert out["data"]["build"]["succeeded"] is True
    assert out["data"]["configured"] is True
    assert out["data"]["started"] is True
    assert debugger_calls == [("configureSession", (launch,)), ("startSMPSession", ())]
    assert server_module._session_state() == {"configured": True, "started": True}


def test_project_configure_and_start_debug_build_failure_short_circuits(
    server_module, monkeypatch
):
    fake = FakeProjectManager(
        {"BuildProject": {"succeded": False, "buildOutput": ["error: nope"]}}
    )
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    def _fail(*args, **kwargs):
        raise AssertionError("debugger must not be touched when the build fails")

    monkeypatch.setattr(server_module, "_call_debugger", _fail)
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", _fail)

    out = server_module.project_configure_and_start_debug("/w/p.ewp", "Debug")
    assert out["ok"] is False
    assert out["error"]["code"] == "BUILD_FAILED"
    assert "GetLaunchConfigurationForConfiguration" not in fake.methods_called()


def test_project_configure_and_start_debug_skip_build_and_start(server_module, monkeypatch):
    launch = {"program": "/w/Debug/Exe/a.out"}
    fake = FakeProjectManager({"GetLaunchConfigurationForConfiguration": launch})
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)
    monkeypatch.setattr(server_module, "_ensure_debug_eventhandler", lambda: {"ok": True})
    monkeypatch.setattr(server_module, "_ensure_libsupport", lambda: {"ok": True})

    debugger_calls = []
    monkeypatch.setattr(
        server_module,
        "_call_debugger",
        lambda method, *args, **kwargs: debugger_calls.append((method, args)),
    )

    out = server_module.project_configure_and_start_debug(
        "/w/p.ewp", "Debug", build_first=False, start_session=False
    )
    assert out["ok"] is True
    assert "build" not in out["data"]
    assert out["data"]["started"] is False
    assert debugger_calls == [("configureSession", (launch,))]
    assert server_module._session_state() == {"configured": True, "started": False}


def test_project_load_workspace_dispatches_on_extension(server_module, monkeypatch):
    fake = FakeProjectManager({"LoadEwwFile": None, "LoadEwpFile": None})
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)
    monkeypatch.setattr(
        server_module, "project_status", lambda: {"ok": True, "data": {}}
    )

    server_module.project_load_workspace("/w/space.eww")
    server_module.project_load_workspace("/w/proj.EWP", fetch_dependency_data=False)

    assert fake.calls == [
        ("LoadEwwFile", ("/w/space.eww", True), {}),
        ("LoadEwpFile", ("/w/proj.EWP", False), {}),
    ]


def test_project_status_collects_configurations(server_module, monkeypatch):
    fake = FakeProjectManager(
        {
            "HasWorkspace": True,
            "GetProjects": [{"filename": "/w/a.ewp"}],
            "GetCurrentProject": {"filename": "/w/a.ewp"},
            "GetConfigurations": [{"name": "Debug"}, {"name": "Release"}],
            "GetCurrentConfiguration": {"name": "Debug"},
        }
    )
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    out = server_module.project_status()
    assert out["ok"] is True
    data = out["data"]
    assert data["has_workspace"] is True
    assert data["current_project"] == {"filename": "/w/a.ewp"}
    assert data["projects"][0]["configurations"] == [{"name": "Debug"}, {"name": "Release"}]
    assert data["projects"][0]["current_configuration"] == {"name": "Debug"}


def test_project_get_files_maps_collection_enum(server_module, monkeypatch):
    fake = FakeProjectManager({"GetFiles": ["/w/main.c", "/w/util.c"]})
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)
    monkeypatch.setattr(
        server_module,
        "_projectmanager_module",
        lambda: SimpleNamespace(
            FileCollectionType=SimpleNamespace(ProjFiles=0, WsFiles=3)
        ),
    )

    out = server_module.project_get_files("/w/p.ewp", "Debug", collection="WsFiles")
    assert out["ok"] is True
    assert out["data"]["file_count"] == 2
    method, args, _ = fake.calls[0]
    assert method == "GetFiles"
    assert args == ({"filename": "/w/p.ewp"}, "Debug", 3)

    with pytest.raises(server_module.ThriftBridgeError):
        server_module.project_get_files("/w/p.ewp", "Debug", collection="Bogus")


def test_projectmanager_call_arg_forms(server_module, monkeypatch):
    fake = FakeProjectManager(
        {
            "GetConfigurations": [{"name": "Debug"}],
            "SetCurrentConfiguration": None,
            "HasWorkspace": True,
        }
    )
    monkeypatch.setattr(server_module, "_call_projectmanager", fake)

    result = server_module.projectmanager_call(
        "GetConfigurations", json.dumps([{"filename": "/w/p.ewp"}])
    )
    assert result == [{"name": "Debug"}]

    server_module.projectmanager_call(
        "SetCurrentConfiguration",
        json.dumps({"project": {"filename": "/w/p.ewp"}, "configurationName": "Debug"}),
    )
    assert fake.calls[1] == (
        "SetCurrentConfiguration",
        (),
        {"project": {"filename": "/w/p.ewp"}, "configurationName": "Debug"},
    )

    with pytest.raises(server_module.ThriftBridgeError):
        server_module.projectmanager_call("HasWorkspace", "{not json")
