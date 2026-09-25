"""project_*, options_* and ide_services_* tools."""

from __future__ import annotations

import json

import pytest

from iar_cspy import CSpyError


def ok(**fields):
    return {"success": {"value": True, "failureMessage": ""}, **fields}


def test_project_build_success_and_failure_envelopes(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "BuildProject", {"succeded": True, "buildOutput": [f"line{i}" for i in range(10)]})

    out = server.project_build("/tmp/p.ewp", "Debug", max_output_lines=3)
    assert out["ok"] is True
    assert out["data"]["succeeded"] is True
    assert out["data"]["output_lines_total"] == 10
    assert out["data"]["output_tail"] == ["line7", "line8", "line9"]
    assert rpc.args("projectmanager", "BuildProject") == [({"filename": "/tmp/p.ewp"}, "Debug", 4)]

    rpc.on("projectmanager", "BuildProject", {"succeded": False, "buildOutput": ["error: boom"]})
    out = server.project_build("/tmp/p.ewp", "Debug")
    assert out["ok"] is False
    assert out["error"]["code"] == "BUILD_FAILED"
    assert out["error"]["message"] == "Build of 'Debug' in '/tmp/p.ewp' failed; see data.output_tail."
    assert out["data"]["output_tail"] == ["error: boom"]


def test_project_defaults_to_current_project(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "GetCurrentProject", {"filename": "/w/proj.ewp"})
    rpc.on("projectmanager", "GetCurrentConfiguration", {"name": "Debug"})
    rpc.on("projectmanager", "BuildProject", {"succeded": True, "buildOutput": []})
    out = server.project_build()
    assert (out["data"]["project"], out["data"]["configuration"]) == ({"filename": "/w/proj.ewp"}, "Debug")


def test_project_without_current_project_points_at_load_workspace(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "GetCurrentProject", {"filename": ""})
    with pytest.raises(CSpyError, match="project_load_workspace"):
        server.project_build()


def test_project_get_launch_config(server, fake):
    _, rpc = fake
    launch = {"program": "/w/Debug/Exe/a.out", "driverNameOrEmpty": "Simulator"}
    rpc.on("projectmanager", "GetLaunchConfigurationForConfiguration", launch)
    out = server.project_get_launch_config("/w/p.ewp", "Debug")
    assert out["ok"] is True
    assert out["data"]["launch_configuration"] == launch
    assert rpc.args("projectmanager", "GetLaunchConfigurationForConfiguration") == [({"filename": "/w/p.ewp"}, "Debug")]


def test_project_configure_and_start_debug(server, fake):
    client, rpc = fake
    client.debugger.set_state(configured=False, started=False)
    launch = {"program": "/w/Debug/Exe/a.out"}
    rpc.on("projectmanager", "BuildProject", {"succeded": True, "buildOutput": ["ok"]})
    rpc.on("projectmanager", "GetLaunchConfigurationForConfiguration", launch)
    rpc.on("debugger", "configureSession").on("debugger", "startSMPSession")

    out = server.project_configure_and_start_debug("/w/p.ewp", "Debug")
    assert out["ok"] is True
    assert out["data"]["build"]["succeeded"] is True
    assert (out["data"]["configured"], out["data"]["started"]) == (True, True)
    assert out["data"]["launch_configuration"] == launch
    assert rpc.methods("debugger") == ["configureSession", "startSMPSession"]
    assert client.debugger.state == {"configured": True, "started": True}


def test_project_configure_and_start_debug_build_failure_short_circuits(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "BuildProject", {"succeded": False, "buildOutput": ["error: nope"]})
    out = server.project_configure_and_start_debug("/w/p.ewp", "Debug")
    assert out["ok"] is False
    assert out["error"]["code"] == "BUILD_FAILED"
    assert "session was not configured" in out["error"]["message"]
    assert "launch_configuration" not in out["data"]
    assert rpc.methods() == ["BuildProject"]


