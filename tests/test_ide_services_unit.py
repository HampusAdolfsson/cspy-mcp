from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from mcp_thrift_server import cspy_server_manager, service_launcher
from mcp_thrift_server.config import load_config


def _config(monkeypatch, **env):
    for key in list(os.environ):
        if key.startswith("THRIFT_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_config()


def _fake_stage(tmp_path, *, libraries=("ProjectManagerHandler",), manifests=()):
    """Create a stage-like bin dir with service libraries and stock manifests."""
    bin_dir = tmp_path / "stage" / "common" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "IarServiceLauncher").write_text("#!/bin/sh\n")
    (bin_dir / "CSpyServer2").write_text("#!/bin/sh\n")
    for name in libraries:
        (bin_dir / f"lib{name}.so").write_bytes(b"")
    for name in manifests:
        (bin_dir / name).write_text("{}")
    return bin_dir


# --------------------------------------------------------------------------
# Service selection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ["projectmanager", "options"]),
        (None, ["projectmanager", "options"]),
        ([], ["projectmanager", "options"]),
        # options depends on projectmanager, which is pulled in ahead of it.
        ("options", ["projectmanager", "options"]),
        ("OPTIONS, projectmanager", ["projectmanager", "options"]),
        ("options;options", ["projectmanager", "options"]),
        (["projectmanager"], ["projectmanager"]),
    ],
)
def test_normalize_service_keys(raw, expected):
    assert service_launcher.normalize_service_keys(raw) == expected


def test_options_depends_on_projectmanager():
    # OptionsServiceHandler reaches the project manager by linkage, so hosting
    # options without projectmanager crashes the host process on CreateSession.
    assert service_launcher.service_dependencies("options") == ("projectmanager",)
    assert service_launcher.service_dependencies("projectmanager") == ()


def test_normalize_service_keys_detects_dependency_cycles(monkeypatch):
    monkeypatch.setitem(
        service_launcher.IDE_SERVICES["projectmanager"], "depends_on", ("options",)
    )
    with pytest.raises(RuntimeError, match="Circular IDE service dependency"):
        service_launcher.normalize_service_keys("options")


def test_normalize_service_keys_rejects_unknown():
    with pytest.raises(RuntimeError, match="Unknown IDE service"):
        service_launcher.normalize_service_keys("nosuchservice")


def test_registry_names_match_idl_constants():
    # These are the SERVICE_ID/PROJECTMANAGER_ID constants the services
    # register themselves under, not the manifest 'name' fields.
    assert (
        service_launcher.IDE_SERVICES["projectmanager"]["registry_name"]
        == "com.iar.thrift.service.projectmanager"
    )
    assert service_launcher.IDE_SERVICES["options"]["registry_name"] == "com.iar.optionsservice"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_defaults_for_ide_services(monkeypatch):
    cfg = _config(monkeypatch)
    assert cfg.cspy_mode == "managed"
    assert cfg.launcher_executable is None
    assert cfg.service_bin_dir is None
    assert cfg.ide_services == []
    assert cfg.auto_ide_services is True


def test_config_from_stage(monkeypatch, tmp_path):
    # One path for the whole toolchain; the programs come from common/bin.
    bin_dir = _fake_stage(tmp_path)
    stage = bin_dir.parent.parent
    cfg = _config(
        monkeypatch,
        IAR_STAGE=str(stage),
        THRIFT_IDE_SERVICES="options, projectmanager",
        THRIFT_AUTO_IDE_SERVICES="0",
    )
    assert cfg.cspy_mode == "managed"
    assert cfg.iar_stage == stage
    assert cfg.service_bin_dir == bin_dir
    assert cfg.launcher_executable == bin_dir / "IarServiceLauncher"
    assert cfg.cspy_executable == bin_dir / "CSpyServer2"
    assert cfg.ide_services == ["options", "projectmanager"]
    assert cfg.auto_ide_services is False


