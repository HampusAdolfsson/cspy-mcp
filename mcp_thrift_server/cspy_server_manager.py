from __future__ import annotations

import atexit
import os
import re
import socket
import subprocess
import threading
import tempfile
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from .config import ThriftConfig
from .service_launcher import ensure_launcher_registry

_PORT_PATTERN = re.compile(r"Service registry running on local socket on port:\s*(\d+)")


class _ManagedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.stdout_thread: threading.Thread | None = None
        self.registry_port: int | None = None
        self.log_file: TextIO | None = None
        self.log_path: Path | None = None
        self.last_cmd: list[str] = []
        self.registry_port_event = threading.Event()
        self.stdout_tail: deque[str] = deque(maxlen=200)
        self.last_process_diagnostics: str = ""


_STATE = _ManagedState()


def _is_reachable(host: str, port: int, timeout_s: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except Exception:
        return False


def _reader_loop(process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\r\n")
        with _STATE.lock:
            _STATE.stdout_tail.append(line)
            match = _PORT_PATTERN.search(line)
            if match:
                _STATE.registry_port = int(match.group(1))
                _STATE.registry_port_event.set()


def _cleanup_process_locked() -> None:
    proc = _STATE.process
    log_file = _STATE.log_file

    if proc is None:
        _STATE.process = None
        _STATE.stdout_thread = None
        _STATE.registry_port = None
        _STATE.log_file = None
        _STATE.log_path = None
        _STATE.last_cmd = []
        _STATE.registry_port_event.clear()
        return

    _refresh_stdout_tail_locked()
    cmd = " ".join(_STATE.last_cmd) if _STATE.last_cmd else "<unknown>"
    log_path = str(_STATE.log_path) if _STATE.log_path else "<none>"

    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    _refresh_stdout_tail_locked()
    tail = "\n".join(list(_STATE.stdout_tail)[-40:])
    if not tail:
        tail = "<no output captured>"
    _STATE.last_process_diagnostics = (
        "Most recent managed CSpyServer2 process snapshot. "
        f"Exit code: {proc.returncode}. "
        f"Command: {cmd}. "
        f"Log: {log_path}. "
        "Recent output:\n"
        f"{tail}"
    )

    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass

    _STATE.process = None
    _STATE.stdout_thread = None
    _STATE.registry_port = None
    _STATE.log_file = None
    _STATE.log_path = None
    _STATE.last_cmd = []
    _STATE.registry_port_event.clear()


def _require_managed_mode(cfg: ThriftConfig) -> None:
    if cfg.cspy_mode != "managed":
        raise RuntimeError(
            "Starting a backend is only done in THRIFT_CSPYSERVER_MODE=managed; "
            f"the current mode is {cfg.cspy_mode!r}"
        )


def _cspy_args(cfg: ThriftConfig, join_registry_port: int | None) -> list[str]:
    """Arguments for the CSpyServer2 process.

    When an IarServiceLauncher is hosting the IDE services, CSpyServer2 must
    not start a registry of its own: it joins the launcher's with
    `-registry <port>`, so the debugger services and the IDE services end up in
    a single registry. Without a launcher it starts its own, as before.
    """
    if join_registry_port is None:
        return list(cfg.cspy_args)
    return ["-sockets", "-registry", str(int(join_registry_port))]


def _effective_registry_host(cfg: ThriftConfig) -> str:
    return cfg.registry_host or "127.0.0.1"


def _resolve_executable(cfg: ThriftConfig) -> Path:
    exe = cfg.cspy_executable
    if exe is None:
        raise RuntimeError(
            "No CSpyServer2 path. Pass --iar-path <path> (or set IAR_INSTALL_PATH) and it "
            "is taken from <path>/common/bin, or point THRIFT_CSPYSERVER_EXE at it "
            "directly."
        )
    if not exe.exists():
        raise RuntimeError(f"CSpyServer2 executable not found: {exe}")
    return exe


def _refresh_stdout_tail_locked() -> None:
    log_path = _STATE.log_path
    if log_path is None or not log_path.exists():
        return

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    lines = text.splitlines()
    _STATE.stdout_tail.clear()
    _STATE.stdout_tail.extend(lines[-200:])

    matches = list(_PORT_PATTERN.finditer(text))
    if matches:
        _STATE.registry_port = int(matches[-1].group(1))
        _STATE.registry_port_event.set()


def _start_process_locked(cfg: ThriftConfig, join_registry_port: int | None = None) -> None:
    exe = _resolve_executable(cfg)
    cmd = [str(exe), *_cspy_args(cfg, join_registry_port)]

    tmp = tempfile.NamedTemporaryFile(prefix="cspyserver2-", suffix=".log", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    log_file = tmp_path.open("w", encoding="utf-8", errors="replace")

    process = subprocess.Popen(
        cmd,
        cwd=str(exe.parent),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    _STATE.process = process
    _STATE.registry_port = None
    _STATE.log_file = log_file
    _STATE.log_path = tmp_path
    _STATE.last_cmd = list(cmd)
    _STATE.registry_port_event.clear()
    _STATE.stdout_thread = None


def _wait_for_registry_port_locked(cfg: ThriftConfig) -> int:
    timeout_s = max(1.0, cfg.cspy_start_timeout_ms / 1000.0)
    deadline = time.time() + timeout_s
    host = _effective_registry_host(cfg)

    while True:
        _refresh_stdout_tail_locked()

        if _STATE.registry_port_event.wait(timeout=0.2):
            break
        proc = _STATE.process
        if proc is not None and proc.poll() is not None:
            _refresh_stdout_tail_locked()
            tail = "\n".join(list(_STATE.stdout_tail)[-20:])
            exit_code = proc.returncode
            cmd = " ".join(_STATE.last_cmd)
            log_path = str(_STATE.log_path) if _STATE.log_path else ""
            _cleanup_process_locked()
            raise RuntimeError(
                "CSpyServer2 exited before publishing registry port. "
                f"Exit code: {exit_code}. Command: {cmd}. Log: {log_path}. Recent output:\n{tail}"
            )
        if time.time() >= deadline:
            _refresh_stdout_tail_locked()
            tail = "\n".join(list(_STATE.stdout_tail)[-20:])
            cmd = " ".join(_STATE.last_cmd)
            log_path = str(_STATE.log_path) if _STATE.log_path else ""
            raise RuntimeError(
                "Timed out waiting for CSpyServer2 registry port. "
                f"Command: {cmd}. Log: {log_path}. Recent output:\n{tail}"
            )

    port = _STATE.registry_port
    if port is None:
        raise RuntimeError("Internal error: registry port event set without port")

    if not _is_reachable(host, port):
        raise RuntimeError(
            f"CSpyServer2 reported registry port {port}, but {host}:{port} is unreachable"
        )
    return int(port)


def _is_running_and_healthy_locked(cfg: ThriftConfig) -> bool:
    proc = _STATE.process
    port = _STATE.registry_port
    if proc is None or proc.poll() is not None or port is None:
        return False
    return _is_reachable(_effective_registry_host(cfg), int(port))


def ensure_managed_server(cfg: ThriftConfig) -> tuple[str, int]:
    _require_managed_mode(cfg)

    join_port: int | None = None
    if cfg.launcher_executable is not None:
        # An IarServiceLauncher is available, so it hosts the IDE services and
        # owns the registry: it comes up first and its endpoint is what
        # everything resolves through, whether or not a CSpyServer2 joins it.
        host, join_port = ensure_launcher_registry(cfg)
        os.environ["THRIFT_REGISTRY_PORT"] = str(join_port)
        os.environ.setdefault("THRIFT_REGISTRY_HOST", host)
        if cfg.cspy_executable is None:
            # No debugger at that path, or one was declined: project/options
            # tools work and debugger tools fail to resolve `debugger` with a
            # clear registry error.
            return host, int(join_port)

    with _STATE.lock:
        if _is_running_and_healthy_locked(cfg):
            return _effective_registry_host(cfg), int(_STATE.registry_port)

        if _STATE.process is not None and _STATE.process.poll() is None:
            if not cfg.cspy_restart_on_failure:
                raise RuntimeError("Managed CSpyServer2 is unhealthy and restart is disabled")
            _cleanup_process_locked()

        last_error: Exception | None = None
        attempts = 2 if cfg.cspy_restart_on_failure else 1
        for _ in range(attempts):
            try:
                _start_process_locked(cfg, join_port)
                port = _wait_for_registry_port_locked(cfg)
                if join_port is not None and int(port) != int(join_port):
                    raise RuntimeError(
                        f"CSpyServer2 was asked to join registry port {join_port} but "
                        f"reported port {port}; it did not attach to the "
                        "IarServiceLauncher registry"
                    )
                os.environ["THRIFT_REGISTRY_PORT"] = str(port)
                os.environ.setdefault("THRIFT_REGISTRY_HOST", _effective_registry_host(cfg))
                return _effective_registry_host(cfg), int(port)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                _cleanup_process_locked()

        raise RuntimeError(f"Failed to start managed CSpyServer2: {last_error}")


def apply_managed_registry_to_config(cfg: ThriftConfig) -> ThriftConfig:
    if cfg.cspy_mode != "managed":
        return cfg
    host, port = ensure_managed_server(cfg)
    return replace(cfg, registry_host=host, registry_port=int(port))


def managed_server_status() -> dict[str, object]:
    with _STATE.lock:
        proc = _STATE.process
        running = proc is not None and proc.poll() is None
        return {
            "mode": "managed",
            "running": bool(running),
            "pid": int(proc.pid) if running and proc is not None else None,
            "registry_port": _STATE.registry_port,
            "recent_output": list(_STATE.stdout_tail)[-20:],
            "last_process_diagnostics": _STATE.last_process_diagnostics,
        }


def managed_server_crash_diagnostics(max_lines: int = 40) -> str:
    """Return best-effort managed CSpyServer2 diagnostics for Thrift failures.

    If the process has exited, includes exit code and recent output.
    If it is still running, includes a recent output tail to aid triage.
    If a process was recently cleaned up/restarted, returns the last snapshot.
    """
    with _STATE.lock:
        proc = _STATE.process
        if proc is None:
            return _STATE.last_process_diagnostics

        _refresh_stdout_tail_locked()

        line_limit = max(1, int(max_lines))
        tail = list(_STATE.stdout_tail)[-line_limit:]
        tail_text = "\n".join(tail) if tail else "<no output captured>"
        cmd = " ".join(_STATE.last_cmd) if _STATE.last_cmd else "<unknown>"
        log_path = str(_STATE.log_path) if _STATE.log_path else "<none>"

        if proc.poll() is None:
            return (
                "Managed CSpyServer2 is still running, but a Thrift call failed. "
                f"PID: {proc.pid}. "
                f"Command: {cmd}. "
                f"Log: {log_path}. "
                "Recent output:\n"
                f"{tail_text}"
            )

        return (
            "CSpyServer2 appears to have exited. "
            f"Exit code: {proc.returncode}. "
            f"Command: {cmd}. "
            f"Log: {log_path}. "
            "Recent output:\n"
            f"{tail_text}"
        )


def shutdown_managed_server() -> None:
    with _STATE.lock:
        _cleanup_process_locked()


atexit.register(shutdown_managed_server)