def test_project_configure_and_start_debug_skip_build_and_start(server, fake):
    client, rpc = fake
    client.debugger.set_state(configured=False, started=False)
    rpc.on("projectmanager", "GetLaunchConfigurationForConfiguration", {"program": "/w/a.out"})
    rpc.on("debugger", "configureSession")
    out = server.project_configure_and_start_debug("/w/p.ewp", "Debug", build_first=False, start_session=False)
    assert out["ok"] is True
    assert "build" not in out["data"]
    assert out["data"]["started"] is False
    assert client.debugger.state == {"configured": True, "started": False}


def test_project_load_workspace_and_status(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "LoadEwwFile")
    rpc.on("projectmanager", "HasWorkspace", True)
    rpc.on("projectmanager", "GetProjects", [{"filename": "/w/a.ewp"}])
    rpc.on("projectmanager", "GetCurrentProject", {"filename": "/w/a.ewp"})
    rpc.on("projectmanager", "GetConfigurations", [{"name": "Debug"}, {"name": "Release"}])
    rpc.on("projectmanager", "GetCurrentConfiguration", {"name": "Debug"})

    out = server.project_load_workspace("/w/space.eww")
    assert (out["ok"], out["tool"]) == (True, "project_status")
    data = out["data"]
    assert data["has_workspace"] is True
    assert data["current_project"] == {"filename": "/w/a.ewp"}
    assert data["projects"][0]["configurations"] == [{"name": "Debug"}, {"name": "Release"}]
    assert data["projects"][0]["current_configuration"] == {"name": "Debug"}
    assert rpc.args("projectmanager", "LoadEwwFile") == [("/w/space.eww", True)]


def test_project_get_files_maps_collection_enum(server, fake):
    client, rpc = fake
    rpc.on("projectmanager", "GetFiles", ["/w/main.c", "/w/util.c"])
    out = server.project_get_files("/w/p.ewp", "Debug", collection="WsFiles")
    assert out["data"]["file_count"] == 2
    ws_files = client.idl.module("projectmanager.thrift").FileCollectionType.WsFiles
    assert rpc.args("projectmanager", "GetFiles") == [({"filename": "/w/p.ewp"}, "Debug", ws_files)]
    with pytest.raises(CSpyError, match="Unknown collection"):
        server.project_get_files("/w/p.ewp", "Debug", collection="Bogus")


def test_projectmanager_call_arg_forms(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "GetConfigurations", [{"name": "Debug"}])
    rpc.on("projectmanager", "SetCurrentConfiguration")
    assert server.projectmanager_call("GetConfigurations", json.dumps([{"filename": "/w/p.ewp"}])) == [
        {"name": "Debug"}
    ]
    server.projectmanager_call(
        "SetCurrentConfiguration", json.dumps({"project": {"filename": "/w/p.ewp"}, "configurationName": "Debug"})
    )
    assert rpc.calls[1][3] == {"project": {"filename": "/w/p.ewp"}, "configurationName": "Debug"}
    with pytest.raises(CSpyError, match="Invalid JSON"):
        server.projectmanager_call("HasWorkspace", "{not json")


# -- options ------------------------------------------------------------------


def test_options_create_session(server, fake):
    _, rpc = fake
    rpc.on("projectmanager", "GetCurrentProject", {"filename": "/w/proj.ewp"})
    rpc.on("projectmanager", "GetCurrentConfiguration", {"name": "Debug"})
    rpc.on("options", "CreateSession", ok(sessionId={"value": "0", "type": "OptionsService"}, readOnly=False))

    data = server.options_create_session()["data"]
    assert (data["session_id"], data["configuration"], data["read_only"]) == ("0", "Debug", False)
    request = rpc.args("options", "CreateSession")[0][0]
    assert (request["projectPath"], request["showHiddenOptions"]) == ("/w/proj.ewp", False)


def test_options_create_session_hints_at_loading_the_project(server, fake):
    _, rpc = fake
    rpc.on("options", "CreateSession", {"success": {"value": False, "failureMessage": "Project not found: /w/p.ewp"}})
    with pytest.raises(CSpyError) as excinfo:
        server.options_create_session("/w/p.ewp", "Debug")
    assert 'project_load_workspace("/w/p.ewp")' in str(excinfo.value)
    assert "client.project" not in str(excinfo.value)