def test_config_explicit_paths_override_the_stage(monkeypatch, tmp_path):
    bin_dir = _fake_stage(tmp_path)
    stage = bin_dir.parent.parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg = _config(
        monkeypatch,
        IAR_STAGE=str(stage),
        THRIFT_CSPYSERVER_EXE=str(elsewhere / "CSpyServer2"),
        THRIFT_SERVICE_LAUNCHER_EXE=str(elsewhere / "IarServiceLauncher"),
    )
    assert cfg.cspy_executable == elsewhere / "CSpyServer2"
    assert cfg.launcher_executable == elsewhere / "IarServiceLauncher"


def test_config_explicit_missing_path_is_not_silently_dropped(monkeypatch, tmp_path):
    # A stage that does not ship a program yields None so the bridge can do
    # without it, but a path given by hand must still surface as an error later.
    cfg = _config(monkeypatch, THRIFT_CSPYSERVER_EXE=str(tmp_path / "nope" / "CSpyServer2"))
    assert cfg.cspy_executable == tmp_path / "nope" / "CSpyServer2"


def test_config_stage_without_a_launcher(monkeypatch, tmp_path):
    # A compiler-only toolchain has no IarServiceLauncher; managed mode then
    # just runs CSpyServer2 on its own, as it always did.
    bin_dir = tmp_path / "stage" / "common" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "CSpyServer2").write_text("#!/bin/sh\n")
    cfg = _config(monkeypatch, IAR_STAGE=str(tmp_path / "stage"))
    assert cfg.cspy_executable == bin_dir / "CSpyServer2"
    assert cfg.launcher_executable is None


def test_config_host_ide_services_opt_out(monkeypatch, tmp_path):
    bin_dir = _fake_stage(tmp_path)
    cfg = _config(
        monkeypatch,
        IAR_STAGE=str(bin_dir.parent.parent),
        THRIFT_HOST_IDE_SERVICES="0",
    )
    assert cfg.host_ide_services is False
    assert cfg.launcher_executable is None
    assert cfg.cspy_executable == bin_dir / "CSpyServer2"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "managed"),
        ("managed", "managed"),
        ("standalone", "standalone"),
        # Names this setting used before the modes were reduced to two.
        ("external", "standalone"),
        ("launcher", "managed"),
        ("STANDALONE", "standalone"),
        ("nonsense", "managed"),
    ],
)
def test_config_mode_names_and_aliases(monkeypatch, raw, expected):
    env = {"THRIFT_CSPYSERVER_MODE": raw} if raw is not None else {}
    assert _config(monkeypatch, **env).cspy_mode == expected


# --------------------------------------------------------------------------
# Locating the service libraries
# --------------------------------------------------------------------------


def test_service_bin_dir_prefers_explicit_setting(monkeypatch, tmp_path):
    bin_dir = _fake_stage(tmp_path)
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    cfg = _config(
        monkeypatch,
        THRIFT_SERVICE_LAUNCHER_EXE=str(bin_dir / "IarServiceLauncher"),
        THRIFT_SERVICE_BIN_DIR=str(explicit),
    )
    assert service_launcher.service_bin_dir(cfg) == explicit


def test_service_bin_dir_falls_back_to_launcher_then_cspyserver(monkeypatch, tmp_path):
    bin_dir = _fake_stage(tmp_path)
    cfg = _config(monkeypatch, THRIFT_SERVICE_LAUNCHER_EXE=str(bin_dir / "IarServiceLauncher"))
    assert service_launcher.service_bin_dir(cfg) == bin_dir

    cfg = _config(monkeypatch, THRIFT_CSPYSERVER_EXE=str(bin_dir / "CSpyServer2"))
    assert service_launcher.service_bin_dir(cfg) == bin_dir


def test_service_bin_dir_without_any_hint_raises(monkeypatch):
    cfg = _config(monkeypatch)
    with pytest.raises(RuntimeError, match="--iar-stage"):
        service_launcher.service_bin_dir(cfg)


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


def test_resolve_manifest_prefers_stock_manifest(monkeypatch, tmp_path):
    bin_dir = _fake_stage(tmp_path, manifests=("projectmanager.json",))
    cfg = _config(monkeypatch, THRIFT_SERVICE_LAUNCHER_EXE=str(bin_dir / "IarServiceLauncher"))
    assert service_launcher.resolve_manifest(cfg, "projectmanager") == bin_dir / "projectmanager.json"


