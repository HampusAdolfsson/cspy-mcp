"""Host IDE platform Thrift services (ProjectManager, OptionsService) via IarServiceLauncher.

CSpyServer2 hosts only the debugger-side services (`debugger`, `breakpoints`,
`debugger.contextmanager`, `debugger.memory`, `disassembly`, `sourcelookup`).
It has no service manager, so it cannot load the IDE platform's service
libraries. Those live in dynamic libraries next to it in the stage
(`libProjectManagerHandler.so`, `libOptionsService.so`) and are hosted by
`IarServiceLauncher`, which:

1. with `-standalone -sockets`, starts a service registry and writes its
   location to `CSpyServer2-ServiceRegistry.txt` in its working directory,
2. starts a `com.iar.thrift.service.manager` (ServiceManager.thrift) service,
3. loads service libraries named by JSON manifest files, either on the command
   line or at runtime via `CSpyServiceManager.startServicesFromJsonManifest`.

The interesting part is that a registry can be *shared*: `CSpyServer2 -sockets
-registry <port>` joins an existing registry instead of starting its own. So
one launcher-owned registry ends up holding the debugger services and the IDE
services together, and every service in this bridge resolves through the same
registry with no special-casing per service.

That is what `THRIFT_CSPYSERVER_MODE=launcher` sets up. `external` mode reaches
the same services when something else already hosts them (a Thrift-enabled
`iaride`, or a hand-started IarServiceLauncher).

Manifest path gotcha
--------------------
`CSpyServiceManagerHandler::GetServiceConfigsFromJsonFile` resolves each
manifest's `libraryName` against *the manifest file's own directory*, and does
so unconditionally — an absolute `libraryName` gets the manifest directory
prepended to it and becomes a nonexistent path. So a manifest that lives
somewhere other than the stage must name its library with a path relative to
itself, which is what `_render_manifest` does.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, TextIO

from .config import ThriftConfig

# IarServiceLauncher prints the same registry banner CSpyServer2 does.
_PORT_PATTERN = re.compile(r"Service registry running on local socket on port:\s*(\d+)")

SERVICE_MANAGER_SERVICE = "com.iar.thrift.service.manager"

#: Known IDE services, keyed by the short name used in
#: ``THRIFT_IDE_SERVICES``. ``registry_name`` is the name the service registers
#: itself under (the ``*_ID``/``SERVICE_ID`` constant in its IDL, *not* the
#: ``name`` field of the manifest, which is only a ServiceManager-internal id).
#: ``depends_on`` names services that must be hosted in the same process first.
IDE_SERVICES: dict[str, dict[str, Any]] = {
    "projectmanager": {
        "registry_name": "com.iar.thrift.service.projectmanager",
        "manifest": "projectmanager.json",
        "manifest_service_name": "com.iar.ProjectManager",
        "library": "ProjectManagerHandler",
        "start_entry_point": "StartProjectManager",
        "stop_entry_point": "StopProjectManager",
        "depends_on": (),
    },
    "options": {
        "registry_name": "com.iar.optionsservice",
        "manifest": "OptionsService.json",
        "manifest_service_name": "com.iar.OptionsService",
        "library": "OptionsService",
        "start_entry_point": "OptionsServiceStart",
        "stop_entry_point": "OptionsServiceStop",
        # OptionsServiceHandler reaches the project manager by linkage
        # (GetProjectManager()), not over Thrift, so it needs the
        # ProjectManager service started in the same process. Without it,
        # CreateSession takes the whole host process down rather than
        # returning a failure - so this dependency is not optional.
        "depends_on": ("projectmanager",),
    },
}


class _LauncherState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.registry_port: int | None = None
        self.log_file: TextIO | None = None
        self.log_path: Path | None = None
        self.work_dir: Path | None = None
        self.last_cmd: list[str] = []
        self.stdout_tail: deque[str] = deque(maxlen=200)
        self.last_process_diagnostics: str = ""
        # Short service key -> manifest path handed to the ServiceManager.
        self.loaded: dict[str, str] = {}


_STATE = _LauncherState()


def _is_reachable(host: str, port: int, timeout_s: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except Exception:
        return False


def _effective_registry_host(cfg: ThriftConfig) -> str:
    return cfg.registry_host or "127.0.0.1"


def service_dependencies(key: str) -> tuple[str, ...]:
    return tuple(IDE_SERVICES[key].get("depends_on", ()))


def normalize_service_keys(raw: object) -> list[str]:
    """Normalize a service selection into known keys, in startup order.

    Accepts a list of names or a comma/semicolon separated string. Empty input
    means "all known services". Dependencies are pulled in and placed before
    the services that need them, so the result can be started front to back.
    """
    if raw is None or raw == "":
        names: list[str] = list(IDE_SERVICES)
    elif isinstance(raw, str):
        names = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    else:
        names = [str(p).strip() for p in raw if str(p).strip()]

    requested: list[str] = []
    for name in names or list(IDE_SERVICES):
        key = name.lower()
        if key not in IDE_SERVICES:
            raise RuntimeError(
                f"Unknown IDE service {name!r}. Known services: {', '.join(IDE_SERVICES)}"
            )
        if key not in requested:
            requested.append(key)

    result: list[str] = []

    def add(key: str, seen: tuple[str, ...] = ()) -> None:
        if key in result:
            return
        if key in seen:
            raise RuntimeError(
                "Circular IDE service dependency: " + " -> ".join((*seen, key))
            )
        for dependency in service_dependencies(key):
            add(dependency, (*seen, key))
        result.append(key)

    for key in requested:
        add(key)
    return result


def _resolve_launcher_executable(cfg: ThriftConfig) -> Path:
    exe = cfg.launcher_executable
    if exe is None:
        raise RuntimeError(
            "Cannot host IDE services: set THRIFT_SERVICE_LAUNCHER_EXE to the "
            "IarServiceLauncher executable in the IAR stage/installation "
            "(or pass --service-launcher). It sits next to CSpyServer2 in "
            "<install>/common/bin."
        )
    if not exe.exists():
        raise RuntimeError(f"IarServiceLauncher executable not found: {exe}")
    return exe


def service_bin_dir(cfg: ThriftConfig) -> Path:
    """Directory holding the service libraries and their stock manifests.

    Defaults to the directory of whichever of IarServiceLauncher/CSpyServer2 is
    configured, since both live in ``<install>/common/bin`` alongside the
    service libraries.
    """
    if cfg.service_bin_dir is not None:
        return cfg.service_bin_dir
    for exe in (cfg.launcher_executable, cfg.cspy_executable):
        if exe is not None:
            return exe.parent
    raise RuntimeError(
        "Cannot locate the IAR service libraries: set THRIFT_SERVICE_LAUNCHER_EXE "
        "(or THRIFT_SERVICE_BIN_DIR) so the bridge knows which <install>/common/bin to use."
    )


def _render_manifest(spec: dict[str, str], bin_dir: Path, manifest_dir: Path) -> str:
    """Render a service manifest whose libraryName is relative to manifest_dir.

    The ServiceManager prepends the manifest's directory to ``libraryName``
    unconditionally, so the only portable way to point a generated manifest at
    a library elsewhere on disk is a relative path.
    """
    library = os.path.relpath(bin_dir / spec["library"], manifest_dir)
    return json.dumps(
        {
            "services": [
                {
                    "name": spec["manifest_service_name"],
                    "libraryName": library,
                    "spawnNewProcess": False,
                    "startupEntryPoint": spec["start_entry_point"],
                    "shutdownEntryPoint": spec["stop_entry_point"],
                    "registerInLauncher": False,
                }
            ]
        },
        indent=2,
    )


def resolve_manifest(cfg: ThriftConfig, key: str) -> Path:
    """Return a manifest path for one IDE service.

    Prefers the manifest shipped in the stage next to the library. Falls back to
    generating an equivalent one in a temp directory, which is needed in
    practice: OptionsService's CMakeLists has an ``install(FILES DESTINATION
    ...)`` with an empty file list, so `OptionsService.json` is absent from the
    stage even though `libOptionsService.so` is there.
    """
    spec = IDE_SERVICES[key]
    bin_dir = service_bin_dir(cfg)

    stock = bin_dir / spec["manifest"]
    if stock.exists():
        return stock

    library = bin_dir / f"lib{spec['library']}.so"
    library_dll = bin_dir / f"{spec['library']}.dll"
    if not library.exists() and not library_dll.exists():
        raise RuntimeError(
            f"Cannot host IDE service {key!r}: neither the manifest {stock} nor the "
            f"service library ({library.name} / {library_dll.name}) exists in {bin_dir}. "
            "Check THRIFT_SERVICE_BIN_DIR / THRIFT_SERVICE_LAUNCHER_EXE."
        )

    manifest_dir = Path(tempfile.mkdtemp(prefix="iar-service-manifests-"))
    manifest = manifest_dir / spec["manifest"]
    manifest.write_text(_render_manifest(spec, bin_dir, manifest_dir), encoding="utf-8")
    return manifest


def _refresh_stdout_tail_locked() -> None:
    log_path = _STATE.log_path
    if log_path is None or not log_path.exists():
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    _STATE.stdout_tail.clear()
    _STATE.stdout_tail.extend(text.splitlines()[-200:])

    matches = list(_PORT_PATTERN.finditer(text))
    if matches:
        _STATE.registry_port = int(matches[-1].group(1))


def _cleanup_process_locked() -> None:
    proc = _STATE.process
    log_file = _STATE.log_file

    if proc is not None:
        _refresh_stdout_tail_locked()
        cmd = " ".join(_STATE.last_cmd) if _STATE.last_cmd else "<unknown>"
        log_path = str(_STATE.log_path) if _STATE.log_path else "<none>"

        if proc.poll() is None:
            # SIGTERM is handled: the launcher asks its service manager to shut
            # the services down in reverse order before exiting.
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        _refresh_stdout_tail_locked()
        tail = "\n".join(list(_STATE.stdout_tail)[-40:]) or "<no output captured>"
        _STATE.last_process_diagnostics = (
            "Most recent IarServiceLauncher process snapshot. "
            f"Exit code: {proc.returncode}. Command: {cmd}. Log: {log_path}. "
            f"Recent output:\n{tail}"
        )

    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass

    _STATE.process = None
    _STATE.registry_port = None
    _STATE.log_file = None
    _STATE.log_path = None
    _STATE.work_dir = None
    _STATE.last_cmd = []
    _STATE.loaded.clear()


def _start_process_locked(cfg: ThriftConfig) -> None:
    exe = _resolve_launcher_executable(cfg)

    # A private working directory keeps the launcher's
    # CSpyServer2-ServiceRegistry.txt out of the (possibly read-only, possibly
    # shared with another backend) stage directory.
    work_dir = Path(tempfile.mkdtemp(prefix="iar-service-launcher-"))
    cmd = [str(exe), "-standalone", "-sockets"]

    tmp = tempfile.NamedTemporaryFile(
        prefix="iar-service-launcher-", suffix=".log", delete=False
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    log_file = tmp_path.open("w", encoding="utf-8", errors="replace")

    process = subprocess.Popen(
        cmd,
        cwd=str(work_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    _STATE.process = process
    _STATE.registry_port = None
    _STATE.log_file = log_file
    _STATE.log_path = tmp_path
    _STATE.work_dir = work_dir
    _STATE.last_cmd = list(cmd)
    _STATE.loaded.clear()


def _wait_for_registry_port_locked(cfg: ThriftConfig) -> int:
    deadline = time.time() + max(1.0, cfg.launcher_start_timeout_ms / 1000.0)
    host = _effective_registry_host(cfg)

    while True:
        _refresh_stdout_tail_locked()
        if _STATE.registry_port is not None:
            break

        proc = _STATE.process
        if proc is not None and proc.poll() is not None:
            _refresh_stdout_tail_locked()
            tail = "\n".join(list(_STATE.stdout_tail)[-20:]) or "<no output captured>"
            exit_code = proc.returncode
            cmd = " ".join(_STATE.last_cmd)
            log_path = str(_STATE.log_path) if _STATE.log_path else ""
            _cleanup_process_locked()
            raise RuntimeError(
                "IarServiceLauncher exited before publishing a registry port. "
                f"Exit code: {exit_code}. Command: {cmd}. Log: {log_path}. "
                f"Recent output:\n{tail}"
            )
        if time.time() >= deadline:
            _refresh_stdout_tail_locked()
            tail = "\n".join(list(_STATE.stdout_tail)[-20:]) or "<no output captured>"
            cmd = " ".join(_STATE.last_cmd)
            log_path = str(_STATE.log_path) if _STATE.log_path else ""
            raise RuntimeError(
                "Timed out waiting for the IarServiceLauncher registry port. "
                f"Command: {cmd}. Log: {log_path}. Recent output:\n{tail}"
            )
        time.sleep(0.1)

    port = _STATE.registry_port
    if port is None:
        raise RuntimeError("Internal error: registry port missing after wait")
    if not _is_reachable(host, port):
        raise RuntimeError(
            f"IarServiceLauncher reported registry port {port}, but {host}:{port} is unreachable"
        )
    return int(port)


def _is_running_and_healthy_locked(cfg: ThriftConfig) -> bool:
    proc = _STATE.process
    port = _STATE.registry_port
    if proc is None or proc.poll() is not None or port is None:
        return False
    return _is_reachable(_effective_registry_host(cfg), int(port))


def ensure_launcher_registry(cfg: ThriftConfig) -> tuple[str, int]:
    """Start (or reuse) an IarServiceLauncher-owned registry; return its endpoint."""
    if cfg.cspy_mode != "launcher":
        raise RuntimeError(
            "An IarServiceLauncher-hosted registry is only started in "
            "THRIFT_CSPYSERVER_MODE=launcher"
        )

    with _STATE.lock:
        if _is_running_and_healthy_locked(cfg):
            return _effective_registry_host(cfg), int(_STATE.registry_port)

        if _STATE.process is not None:
            _cleanup_process_locked()

        last_error: Exception | None = None
        for _ in range(2 if cfg.launcher_restart_on_failure else 1):
            try:
                _start_process_locked(cfg)
                port = _wait_for_registry_port_locked(cfg)
                return _effective_registry_host(cfg), int(port)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _cleanup_process_locked()

        raise RuntimeError(f"Failed to start IarServiceLauncher: {last_error}")


def registry_endpoint() -> tuple[str, int] | None:
    """Return the launcher-owned registry endpoint, if one is running."""
    with _STATE.lock:
        if _STATE.process is None or _STATE.process.poll() is not None:
            return None
        if _STATE.registry_port is None:
            return None
        return "127.0.0.1", int(_STATE.registry_port)


def launcher_status() -> dict[str, object]:
    with _STATE.lock:
        proc = _STATE.process
        running = proc is not None and proc.poll() is None
        if proc is not None:
            _refresh_stdout_tail_locked()
        return {
            "running": bool(running),
            "pid": int(proc.pid) if running and proc is not None else None,
            "registry_port": _STATE.registry_port,
            "work_dir": str(_STATE.work_dir) if _STATE.work_dir else None,
            "log_path": str(_STATE.log_path) if _STATE.log_path else None,
            "command": list(_STATE.last_cmd),
            "loaded_manifests": dict(_STATE.loaded),
            "recent_output": list(_STATE.stdout_tail)[-20:],
            "last_process_diagnostics": _STATE.last_process_diagnostics,
        }


def launcher_crash_diagnostics(max_lines: int = 40) -> str:
    """Best-effort IarServiceLauncher diagnostics to attach to Thrift failures."""
    with _STATE.lock:
        proc = _STATE.process
        if proc is None:
            return _STATE.last_process_diagnostics

        _refresh_stdout_tail_locked()
        tail = "\n".join(list(_STATE.stdout_tail)[-max(1, int(max_lines)):])
        tail = tail or "<no output captured>"
        cmd = " ".join(_STATE.last_cmd) if _STATE.last_cmd else "<unknown>"
        log_path = str(_STATE.log_path) if _STATE.log_path else "<none>"

        if proc.poll() is None:
            return (
                "IarServiceLauncher is still running, but a Thrift call failed. "
                f"PID: {proc.pid}. Command: {cmd}. Log: {log_path}. "
                f"Recent output:\n{tail}"
            )
        return (
            "IarServiceLauncher appears to have exited. "
            f"Exit code: {proc.returncode}. Command: {cmd}. Log: {log_path}. "
            f"Recent output:\n{tail}"
        )


def note_manifest_loaded(key: str, manifest: Path | str) -> None:
    """Record that a manifest was loaded, so repeat requests can be skipped."""
    with _STATE.lock:
        _STATE.loaded[key] = str(manifest)


def loaded_manifests() -> dict[str, str]:
    with _STATE.lock:
        return dict(_STATE.loaded)


def shutdown_service_launcher() -> None:
    with _STATE.lock:
        _cleanup_process_locked()


atexit.register(shutdown_service_launcher)
