"""Command line to backend Config."""

from __future__ import annotations

from pathlib import Path

from iar_cspy_mcp.cli import _parse_args, config_from_args


def config(argv, **env):
    return config_from_args(_parse_args(argv), environ=env)


def test_defaults_come_from_the_environment(tmp_path):
    cfg = config([], IAR_INSTALL_PATH=str(tmp_path), THRIFT_TIMEOUT_MS="42")
    assert (cfg.mode, cfg.iar_path, cfg.timeout_ms) == ("managed", tmp_path, 42)


def test_iar_path_selects_managed_mode_and_drops_a_stale_registry(tmp_path):
    cfg = config(
        ["--iar-path", str(tmp_path)],
        THRIFT_CSPYSERVER_MODE="standalone",
        THRIFT_REGISTRY_PORT="5000",
        THRIFT_REGISTRY_HOST="10.0.0.5",
    )
    assert (cfg.mode, cfg.iar_path, cfg.registry_port, cfg.registry_host) == ("managed", tmp_path, None, None)


def test_explicit_programs_override_the_installation(tmp_path):
    cfg = config(
        ["--iar-path", str(tmp_path), "--cspyserver2", "/x/CSpyServer2", "--cspyserver2-args", "-a -b",
         "--service-launcher", "/x/IarServiceLauncher", "--ide-services", "options"]
    )
    assert cfg.cspy_executable == Path("/x/CSpyServer2")
    assert cfg.cspy_args == ("-a", "-b")
    assert cfg.launcher_executable == Path("/x/IarServiceLauncher")
    assert cfg.ide_services == ("options",)


def test_no_ide_services_drops_the_launcher(tmp_path):
    bin_dir = tmp_path / "common" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "IarServiceLauncher").write_text("")
    assert config(["--iar-path", str(tmp_path)]).launcher_executable is not None
    assert config(["--iar-path", str(tmp_path), "--no-ide-services"]).launcher_executable is None


def test_a_registry_selects_standalone_mode():
    cfg = config(["--registry-port", "5000", "--registry-host", "10.0.0.5", "--registry-service", "dbg"])
    assert (cfg.mode, cfg.registry_host, cfg.registry_port, cfg.registry_service_name) == (
        "standalone", "10.0.0.5", 5000, "dbg",
    )