def test_resolve_manifest_generates_one_with_relative_library(monkeypatch, tmp_path):
    # OptionsService ships libOptionsService.so but no OptionsService.json,
    # so the manifest has to be generated.
    bin_dir = _fake_stage(tmp_path, libraries=("OptionsService",))
    cfg = _config(monkeypatch, THRIFT_SERVICE_LAUNCHER_EXE=str(bin_dir / "IarServiceLauncher"))

    manifest = service_launcher.resolve_manifest(cfg, "options")
    assert manifest.parent != bin_dir

    entry = json.loads(manifest.read_text())["services"][0]
    assert entry["name"] == "com.iar.OptionsService"
    assert entry["startupEntryPoint"] == "OptionsServiceStart"
    assert entry["shutdownEntryPoint"] == "OptionsServiceStop"
    assert entry["spawnNewProcess"] is False
    assert entry["registerInLauncher"] is False

    # The ServiceManager prepends the manifest's own directory to libraryName
    # unconditionally, so an absolute path here would resolve to nonsense.
    assert not os.path.isabs(entry["libraryName"])
    resolved = (manifest.parent / entry["libraryName"]).resolve()
    assert resolved == (bin_dir / "OptionsService").resolve()
    assert resolved.with_name("lib" + resolved.name + ".so").exists()


def test_resolve_manifest_without_library_or_manifest_raises(monkeypatch, tmp_path):
    bin_dir = _fake_stage(tmp_path, libraries=())
    cfg = _config(monkeypatch, THRIFT_SERVICE_LAUNCHER_EXE=str(bin_dir / "IarServiceLauncher"))
    with pytest.raises(RuntimeError, match="neither the manifest"):
        service_launcher.resolve_manifest(cfg, "options")


def test_render_manifest_is_relative_from_a_sibling_directory(tmp_path):
    bin_dir = tmp_path / "a" / "common" / "bin"
    manifest_dir = tmp_path / "b"
    bin_dir.mkdir(parents=True)
    manifest_dir.mkdir()

    rendered = json.loads(
        service_launcher._render_manifest(
            service_launcher.IDE_SERVICES["projectmanager"], bin_dir, manifest_dir
        )
    )
    library = rendered["services"][0]["libraryName"]
    assert library.startswith("..")
    assert (manifest_dir / library).resolve() == (bin_dir / "ProjectManagerHandler").resolve()


# --------------------------------------------------------------------------
# CSpyServer2 arguments
# --------------------------------------------------------------------------


def test_cspy_args_default_to_standalone(monkeypatch):
    cfg = _config(monkeypatch)
    assert cspy_server_manager._cspy_args(cfg, None) == ["-standalone", "-sockets"]


def test_cspy_args_join_launcher_registry(monkeypatch):
    # In launcher mode CSpyServer2 must attach to the launcher's registry
    # instead of starting a second one.
    cfg = _config(monkeypatch, THRIFT_CSPYSERVER_MODE="launcher")
    assert cspy_server_manager._cspy_args(cfg, 4711) == ["-sockets", "-registry", "4711"]


def test_cspy_args_join_overrides_configured_args(monkeypatch):
    cfg = _config(
        monkeypatch,
        THRIFT_CSPYSERVER_MODE="launcher",
        THRIFT_CSPYSERVER_ARGS="-standalone -sockets",
    )
    assert "-standalone" not in cspy_server_manager._cspy_args(cfg, 5)


def test_ensure_launcher_registry_requires_managed_mode(monkeypatch):
    cfg = _config(monkeypatch, THRIFT_CSPYSERVER_MODE="standalone")
    with pytest.raises(RuntimeError, match="managed"):
        service_launcher.ensure_launcher_registry(cfg)


def test_resolve_launcher_executable_error_points_at_the_stage_option(monkeypatch):
    cfg = _config(monkeypatch)
    with pytest.raises(RuntimeError, match="--iar-stage"):
        service_launcher._resolve_launcher_executable(cfg)