def test_options_create_session_other_failures_pass_through(server, fake):
    _, rpc = fake
    rpc.on("options", "CreateSession", {"success": {"value": False, "failureMessage": "no such configuration"}})
    with pytest.raises(CSpyError) as excinfo:
        server.options_create_session("/w/p.ewp", "Nope")
    assert "no such configuration" in str(excinfo.value)
    assert "project_load_workspace" not in str(excinfo.value)


def test_options_trees_update_and_commit(server, fake):
    _, rpc = fake
    rpc.on("options", "GetCategoryTree", ok(tree={"id": {"value": "Category", "type": "OptionsService"}, "data": "<pages/>"}))
    rpc.on("options", "GetOptionTree", ok(tree={"data": "<options/>"}))
    errors = [{"optionDefinitionId": "CCOptLevel", "errorMessage": "out of range"}]
    rpc.on("options", "UpdateOptionsState", ok(tree={"data": "<options/>"}, verificationErrors=errors))
    rpc.on("options", "CommitOptionState", ok()).on("options", "DestroySession", ok())

    assert server.options_get_category_tree("0")["data"] == {"session_id": "0", "tree_id": "Category", "tree_xml": "<pages/>"}
    assert server.options_get_option_tree("0", "General")["data"]["tree_id"] == "General"

    out = server.options_update_state(
        "3",
        "General-GEN-TARGET",
        updated_json='[{"optionDefinitionId": "u"}]',
        created_json='[{"optionDefinitionId": "c"}]',
        deleted_json='[{"optionDefinitionId": "d"}]',
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "OPTION_VERIFICATION_FAILED"
    assert out["data"]["verification_errors"] == errors
    request = rpc.args("options", "UpdateOptionsState")[0][0]
    assert request["sessionId"] == {"value": "3", "type": "OptionsService"}
    assert request["treeId"] == {"value": "General-GEN-TARGET", "type": "OptionsService"}
    assert request["updatedOptionValues"] == [{"optionDefinitionId": "u"}]
    assert request["createdOptionValues"] == [{"optionDefinitionId": "c"}]
    assert request["deletedOptionValues"] == [{"optionDefinitionId": "d"}]

    assert server.options_commit("3")["ok"] is True
    assert server.options_destroy_session("3")["ok"] is True


def test_options_update_state_rejects_non_list_json(server, fake):
    with pytest.raises(CSpyError, match="must be a JSON list"):
        server.options_update_state("0", "General", updated_json='{"a": 1}')


def test_options_call_rejects_invalid_json(server, fake):
    with pytest.raises(CSpyError, match="Invalid JSON"):
        server.options_call("GetCategoryTree", "{not json")


# -- IDE services -------------------------------------------------------------


def test_ide_services_ensure_reports_per_service_errors(server, fake):
    client, _ = fake
    client.registry.set(["debugger"])
    out = server.ide_services_ensure()
    assert out["ok"] is False
    assert out["error"]["code"] == "IDE_SERVICE_START_FAILED"
    assert [r["service"] for r in out["data"]["results"]] == ["projectmanager", "options"]
    assert all(r["status"] == "error" for r in out["data"]["results"])


def test_ide_services_ensure_expands_dependencies(server, fake):
    client, _ = fake
    client.registry.set(["com.iar.thrift.service.projectmanager", "com.iar.optionsservice"])
    out = server.ide_services_ensure("options")
    assert out["ok"] is True
    assert out["data"]["requested"] == ["projectmanager", "options"]


def test_ide_services_ensure_rejects_unknown_service(server, fake):
    with pytest.raises(CSpyError, match="Unknown IDE service"):
        server.ide_services_ensure("bogus")


def test_ide_services_status_and_stop(server, fake):
    client, _ = fake
    client.registry.set(["com.iar.thrift.service.manager"])
    data = server.ide_services_status()["data"]
    assert data["has_service_manager"] is True
    assert data["services"]["projectmanager"]["registered"] is False
    stopped = server.ide_services_stop_launcher()
    assert stopped["ok"] is True and stopped["data"]["was_running"] is False
