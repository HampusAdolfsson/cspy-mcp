from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ThriftConfig:
    host: str
    port: int
    timeout_ms: int
    thrift_file: Path
    include_dirs: list[str]
    registry_host: str | None
    registry_port: int | None
    registry_service_name: str
    cspy_mode: str
    iar_stage: Path | None
    cspy_executable: Path | None
    cspy_args: list[str]
    cspy_start_timeout_ms: int
    cspy_restart_on_failure: bool
    launcher_executable: Path | None
    launcher_start_timeout_ms: int
    launcher_restart_on_failure: bool
    service_bin_dir: Path | None
    ide_services: list[str]
    auto_ide_services: bool
    host_ide_services: bool


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _split_include_dirs(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(";") if part.strip()]


def _default_thrift_file() -> Path:
    # Prefer bundled IDL from repository thrift/ folder when present.
    repo_local = Path(__file__).resolve().parent.parent / "thrift" / "cspy.thrift"
    if repo_local.exists():
        return repo_local
    # Backward-compatible fallback for older repo layouts.
    repo_legacy = Path(__file__).resolve().parent.parent / "cspy.thrift"
    if repo_legacy.exists():
        return repo_legacy
    return Path("./cspy.thrift").expanduser().resolve()


def _split_args(raw: str | None) -> list[str]:
    if not raw:
        return ["-standalone", "-sockets"]
    return [p for p in shlex.split(raw, posix=False) if p]


#: ``external`` is the name this setting used before the modes were reduced to
#: two; ``launcher`` named what is now just part of managed mode.
_MODE_ALIASES = {"external": "standalone", "launcher": "managed"}


def _normalize_mode(raw: str | None) -> str:
    """Resolve THRIFT_CSPYSERVER_MODE to ``managed`` or ``standalone``.

    managed:    the bridge starts and supervises the backend itself.
    standalone: the backend is already running; connect to its registry.
    """
    mode = (raw or "managed").strip().lower()
    mode = _MODE_ALIASES.get(mode, mode)
    return mode if mode in {"managed", "standalone"} else "managed"


def _stage_bin_dir(stage: Path | None) -> Path | None:
    """Executables and service libraries live in <stage>/common/bin.

    That layout is stable across IAR products and build stages, so it is
    derived rather than configured per program.
    """
    return stage / "common" / "bin" if stage is not None else None


def _stage_executable(stage_bin: Path | None, name: str) -> Path | None:
    """Locate a program in the stage, or None when the stage does not ship it.

    Existence is checked here, and only for stage-derived paths: a
    compiler-only toolchain has no IarServiceLauncher, and the bridge should
    quietly do without rather than fail. A path given explicitly is returned
    unchecked so that a typo surfaces as an error later on.
    """
    if stage_bin is None:
        return None
    candidate = stage_bin / (f"{name}.exe" if os.name == "nt" else name)
    return candidate if candidate.exists() else None


def _split_services(raw: str | None) -> list[str]:
    """Parse THRIFT_IDE_SERVICES; empty means every known IDE service."""
    if raw is None or not raw.strip():
        return []
    return [part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()]


def load_config() -> ThriftConfig:
    host = os.getenv("THRIFT_HOST", "127.0.0.1")
    port = int(os.getenv("THRIFT_PORT", "9090"))
    timeout_ms = int(os.getenv("THRIFT_TIMEOUT_MS", "15000"))

    thrift_file_raw = os.getenv("THRIFT_FILE")
    thrift_file = (
        Path(thrift_file_raw).expanduser().resolve()
        if thrift_file_raw
        else _default_thrift_file()
    )

    include_dirs = _split_include_dirs(os.getenv("THRIFT_INCLUDE_DIRS"))
    thrift_parent = str(thrift_file.parent)
    if not include_dirs:
        include_dirs = [thrift_parent]
    elif thrift_parent not in include_dirs:
        include_dirs.append(thrift_parent)
    registry_host = os.getenv("THRIFT_REGISTRY_HOST")
    registry_port_raw = os.getenv("THRIFT_REGISTRY_PORT")
    registry_port = int(registry_port_raw) if registry_port_raw else None
    registry_service_name = os.getenv("THRIFT_REGISTRY_SERVICE", "debugger")

    cspy_mode = _normalize_mode(os.getenv("THRIFT_CSPYSERVER_MODE"))

    # One path for the whole toolchain; the individual program paths below stay
    # supported and win over it when set.
    iar_stage_raw = os.getenv("IAR_STAGE")
    iar_stage = Path(iar_stage_raw).expanduser().resolve() if iar_stage_raw else None
    stage_bin = _stage_bin_dir(iar_stage)

    cspy_executable_raw = os.getenv("THRIFT_CSPYSERVER_EXE")
    cspy_executable = (
        Path(cspy_executable_raw).expanduser().resolve()
        if cspy_executable_raw
        else _stage_executable(stage_bin, "CSpyServer2")
    )
    cspy_args = _split_args(os.getenv("THRIFT_CSPYSERVER_ARGS"))
    cspy_start_timeout_ms = int(os.getenv("THRIFT_CSPYSERVER_START_TIMEOUT_MS", "20000"))
    cspy_restart_on_failure = _env_bool("THRIFT_CSPYSERVER_RESTART_ON_FAILURE", True)

    launcher_executable_raw = os.getenv("THRIFT_SERVICE_LAUNCHER_EXE")
    launcher_executable = (
        Path(launcher_executable_raw).expanduser().resolve()
        if launcher_executable_raw
        else _stage_executable(stage_bin, "IarServiceLauncher")
    )
    # IarServiceLauncher has to dlopen/LoadLibrary the service implementation
    # (and, for ProjectManager, the whole legacy project manager behind it), so
    # it is noticeably slower to become ready than CSpyServer2.
    launcher_start_timeout_ms = int(
        os.getenv("THRIFT_SERVICE_LAUNCHER_START_TIMEOUT_MS", "40000")
    )
    launcher_restart_on_failure = _env_bool(
        "THRIFT_SERVICE_LAUNCHER_RESTART_ON_FAILURE", True
    )

    service_bin_dir_raw = os.getenv("THRIFT_SERVICE_BIN_DIR")
    service_bin_dir = (
        Path(service_bin_dir_raw).expanduser().resolve() if service_bin_dir_raw else stage_bin
    )
    ide_services = _split_services(os.getenv("THRIFT_IDE_SERVICES"))
    auto_ide_services = _env_bool("THRIFT_AUTO_IDE_SERVICES", True)
    host_ide_services = _env_bool("THRIFT_HOST_IDE_SERVICES", True)
    if not host_ide_services:
        # Debugger only: do not start an IarServiceLauncher at all.
        launcher_executable = None

    return ThriftConfig(
        host=host,
        port=port,
        timeout_ms=timeout_ms,
        thrift_file=thrift_file,
        include_dirs=include_dirs,
        registry_host=registry_host,
        registry_port=registry_port,
        registry_service_name=registry_service_name,
        cspy_mode=cspy_mode,
        iar_stage=iar_stage,
        cspy_executable=cspy_executable,
        cspy_args=cspy_args,
        cspy_start_timeout_ms=cspy_start_timeout_ms,
        cspy_restart_on_failure=cspy_restart_on_failure,
        launcher_executable=launcher_executable,
        launcher_start_timeout_ms=launcher_start_timeout_ms,
        launcher_restart_on_failure=launcher_restart_on_failure,
        service_bin_dir=service_bin_dir,
        ide_services=ide_services,
        auto_ide_services=auto_ide_services,
        host_ide_services=host_ide_services,
    )