def test_resolve_launcher_executable_missing_file(monkeypatch, tmp_path):
    cfg = _config(monkeypatch)
    cfg = replace(cfg, launcher_executable=tmp_path / "nope" / "IarServiceLauncher")
    with pytest.raises(RuntimeError, match="not found"):
        service_launcher._resolve_launcher_executable(cfg)


def test_registry_banner_pattern_matches_launcher_output():
    line = "Service registry running on local socket on port: 41821"
    assert service_launcher._PORT_PATTERN.search(line).group(1) == "41821"


# --------------------------------------------------------------------------
# _ensure_ide_service
# --------------------------------------------------------------------------


@pytest.fixture()
def ide_env(server_module, monkeypatch, tmp_path):
    """server_module wired to a fake registry, with the ensure-cache cleared."""
    bin_dir = _fake_stage(
        tmp_path, libraries=("ProjectManagerHandler", "OptionsService"), manifests=("projectmanager.json",)
    )
    monkeypatch.setenv("THRIFT_CSPYSERVER_MODE", "standalone")
    monkeypatch.setenv("THRIFT_REGISTRY_HOST", "127.0.0.1")
    monkeypatch.setenv("THRIFT_REGISTRY_PORT", "4711")
    monkeypatch.setenv("THRIFT_SERVICE_LAUNCHER_EXE", str(bin_dir / "IarServiceLauncher"))
    server_module._IDE_SERVICES_PRESENT.clear()

    loaded: list[str] = []
    monkeypatch.setattr(server_module, "_load_service_manifest", lambda cfg, m: loaded.append(str(m)))
    yield server_module, bin_dir, loaded
    server_module._IDE_SERVICES_PRESENT.clear()


def _registry(server_module, monkeypatch, names):
    monkeypatch.setattr(
        server_module,
        "_list_registry_services",
        lambda name_filter="": [{"name": n} for n in names],
    )


def test_ensure_ide_service_reports_already_registered(ide_env, monkeypatch):
    server_module, _bin_dir, loaded = ide_env
    _registry(server_module, monkeypatch, ["com.iar.thrift.service.projectmanager"])

    result = server_module._ensure_ide_service("projectmanager")
    assert result["status"] == "already-registered"
    assert loaded == []


def test_ensure_ide_service_caches_presence(ide_env, monkeypatch):
    server_module, _bin_dir, _loaded = ide_env
    _registry(server_module, monkeypatch, ["com.iar.thrift.service.projectmanager"])

    assert server_module._ensure_ide_service("projectmanager")["status"] == "already-registered"
    assert server_module._ensure_ide_service("projectmanager")["status"] == "cached"
    # force= re-queries the registry rather than trusting the cache.
    assert server_module._ensure_ide_service("projectmanager", force=True)["status"] == (
        "already-registered"
    )


def test_ensure_ide_service_starts_missing_service(ide_env, monkeypatch):
    server_module, bin_dir, loaded = ide_env
    _registry(server_module, monkeypatch, ["debugger", "com.iar.thrift.service.manager"])

    result = server_module._ensure_ide_service("projectmanager")
    assert result["status"] == "started"
    assert result["manifest"] == str(bin_dir / "projectmanager.json")
    assert loaded == [str(bin_dir / "projectmanager.json")]


def test_ensure_ide_service_starts_dependency_first(ide_env, monkeypatch):
    server_module, bin_dir, loaded = ide_env
    _registry(server_module, monkeypatch, ["com.iar.thrift.service.manager"])

    result = server_module._ensure_ide_service("options")
    assert result["status"] == "started"
    assert [d["service"] for d in result["dependencies"]] == ["projectmanager"]
    # ProjectManager's manifest is loaded before the OptionsService one.
    assert len(loaded) == 2
    assert loaded[0] == str(bin_dir / "projectmanager.json")
    assert loaded[1].endswith("OptionsService.json")


def test_ensure_ide_service_without_service_manager_explains_cspyserver_limitation(
    ide_env, monkeypatch
):
    server_module, _bin_dir, loaded = ide_env
    _registry(server_module, monkeypatch, ["debugger", "breakpoints"])

    with pytest.raises(server_module.ThriftBridgeError) as excinfo:
        server_module._ensure_ide_service("options")

    message = str(excinfo.value)
    assert "CSpyServer2 alone cannot host IDE services" in message
    assert "--service-launcher --iar-stage <stage>" in message
    # The registry snapshot is included so the caller can see what it did get.
    assert "debugger" in message
    assert loaded == []


def test_ensure_ide_service_without_registry_explains_options(ide_env, monkeypatch):
    server_module, _bin_dir, _loaded = ide_env
    monkeypatch.delenv("THRIFT_REGISTRY_PORT", raising=False)

    with pytest.raises(server_module.ThriftBridgeError, match="no service registry"):
        server_module._ensure_ide_service("options")


def test_auto_ensure_respects_opt_out(ide_env, monkeypatch):
    server_module, _bin_dir, _loaded = ide_env
    monkeypatch.setenv("THRIFT_AUTO_IDE_SERVICES", "0")

    called: list[str] = []
    monkeypatch.setattr(server_module, "_ensure_ide_service", lambda key, **kw: called.append(key))
    server_module._auto_ensure_ide_service("options")
    assert called == []

    monkeypatch.setenv("THRIFT_AUTO_IDE_SERVICES", "1")
    server_module._auto_ensure_ide_service("options")
    assert called == ["options"]


def test_ide_services_ensure_tool_reports_per_service_errors(ide_env, monkeypatch):
    server_module, _bin_dir, _loaded = ide_env
    _registry(server_module, monkeypatch, ["debugger"])

    envelope = server_module.ide_services_ensure()
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "IDE_SERVICE_START_FAILED"
    assert [r["service"] for r in envelope["data"]["results"]] == ["projectmanager", "options"]
    assert all(r["status"] == "error" for r in envelope["data"]["results"])


def test_ide_services_ensure_tool_expands_dependencies(ide_env, monkeypatch):
    server_module, _bin_dir, _loaded = ide_env
    _registry(server_module, monkeypatch, ["com.iar.thrift.service.manager"])

    envelope = server_module.ide_services_ensure("options")
    assert envelope["ok"] is True
    assert envelope["data"]["requested"] == ["projectmanager", "options"]


def test_ide_services_ensure_tool_rejects_unknown_service(ide_env):
    server_module, _bin_dir, _loaded = ide_env
    with pytest.raises(server_module.ThriftBridgeError, match="Unknown IDE service"):
        server_module.ide_services_ensure("bogus")


# --------------------------------------------------------------------------
# OptionsService helpers
# --------------------------------------------------------------------------


def test_options_id_defaults_to_service_type(server_module):
    assert server_module._options_id("7") == {"value": "7", "type": "OptionsService"}
    assert server_module._options_id("7", "Category")["type"] == "Category"


def test_options_check_raises_on_failure_payload(server_module):
    # OptionsService signals failure through a Success field, not an exception.
    failing = {"success": {"value": False, "failureMessage": "no such configuration"}}
    with pytest.raises(server_module.ThriftBridgeError, match="no such configuration"):
        server_module._options_check(failing, "CreateSession")


def test_options_check_passes_through_success(server_module):
    payload = {"success": {"value": True, "failureMessage": ""}, "sessionId": {"value": "0"}}
    assert server_module._options_check(payload, "CreateSession") == payload


def test_options_update_state_rejects_non_list_json(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "_call_options", lambda *a, **k: {})
    with pytest.raises(server_module.ThriftBridgeError, match="must be a JSON list"):
        server_module.options_update_state("0", "General", updated_json='{"a": 1}')


def test_options_update_state_reports_verification_errors(server_module, monkeypatch):
    errors = [{"optionDefinitionId": "CCOptLevel", "errorMessage": "out of range"}]
    monkeypatch.setattr(
        server_module,
        "_call_options",
        lambda method, request: {
            "success": {"value": True, "failureMessage": ""},
            "tree": {"data": "<options/>"},
            "verificationErrors": errors,
        },
    )

    envelope = server_module.options_update_state(
        "0", "General", updated_json='[{"optionDefinitionId": "CCOptLevel", "data": "9"}]'
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "OPTION_VERIFICATION_FAILED"
    assert envelope["data"]["verification_errors"] == errors


def test_options_update_state_sends_all_three_value_lists(server_module, monkeypatch):
    seen: dict[str, object] = {}

    def fake(method, request):
        seen["method"] = method
        seen["request"] = request
        return {"success": {"value": True, "failureMessage": ""}, "tree": {"data": ""}}

    monkeypatch.setattr(server_module, "_call_options", fake)
    server_module.options_update_state(
        "3",
        "General-GEN-TARGET",
        updated_json='[{"optionDefinitionId": "u"}]',
        created_json='[{"optionDefinitionId": "c"}]',
        deleted_json='[{"optionDefinitionId": "d"}]',
    )

    request = seen["request"]
    assert seen["method"] == "UpdateOptionsState"
    assert request["sessionId"] == {"value": "3", "type": "OptionsService"}
    assert request["treeId"] == {"value": "General-GEN-TARGET", "type": "OptionsService"}
    assert request["updatedOptionValues"] == [{"optionDefinitionId": "u"}]
    assert request["createdOptionValues"] == [{"optionDefinitionId": "c"}]
    assert request["deletedOptionValues"] == [{"optionDefinitionId": "d"}]


def test_options_create_session_resolves_current_project(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "_resolve_project_and_config",
        lambda p, c: ({"filename": "/w/proj.ewp"}, "Debug"),
    )
    seen: dict[str, object] = {}

    def fake(method, request):
        seen["request"] = request
        return {
            "success": {"value": True, "failureMessage": ""},
            "sessionId": {"value": "0", "type": "OptionsService"},
            "readOnly": False,
        }

    monkeypatch.setattr(server_module, "_call_options", fake)

    envelope = server_module.options_create_session()
    assert envelope["data"]["session_id"] == "0"
    assert envelope["data"]["configuration"] == "Debug"
    assert seen["request"]["projectPath"] == "/w/proj.ewp"
    assert seen["request"]["configurationName"] == "Debug"
    assert seen["request"]["showHiddenOptions"] is False


def test_options_create_session_hints_at_loading_the_project(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "_resolve_project_and_config",
        lambda p, c: ({"filename": "/w/proj.ewp"}, "Debug"),
    )

    def fake(method, request):
        return {
            "success": {"value": False, "failureMessage": "Project not found: /w/proj.ewp"},
        }

    monkeypatch.setattr(server_module, "_call_options", fake)
    with pytest.raises(server_module.ThriftBridgeError) as excinfo:
        server_module.options_create_session("/w/proj.ewp", "Debug")
    assert 'project_load_workspace("/w/proj.ewp")' in str(excinfo.value)


def test_options_create_session_other_failures_pass_through(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "_resolve_project_and_config",
        lambda p, c: ({"filename": "/w/proj.ewp"}, "Nope"),
    )
    monkeypatch.setattr(
        server_module,
        "_call_options",
        lambda method, request: {
            "success": {"value": False, "failureMessage": "no such configuration"}
        },
    )
    with pytest.raises(server_module.ThriftBridgeError) as excinfo:
        server_module.options_create_session("/w/proj.ewp", "Nope")
    assert "no such configuration" in str(excinfo.value)
    assert "project_load_workspace" not in str(excinfo.value)


def test_options_get_category_tree_extracts_tree(server_module, monkeypatch):
    monkeypatch.setattr(
        server_module,
        "_call_options",
        lambda method, request: {
            "success": {"value": True, "failureMessage": ""},
            "tree": {"id": {"value": "Category", "type": "OptionsService"}, "data": "<pages/>"},
        },
    )
    data = server_module.options_get_category_tree("0")["data"]
    assert data["tree_id"] == "Category"
    assert data["tree_xml"] == "<pages/>"


def test_options_call_rejects_invalid_json(server_module):
    with pytest.raises(server_module.ThriftBridgeError, match="Invalid JSON"):
        server_module.options_call("GetCategoryTree", "{not json")
