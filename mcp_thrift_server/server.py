from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from thriftpy2.rpc import make_client, make_server
from mcp.server.fastmcp import FastMCP

from .config import load_config
from .cspy_server_manager import (
    apply_managed_registry_to_config,
    managed_server_crash_diagnostics,
    managed_server_status,
    shutdown_managed_server,
)
from .thrift_client import (
    ThriftBridgeError,
    coerce_call_args,
    get_debugger_service,
    load_service_registry_module,
    load_thrift_module,
    open_debugger_client,
    resolve_service_endpoint,
    to_plain,
)

mcp = FastMCP(
    "thrift-debugger-server",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8000")),
)


class _SuppressConnectionResetFilter(logging.Filter):
    """Drop expected connection-reset tracebacks from thriftpy2 callback servers.

    When a debug session or the managed backend process stops, its open
    connections to our eventhandler/libsupport/listwindow listeners reset.
    thriftpy2's TThreadedServer only swallows TTransportException and logs a
    full traceback for ConnectionResetError; that is normal teardown, not an
    error worth surfacing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))


logging.getLogger("thriftpy2.server").addFilter(_SuppressConnectionResetFilter())

_EVENTHANDLER_LOCK = threading.Lock()
_EVENTHANDLER_STARTED = False
_EVENTHANDLER_HOST = ""
_EVENTHANDLER_PORT = 0
_EVENTHANDLER_SERVER: Any | None = None
_EVENTHANDLER_THREAD: threading.Thread | None = None

_LIBSUPPORT_LOCK = threading.Lock()
_LIBSUPPORT_STARTED = False
_LIBSUPPORT_HOST = ""
_LIBSUPPORT_PORT = 0
_LIBSUPPORT_SERVER: Any | None = None
_LIBSUPPORT_THREAD: threading.Thread | None = None
_LIBSUPPORT_OUTPUT_TEXT: deque[str] = deque(maxlen=5000)
_LIBSUPPORT_OUTPUT_BYTES = bytearray()
_LIBSUPPORT_INPUT_BYTES = bytearray()
_LIBSUPPORT_EXIT_CODE: int | None = None
_LIBSUPPORT_ASSERTS: deque[dict[str, str]] = deque(maxlen=100)
_LIBSUPPORT_MAX_BUFFER_BYTES = 256 * 1024

_LISTWINDOW_LOCK = threading.Lock()
_LISTWINDOW_STARTED = False
_LISTWINDOW_HOST = ""
_LISTWINDOW_PORT = 0
_LISTWINDOW_SERVER: Any | None = None
_LISTWINDOW_THREAD: threading.Thread | None = None
_LISTWINDOW_CONNECTED: set[str] = set()
_LISTWINDOW_NOTES: deque[dict[str, Any]] = deque(maxlen=500)
_LISTWINDOW_TOOLBAR_NOTES: deque[dict[str, Any]] = deque(maxlen=500)

_SESSION_LOCK = threading.Lock()
_SESSION_CONFIGURED = False
_SESSION_STARTED = False


_DC_RESULT_VALUE_TO_NAME: dict[int, str] = {
    0: "kDcOk",
    1: "kDcRequestedStop",
    2: "kDcOtherStop",
    3: "kDcUnconditionalStop",
    4: "kDcSympatheticStop",
    5: "kDcBusy",
    6: "kDcError",
    7: "kDcFatalError",
    8: "kDcLicenseViolation",
    9: "kDcSilentFatalError",
    10: "kDcFailure",
    11: "kDcDllLoadLibFailed",
    12: "kDcDllFuncNotFound",
    13: "kDcDllFuncSlotEmpty",
    14: "kDcDllVersionMismatch",
    15: "kDcUnavailable",
}

_FOCUSED_DC_RESULT_VALUES: tuple[int, ...] = (0, 5, 6, 7, 8, 9, 10, 15)


def _dc_result_name(code: int | None) -> str | None:
    if code is None:
        return None
    return _DC_RESULT_VALUE_TO_NAME.get(int(code))


def _all_dc_result_constants() -> list[dict[str, Any]]:
    return [{"value": v, "name": n} for v, n in sorted(_DC_RESULT_VALUE_TO_NAME.items(), key=lambda x: x[0])]


def _focused_dc_result_constants() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value in _FOCUSED_DC_RESULT_VALUES:
        name = _DC_RESULT_VALUE_TO_NAME.get(value)
        if name is not None:
            out.append({"value": value, "name": name})
    return out


def _extract_cspy_exception_info(exc: Exception) -> dict[str, Any] | None:
    # Best case: thrift exception object with fields.
    code = getattr(exc, "code", None)
    method = getattr(exc, "method", None)
    message = getattr(exc, "message", None)
    culprit = getattr(exc, "culprit", None)
    if code is not None and (method is not None or message is not None or culprit is not None):
        code_i = int(code)
        return {
            "code": code_i,
            "code_name": _dc_result_name(code_i),
            "method": str(method or ""),
            "message": str(message or ""),
            "culprit": str(culprit or ""),
        }

    # Fallback for textual exception rendering.
    text = str(exc)
    m = re.search(
        r"CSpyException\(code=(\d+), method='([^']*)', message='([^']*)', culprit='([^']*)'\)",
        text,
    )
    if not m:
        return None

    code_i = int(m.group(1))
    return {
        "code": code_i,
        "code_name": _dc_result_name(code_i),
        "method": m.group(2),
        "message": m.group(3),
        "culprit": m.group(4),
    }


def _format_cspy_exception_for_humans(exc: Exception) -> str:
    info = _extract_cspy_exception_info(exc)
    if info is None:
        return str(exc)

    code_name = info.get("code_name") or "UnknownDcResult"
    code = info.get("code")
    method = info.get("method", "")
    message = info.get("message", "")
    culprit = info.get("culprit", "")

    parts = [
        f"CSpyException {code_name} ({code})",
        f"method={method or '<unknown>'}",
        f"message={message or '<none>'}",
    ]
    if culprit:
        parts.append(f"culprit={culprit}")

    if int(code) == 8:
        parts.append(
            "hint=License violation while loading/starting debug session. "
            "Check IAR_LMS_BEARER_TOKEN or license checkout/environment entitlement."
        )
    elif int(code) == 6 and method == "DkStop":
        parts.append(
            "hint=Backend reported suspend failure; debugger may already be stopped. "
            "Wrapper can treat this as idempotent if core state is halted."
        )

    return "; ".join(parts)


def _probe_stop_effective_state() -> dict[str, Any]:
    """Best-effort probe used to decide if stopSession failure can be treated as idempotent."""
    out: dict[str, Any] = {"already_stopped": False}

    try:
        online = bool(_call_debugger("isOnline"))
        out["online"] = online
    except Exception as exc:  # noqa: BLE001
        out["online_error"] = _format_cspy_exception_for_humans(exc)
        return out

    if not out["online"]:
        out["already_stopped"] = True
        return out

    try:
        core_count = int(_call_debugger("getNumberOfCores"))
        out["core_count"] = core_count
    except Exception as exc:  # noqa: BLE001
        out["core_count_error"] = _format_cspy_exception_for_humans(exc)
        return out

    if core_count <= 0:
        return out

    core_states: list[int] = []
    try:
        for idx in range(core_count):
            core_states.append(int(_call_debugger("getCoreState", idx)))
    except Exception as exc:  # noqa: BLE001
        out["core_state_error"] = _format_cspy_exception_for_humans(exc)
        return out

    out["core_states"] = core_states
    out["already_stopped"] = all(state == 0 for state in core_states)
    return out


def _should_treat_stop_error_as_idempotent(exc: Exception) -> tuple[bool, dict[str, Any] | None]:
    info = _extract_cspy_exception_info(exc)
    if not info:
        return False, None

    if int(info.get("code", -1)) != 6 or info.get("method") != "DkStop":
        return False, info

    probe = _probe_stop_effective_state()
    if bool(probe.get("already_stopped")):
        return True, {"exception": info, "probe": probe}

    return False, {"exception": info, "probe": probe}


def _safe_repr(value: Any, max_len: int = 180) -> str:
    text = repr(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _set_session_state(configured: bool | None = None, started: bool | None = None) -> None:
    global _SESSION_CONFIGURED, _SESSION_STARTED
    with _SESSION_LOCK:
        if configured is not None:
            _SESSION_CONFIGURED = bool(configured)
        if started is not None:
            _SESSION_STARTED = bool(started)


def _session_state() -> dict[str, bool]:
    with _SESSION_LOCK:
        return {"configured": _SESSION_CONFIGURED, "started": _SESSION_STARTED}


def _require_session_started(tool_name: str) -> None:
    state = _session_state()
    if state["configured"] and state["started"]:
        return
    raise ThriftBridgeError(
        f"{tool_name} requires an active debug session. Required order: "
        "debugger_configure_session(launch_json) -> debugger_start_smp_session()."
    )


def _clear_runtime_session_caches() -> None:
    """Clear per-session runtime caches that become stale across reconfigure/stop."""
    with _LISTWINDOW_LOCK:
        _LISTWINDOW_CONNECTED.clear()


def _trace_thrift(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [thrift] {message}", file=sys.stderr, flush=True)


def _response_envelope(
    *,
    ok: bool,
    data: Any,
    tool: str,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "tool": tool,
        "data": data,
        "error": error,
    }


def _error_entry(
    *,
    code: str,
    category: str,
    message: str,
    retryable: bool,
    details: Any | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": str(code),
        "category": str(category),
        "message": str(message),
        "retryable": bool(retryable),
    }
    if details is not None:
        out["details"] = details
    return out


def _should_attach_backend_diagnostics(error_text: str) -> bool:
    low = (error_text or "").lower()

    # Caller/input/serialization mistakes should not be reported as backend faults.
    client_side_markers = (
        "needs type",
        "field '",
        "invalid json",
        "must be non-empty",
        "unexpected keyword argument",
        "missing required positional argument",
    )
    if any(marker in low for marker in client_side_markers):
        return False

    backend_markers = (
        "winerror",
        "connection reset",
        "connection refused",
        "actively refused",
        "timed out",
        "timeout",
        "failed to resolve service",
        "cspyexception",
        "read memory failed",
        "failed to suspend debugger",
        "thrift rpc failed",
    )
    return any(marker in low for marker in backend_markers)


def _classify_error(exc: Exception, fallback_code: str = "UNKNOWN_ERROR") -> dict[str, Any]:
    text = _format_cspy_exception_for_humans(exc)
    low = text.lower()
    diagnostics = managed_server_crash_diagnostics() if _should_attach_backend_diagnostics(text) else ""

    def _with_diag(entry: dict[str, Any]) -> dict[str, Any]:
        if diagnostics:
            entry["backend_diagnostics"] = diagnostics
        return entry

    if "timed out" in low or "timeout" in low:
        return _with_diag(
            _error_entry(
            code="TIMEOUT",
            category="timeout",
            message=text,
            retryable=True,
            )
        )
    if "10054" in low or "connection reset" in low:
        return _with_diag(
            _error_entry(
            code="TRANSPORT_CONNECTION_RESET",
            category="transport",
            message=text,
            retryable=True,
            )
        )
    if "connection refused" in low or "actively refused" in low:
        return _with_diag(
            _error_entry(
            code="TRANSPORT_CONNECTION_REFUSED",
            category="transport",
            message=text,
            retryable=True,
            )
        )
    if "requires an active debug session" in low:
        return _with_diag(
            _error_entry(
            code="SESSION_NOT_STARTED",
            category="lifecycle",
            message=text,
            retryable=False,
            )
        )

    cspy_info = _extract_cspy_exception_info(exc)
    if cspy_info is not None:
        dc_name = str(cspy_info.get("code_name") or "UnknownDcResult")
        code_value = int(cspy_info.get("code", -1))
        method = str(cspy_info.get("method") or "")

        if code_value == 8:
            return _with_diag(
                _error_entry(
                    code="CSPY_KDCLICENSEVIOLATION",
                    category="backend",
                    message=(
                        "License violation reported by debugger while loading/starting session. "
                        f"method={method or '<unknown>'}; dc_result={dc_name} ({code_value})."
                    ),
                    retryable=False,
                    details={
                        "cspy": cspy_info,
                        "hint": "Verify bearer token/license checkout and entitlement for target/debug feature.",
                    },
                )
            )

        return _with_diag(
            _error_entry(
                code=f"CSPY_{dc_name.upper()}",
                category="backend",
                message=(
                    f"Debugger returned {dc_name} ({code_value})"
                    + (f" from {method}." if method else ".")
                ),
                retryable=(code_value in {5, 6, 7}),
                details={"cspy": cspy_info},
            )
        )

    return _with_diag(
        _error_entry(
            code=fallback_code,
            category="unknown",
            message=text,
            retryable=False,
        )
    )


def _invoke_with_trace(service: str, method: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    args_preview = ", ".join(_safe_repr(a) for a in args)
    kwargs_preview = ", ".join(f"{k}={_safe_repr(v)}" for k, v in kwargs.items())
    joined = ", ".join(x for x in (args_preview, kwargs_preview) if x)
    _trace_thrift(f"call {service}.{method}({joined})")

    start = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _trace_thrift(
            f"error {service}.{method} after {elapsed_ms:.1f}ms: "
            f"{type(exc).__name__}: {_safe_repr(_format_cspy_exception_for_humans(exc))}"
        )
        diagnostics = ""
        if _should_attach_backend_diagnostics(str(exc)):
            diagnostics = managed_server_crash_diagnostics()
        friendly = _format_cspy_exception_for_humans(exc)
        if diagnostics:
            raise ThriftBridgeError(f"{friendly}\n{diagnostics}") from exc
        if _extract_cspy_exception_info(exc) is not None:
            raise ThriftBridgeError(friendly) from exc
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _trace_thrift(f"ok {service}.{method} in {elapsed_ms:.1f}ms -> {type(result).__name__}")
    return result


class _DebugEventListenerHandler:
    def postDebugEvent(self, event):
        return None

    def postLogEvent(self, event):
        return None

    def postInspectionContextChangedEvent(self, event):
        return None

    def postBaseContextChangedEvent(self, event):
        return None


def _libsupport_auto_enabled() -> bool:
    raw = os.getenv("THRIFT_AUTO_LIBSUPPORT", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _append_libsupport_output(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace")
    with _LIBSUPPORT_LOCK:
        _LIBSUPPORT_OUTPUT_TEXT.append(text)
        _LIBSUPPORT_OUTPUT_BYTES.extend(data)
        if len(_LIBSUPPORT_OUTPUT_BYTES) > _LIBSUPPORT_MAX_BUFFER_BYTES:
            overflow = len(_LIBSUPPORT_OUTPUT_BYTES) - _LIBSUPPORT_MAX_BUFFER_BYTES
            del _LIBSUPPORT_OUTPUT_BYTES[:overflow]


class _LibSupportServiceHandler:
    def requestInputBinary(self, length):
        n = max(0, int(length))
        with _LIBSUPPORT_LOCK:
            chunk = bytes(_LIBSUPPORT_INPUT_BYTES[:n])
            del _LIBSUPPORT_INPUT_BYTES[:n]
        _trace_thrift(f"libsupport requestInputBinary(len={n}) -> {len(chunk)} bytes")
        return chunk

    def requestInput(self, length):
        data = self.requestInputBinary(length)
        return data.decode("utf-8", errors="replace")

    def printOutputBinary(self, data):
        payload = bytes(data)
        _append_libsupport_output(payload)
        _trace_thrift(f"libsupport printOutputBinary({len(payload)} bytes)")
        return None

    def printOutput(self, data):
        payload = str(data).encode("utf-8", errors="replace")
        _append_libsupport_output(payload)
        _trace_thrift(f"libsupport printOutput({len(payload)} bytes)")
        return None

    def exit(self, code):
        with _LIBSUPPORT_LOCK:
            global _LIBSUPPORT_EXIT_CODE
            _LIBSUPPORT_EXIT_CODE = int(code)
        _trace_thrift(f"libsupport exit(code={int(code)})")
        return None

    def reportAssert(self, file, line, message):
        entry = {
            "file": str(file),
            "line": str(line),
            "message": str(message),
        }
        with _LIBSUPPORT_LOCK:
            _LIBSUPPORT_ASSERTS.append(entry)
        _trace_thrift(
            "libsupport reportAssert(" 
            f"file={_safe_repr(entry['file'])}, line={_safe_repr(entry['line'])}, message={_safe_repr(entry['message'])})"
        )
        return None


class _ListWindowFrontendHandler:
    def notify(self, note):
        with _LISTWINDOW_LOCK:
            _LISTWINDOW_NOTES.append(to_plain(note))
        return None

    def notifyToolbar(self, note):
        with _LISTWINDOW_LOCK:
            _LISTWINDOW_TOOLBAR_NOTES.append(to_plain(note))
        return None


def _pick_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _eventhandler_auto_enabled() -> bool:
    raw = os.getenv("THRIFT_AUTO_EVENTHANDLER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _is_tcp_endpoint_reachable(host: str, port: int, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except Exception:
        return False


def _ensure_debug_eventhandler() -> dict[str, Any]:
    """Ensure debugger.eventhandler exists in registry.

    This avoids configure/start failures in environments where the backend expects
    a registered debug-event listener service.
    """
    if not _eventhandler_auto_enabled():
        return {"ok": False, "reason": "auto-eventhandler-disabled"}

    cfg = load_config()
    try:
        cfg = apply_managed_registry_to_config(cfg)
    except Exception as exc:  # noqa: BLE001
        raise ThriftBridgeError(f"Failed to prepare managed CSpyServer2: {exc}") from exc
    if cfg.registry_port is None:
        return {"ok": False, "reason": "no-registry-port"}

    registry_host = cfg.registry_host or cfg.host
    include_dirs = tuple(cfg.include_dirs)
    registry_mod = load_service_registry_module(include_dirs)

    global _EVENTHANDLER_STARTED, _EVENTHANDLER_HOST, _EVENTHANDLER_PORT
    global _EVENTHANDLER_SERVER, _EVENTHANDLER_THREAD

    with _EVENTHANDLER_LOCK:
        # Check if service is already present.
        registry = make_client(
            registry_mod.CSpyServiceRegistry,
            registry_host,
            cfg.registry_port,
            timeout=cfg.timeout_ms,
        )
        try:
            services = _invoke_with_trace("registry", "getServices", registry.getServices)
            existing = services.get("debugger.eventhandler")
            if existing is not None:
                existing_host = str(getattr(existing, "host", ""))
                existing_port = int(getattr(existing, "port", 0) or 0)
                if _is_tcp_endpoint_reachable(existing_host, existing_port):
                    return {
                        "ok": True,
                        "status": "already-registered",
                        "host": existing_host,
                        "port": existing_port,
                    }

            if not _EVENTHANDLER_STARTED:
                cspy_mod = load_thrift_module(str(cfg.thrift_file), include_dirs)
                listener_service = getattr(cspy_mod, "DebugEventListener", None)
                if listener_service is None:
                    raise ThriftBridgeError("Service 'DebugEventListener' not found in cspy.thrift")

                host = "127.0.0.1"
                port = _pick_free_port(host)

                server = make_server(
                    listener_service,
                    _DebugEventListenerHandler(),
                    host,
                    port,
                    client_timeout=int(os.getenv("THRIFT_EVENTHANDLER_CLIENT_TIMEOUT_MS", "3600000")),
                )

                # Per-connection handler threads must be daemon threads, or a
                # backend connection blocked in recv (client_timeout up to 1h)
                # prevents interpreter exit before atexit cleanup can run.
                server.daemon = True
                thread = threading.Thread(target=server.serve, daemon=True, name="mcp-debug-eventhandler")
                thread.start()
                time.sleep(0.2)

                _EVENTHANDLER_STARTED = True
                _EVENTHANDLER_HOST = host
                _EVENTHANDLER_PORT = port
                _EVENTHANDLER_SERVER = server
                _EVENTHANDLER_THREAD = thread
            elif not _is_tcp_endpoint_reachable(_EVENTHANDLER_HOST, _EVENTHANDLER_PORT):
                # Local listener died; recreate and re-register.
                cspy_mod = load_thrift_module(str(cfg.thrift_file), include_dirs)
                listener_service = getattr(cspy_mod, "DebugEventListener", None)
                if listener_service is None:
                    raise ThriftBridgeError("Service 'DebugEventListener' not found in cspy.thrift")

                host = "127.0.0.1"
                port = _pick_free_port(host)
                server = make_server(
                    listener_service,
                    _DebugEventListenerHandler(),
                    host,
                    port,
                    client_timeout=int(os.getenv("THRIFT_EVENTHANDLER_CLIENT_TIMEOUT_MS", "3600000")),
                )
                # Per-connection handler threads must be daemon threads, or a
                # backend connection blocked in recv (client_timeout up to 1h)
                # prevents interpreter exit before atexit cleanup can run.
                server.daemon = True
                thread = threading.Thread(target=server.serve, daemon=True, name="mcp-debug-eventhandler")
                thread.start()
                time.sleep(0.2)
                _EVENTHANDLER_HOST = host
                _EVENTHANDLER_PORT = port
                _EVENTHANDLER_SERVER = server
                _EVENTHANDLER_THREAD = thread

            loc = registry_mod.ServiceLocation(
                host=_EVENTHANDLER_HOST,
                port=_EVENTHANDLER_PORT,
                protocol=registry_mod.Protocol.Binary,
                transport=registry_mod.Transport.Socket,
            )
            _invoke_with_trace(
                "registry",
                "registerService",
                registry.registerService,
                "debugger.eventhandler",
                loc,
            )
            return {
                "ok": True,
                "status": "registered",
                "host": _EVENTHANDLER_HOST,
                "port": _EVENTHANDLER_PORT,
            }
        finally:
            registry.close()


def _ensure_libsupport() -> dict[str, Any]:
    """Ensure libsupport service exists in registry and capture target I/O."""
    if not _libsupport_auto_enabled():
        return {"ok": False, "reason": "auto-libsupport-disabled"}

    cfg = load_config()
    try:
        cfg = apply_managed_registry_to_config(cfg)
    except Exception as exc:  # noqa: BLE001
        raise ThriftBridgeError(f"Failed to prepare managed CSpyServer2: {exc}") from exc
    if cfg.registry_port is None:
        return {"ok": False, "reason": "no-registry-port"}

    include_dirs = tuple(cfg.include_dirs)
    registry_host = cfg.registry_host or cfg.host
    registry_mod = load_service_registry_module(include_dirs)

    thrift_path = _find_include_thrift(cfg.include_dirs, "libsupport.thrift")
    libsupport_mod = load_thrift_module(str(thrift_path), include_dirs)
    service = getattr(libsupport_mod, "LibSupportService2", None)
    if service is None:
        raise ThriftBridgeError("Service 'LibSupportService2' not found in libsupport.thrift")

    global _LIBSUPPORT_STARTED, _LIBSUPPORT_HOST, _LIBSUPPORT_PORT
    global _LIBSUPPORT_SERVER, _LIBSUPPORT_THREAD

    with _LIBSUPPORT_LOCK:
        if not _LIBSUPPORT_STARTED or not _is_tcp_endpoint_reachable(_LIBSUPPORT_HOST, _LIBSUPPORT_PORT):
            host = "127.0.0.1"
            port = _pick_free_port(host)
            server = make_server(
                service,
                _LibSupportServiceHandler(),
                host,
                port,
                client_timeout=int(os.getenv("THRIFT_LIBSUPPORT_CLIENT_TIMEOUT_MS", "3600000")),
            )
            server.daemon = True
            thread = threading.Thread(target=server.serve, daemon=True, name="mcp-libsupport")
            thread.start()
            time.sleep(0.2)

            _LIBSUPPORT_STARTED = True
            _LIBSUPPORT_HOST = host
            _LIBSUPPORT_PORT = port
            _LIBSUPPORT_SERVER = server
            _LIBSUPPORT_THREAD = thread

    registry = make_client(
        registry_mod.CSpyServiceRegistry,
        registry_host,
        cfg.registry_port,
        timeout=cfg.timeout_ms,
    )
    try:
        loc = registry_mod.ServiceLocation(
            host=_LIBSUPPORT_HOST,
            port=_LIBSUPPORT_PORT,
            protocol=registry_mod.Protocol.Binary,
            transport=registry_mod.Transport.Socket,
        )
        _invoke_with_trace("registry", "registerService", registry.registerService, "libsupport", loc)
    finally:
        registry.close()

    return {
        "ok": True,
        "status": "registered",
        "host": _LIBSUPPORT_HOST,
        "port": _LIBSUPPORT_PORT,
    }


def _ensure_listwindow_frontend() -> dict[str, Any]:
    cfg = load_config()
    thrift_path = _find_include_thrift(cfg.include_dirs, "listwindow.thrift")
    mod = load_thrift_module(str(thrift_path), tuple(cfg.include_dirs))
    service = getattr(mod, "ListWindowFrontend", None)
    if service is None:
        raise ThriftBridgeError("Service 'ListWindowFrontend' not found in listwindow.thrift")

    global _LISTWINDOW_STARTED, _LISTWINDOW_HOST, _LISTWINDOW_PORT
    global _LISTWINDOW_SERVER, _LISTWINDOW_THREAD

    with _LISTWINDOW_LOCK:
        if not _LISTWINDOW_STARTED or not _is_tcp_endpoint_reachable(_LISTWINDOW_HOST, _LISTWINDOW_PORT):
            host = "127.0.0.1"
            port = _pick_free_port(host)
            server = make_server(
                service,
                _ListWindowFrontendHandler(),
                host,
                port,
                client_timeout=int(os.getenv("THRIFT_LISTWINDOW_CLIENT_TIMEOUT_MS", "3600000")),
            )
            server.daemon = True
            thread = threading.Thread(target=server.serve, daemon=True, name="mcp-listwindow-frontend")
            thread.start()
            time.sleep(0.2)

            _LISTWINDOW_STARTED = True
            _LISTWINDOW_HOST = host
            _LISTWINDOW_PORT = port
            _LISTWINDOW_SERVER = server
            _LISTWINDOW_THREAD = thread

    return {
        "ok": True,
        "host": _LISTWINDOW_HOST,
        "port": _LISTWINDOW_PORT,
    }


def _call_debugger(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    args, kwargs = coerce_call_args(get_debugger_service(cfg), method, args, kwargs)
    with open_debugger_client(cfg) as client:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"Debugger method not found: {method}")
        return _invoke_with_trace("debugger", method, fn, *args, **kwargs)


def _call_breakpoints(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    bp_thrift = _find_include_thrift(cfg.include_dirs, "breakpoints.thrift")
    bp_mod = load_thrift_module(str(bp_thrift), tuple(cfg.include_dirs))
    bp_service = getattr(bp_mod, "Breakpoints", None)
    if bp_service is None:
        raise ThriftBridgeError("Service 'Breakpoints' not found in breakpoints.thrift")

    args, kwargs = coerce_call_args(bp_service, method, args, kwargs)
    host, port = resolve_service_endpoint(cfg, "breakpoints")
    client = make_client(bp_service, host, port, timeout=cfg.timeout_ms)
    try:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"Breakpoints method not found: {method}")
        return _invoke_with_trace("breakpoints", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _ensure_valid_breakpoint_result(op_name: str, bp: Any, hint: str = "") -> dict[str, Any]:
    data = to_plain(bp)
    if not isinstance(data, dict):
        raise ThriftBridgeError(f"{op_name} returned unexpected result type: {type(data).__name__}")

    is_valid = bool(data.get("valid", False))
    bp_id = int(data.get("id", 0) or 0)
    if not is_valid or bp_id <= 0:
        suffix = f" {hint}" if hint else ""
        raise ThriftBridgeError(
            f"{op_name} returned an invalid breakpoint (id={bp_id}, valid={is_valid}).{suffix}"
        )

    # Refresh from backend so fields like 'enabled' reflect post-create state.
    refreshed = to_plain(_call_breakpoints("getBreakpoint", bp_id))
    return refreshed if isinstance(refreshed, dict) else data


def _shared_module() -> Any:
    cfg = load_config()
    shared_thrift = _find_include_thrift(cfg.include_dirs, "shared.thrift")
    return load_thrift_module(str(shared_thrift), tuple(cfg.include_dirs))


def _location(zone_id: int, address: int) -> Any:
    shared = _shared_module()
    return shared.Location(zone=shared.Zone(id=int(zone_id)), address=int(address))


def _context_ref_from_json(context_json: str | None) -> Any:
    shared = _shared_module()
    if not context_json:
        return shared.ContextRef(type=shared.ContextType.CurrentBase, level=0, core=0, task=0)

    obj = json.loads(context_json)
    if not isinstance(obj, dict):
        raise ThriftBridgeError("context_json must be a JSON object")

    type_raw = obj.get("type", "CurrentBase")
    if isinstance(type_raw, str):
        enum_name = type_raw if type_raw.startswith("k") else f"{type_raw}"
        if hasattr(shared.ContextType, enum_name):
            enum_value = getattr(shared.ContextType, enum_name)
        elif hasattr(shared.ContextType, f"k{type_raw}"):
            enum_value = getattr(shared.ContextType, f"k{type_raw}")
        else:
            # Support canonical names used in shared.thrift enum
            enum_map = {
                "CurrentBase": shared.ContextType.CurrentBase,
                "CurrentInspection": shared.ContextType.CurrentInspection,
                "Stack": shared.ContextType.Stack,
                "Target": shared.ContextType.Target,
                "Task": shared.ContextType.Task,
                "Unknown": shared.ContextType.Unknown,
            }
            enum_value = enum_map.get(type_raw)
            if enum_value is None:
                raise ThriftBridgeError(f"Unknown context type: {type_raw}")
    else:
        enum_value = int(type_raw)

    return shared.ContextRef(
        type=enum_value,
        level=int(obj.get("level", 0) or 0),
        core=int(obj.get("core", 0) or 0),
        task=int(obj.get("task", 0) or 0),
    )


def _call_contextmanager(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    cspy_mod = load_thrift_module(str(cfg.thrift_file), tuple(cfg.include_dirs))
    service = getattr(cspy_mod, "ContextManager", None)
    if service is None:
        raise ThriftBridgeError("Service 'ContextManager' not found in cspy.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    host, port = resolve_service_endpoint(cfg, "debugger.contextmanager")
    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"ContextManager method not found: {method}")
        return _invoke_with_trace("contextmanager", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_memory(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    mem_thrift = _find_include_thrift(cfg.include_dirs, "memory.thrift")
    mem_mod = load_thrift_module(str(mem_thrift), tuple(cfg.include_dirs))
    service = getattr(mem_mod, "CSpyMemory", None)
    if service is None:
        raise ThriftBridgeError("Service 'CSpyMemory' not found in memory.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    host, port = resolve_service_endpoint(cfg, "debugger.memory")
    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"CSpyMemory method not found: {method}")
        return _invoke_with_trace("memory", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_disassembly(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    thrift_path = _find_include_thrift(cfg.include_dirs, "disassembly.thrift")
    mod = load_thrift_module(str(thrift_path), tuple(cfg.include_dirs))
    service = getattr(mod, "Disassembly", None)
    if service is None:
        raise ThriftBridgeError("Service 'Disassembly' not found in disassembly.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    host, port = resolve_service_endpoint(cfg, "disassembly")
    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"Disassembly method not found: {method}")
        return _invoke_with_trace("disassembly", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_sourcelookup(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    thrift_path = _find_include_thrift(cfg.include_dirs, "sourcelookup.thrift")
    mod = load_thrift_module(str(thrift_path), tuple(cfg.include_dirs))
    service = getattr(mod, "SourceLookup", None)
    if service is None:
        raise ThriftBridgeError("Service 'SourceLookup' not found in sourcelookup.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    host, port = resolve_service_endpoint(cfg, "sourcelookup")
    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"SourceLookup method not found: {method}")
        return _invoke_with_trace("sourcelookup", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_libsupport(method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    thrift_path = _find_include_thrift(cfg.include_dirs, "libsupport.thrift")
    mod = load_thrift_module(str(thrift_path), tuple(cfg.include_dirs))
    service = getattr(mod, "LibSupportService2", None)
    if service is None:
        raise ThriftBridgeError("Service 'LibSupportService2' not found in libsupport.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    host, port = resolve_service_endpoint(cfg, "libsupport")
    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"LibSupportService2 method not found: {method}")
        return _invoke_with_trace("libsupport", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_listwindow(service_name: str, method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    thrift_path = _find_include_thrift(cfg.include_dirs, "listwindow.thrift")
    mod = load_thrift_module(str(thrift_path), tuple(cfg.include_dirs))
    service = getattr(mod, "ListWindowBackend", None)
    if service is None:
        raise ThriftBridgeError("Service 'ListWindowBackend' not found in listwindow.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    _ensure_listwindow_frontend()
    host = ""
    port = 0
    try:
        snapshot = _list_registry_services("")
        exact = next((s for s in snapshot if s["name"] == service_name), None)
    except Exception:
        exact = None

    if exact is not None:
        if int(exact.get("transport", -1)) != 0:
            raise ThriftBridgeError(
                f"Listwindow service '{service_name}' uses non-socket transport={exact.get('transport')}"
            )
        host = str(exact.get("host", ""))
        port = int(exact.get("port", 0) or 0)

    if not host or port <= 0:
        host, port = resolve_service_endpoint(cfg, service_name)

    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        with _LISTWINDOW_LOCK:
            needs_connect = service_name not in _LISTWINDOW_CONNECTED

        if needs_connect:
            listener = mod.ServiceRegistry.ServiceLocation(
                host=_LISTWINDOW_HOST,
                port=_LISTWINDOW_PORT,
                protocol=mod.ServiceRegistry.Protocol.Binary,
                transport=mod.ServiceRegistry.Transport.Socket,
            )
            _invoke_with_trace("listwindow", "connect", client.connect, listener)
            with _LISTWINDOW_LOCK:
                _LISTWINDOW_CONNECTED.add(service_name)

        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"ListWindowBackend method not found: {method}")
        return _invoke_with_trace(f"listwindow[{service_name}]", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _call_trace_listwindow(service_name: str, method: str, *args: Any, **kwargs: Any) -> Any:
    cfg = load_config()
    thrift_path = _find_include_thrift(cfg.include_dirs, "listwindow.thrift")
    mod = load_thrift_module(str(thrift_path), tuple(cfg.include_dirs))
    service = getattr(mod, "TraceListWindowBackend", None)
    if service is None:
        raise ThriftBridgeError("Service 'TraceListWindowBackend' not found in listwindow.thrift")

    args, kwargs = coerce_call_args(service, method, args, kwargs)
    _ensure_listwindow_frontend()
    snapshot = _list_registry_services("")
    exact = next((s for s in snapshot if s["name"] == service_name), None)
    if exact is None:
        raise ThriftBridgeError(f"Trace listwindow service not found in registry: {service_name}")
    if int(exact.get("transport", -1)) != 0:
        raise ThriftBridgeError(
            f"Trace listwindow service '{service_name}' uses non-socket transport={exact.get('transport')}"
        )

    host = str(exact.get("host", ""))
    port = int(exact.get("port", 0) or 0)
    if not host or port <= 0:
        raise ThriftBridgeError(f"Trace listwindow service has invalid endpoint: {service_name}")

    client = make_client(service, host, port, timeout=cfg.timeout_ms)
    try:
        with _LISTWINDOW_LOCK:
            needs_connect = service_name not in _LISTWINDOW_CONNECTED

        if needs_connect:
            listener = mod.ServiceRegistry.ServiceLocation(
                host=_LISTWINDOW_HOST,
                port=_LISTWINDOW_PORT,
                protocol=mod.ServiceRegistry.Protocol.Binary,
                transport=mod.ServiceRegistry.Transport.Socket,
            )
            _invoke_with_trace("trace_listwindow", "connect", client.connect, listener)
            with _LISTWINDOW_LOCK:
                _LISTWINDOW_CONNECTED.add(service_name)

        fn = getattr(client, method, None)
        if fn is None or not callable(fn):
            raise ThriftBridgeError(f"TraceListWindowBackend method not found: {method}")
        return _invoke_with_trace(f"trace_listwindow[{service_name}]", method, fn, *args, **kwargs)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _list_registry_services(name_filter: str = "") -> list[dict[str, Any]]:
    cfg = load_config()
    cfg = apply_managed_registry_to_config(cfg)
    if cfg.registry_port is None:
        raise ThriftBridgeError("THRIFT_REGISTRY_PORT is required to query services")

    include_dirs = tuple(cfg.include_dirs)
    registry_mod = load_service_registry_module(include_dirs)
    registry_host = cfg.registry_host or cfg.host

    client = make_client(
        registry_mod.CSpyServiceRegistry,
        registry_host,
        cfg.registry_port,
        timeout=cfg.timeout_ms,
    )
    try:
        services = _invoke_with_trace("registry", "getServices", client.getServices)
    finally:
        client.close()

    query = name_filter.strip().lower()
    out: list[dict[str, Any]] = []
    for name, loc in services.items():
        if query and query not in str(name).lower():
            continue
        out.append(
            {
                "name": str(name),
                "host": str(getattr(loc, "host", "")),
                "port": int(getattr(loc, "port", 0) or 0),
                "protocol": int(getattr(loc, "protocol", -1)),
                "transport": int(getattr(loc, "transport", -1)),
            }
        )
    return sorted(out, key=lambda x: x["name"])


def _find_include_thrift(include_dirs: list[str], filename: str) -> Path:
    for include_dir in include_dirs:
        candidate = Path(include_dir) / filename
        if candidate.exists():
            return candidate
    raise ThriftBridgeError(f"Required thrift file not found in THRIFT_INCLUDE_DIRS: {filename}")


@mcp.tool()
def thrift_connection_info() -> dict[str, Any]:
    """Return active bridge connection settings.

    Returns:
        Dictionary with host/port defaults, timeout, thrift file path, and include dirs.

    Notes:
        This reports local bridge configuration only and does not verify that backend
        services are reachable.
    """
    cfg = load_config()
    info = {
        "host": cfg.host,
        "port": cfg.port,
        "timeout_ms": cfg.timeout_ms,
        "thrift_file": str(cfg.thrift_file),
        "include_dirs": cfg.include_dirs,
        "registry_host": cfg.registry_host,
        "registry_port": cfg.registry_port,
        "registry_service_name": cfg.registry_service_name,
        "cspy_mode": cfg.cspy_mode,
        "cspy_executable": str(cfg.cspy_executable) if cfg.cspy_executable else None,
        "cspy_args": cfg.cspy_args,
        "cspy_start_timeout_ms": cfg.cspy_start_timeout_ms,
        "cspy_restart_on_failure": cfg.cspy_restart_on_failure,
    }
    if cfg.cspy_mode == "managed":
        info["managed_server"] = managed_server_status()
    return info


@mcp.tool()
def debugger_list_methods() -> list[str]:
    """List RPC method names on the Debugger service.

    Returns:
        Sorted list of method names from the loaded thrift service metadata.

    Preconditions:
        THRIFT_FILE and THRIFT_INCLUDE_DIRS must resolve a valid cspy.thrift model.

    Failure cases:
        Raises ThriftBridgeError if Debugger service cannot be loaded.
    """
    cfg = load_config()
    service = get_debugger_service(cfg)

    methods = getattr(service, "thrift_services", None)
    if isinstance(methods, list):
        return sorted(str(item) for item in methods)

    if isinstance(methods, dict):
        return sorted(methods.keys())

    return sorted(
        name
        for name in dir(service)
        if not name.startswith("_") and callable(getattr(service, name, None))
    )


@mcp.tool()
def debugger_get_version() -> str:
    """Get debugger version string via Debugger.getVersionString().

    Returns:
        Backend version string, for example 9.5.4.0.
    """
    return str(_call_debugger("getVersionString"))


@mcp.tool()
def debugger_is_online() -> bool:
    """Check whether target is online via Debugger.isOnline().

    Returns:
        True when target is online, otherwise False.
    """
    return bool(_call_debugger("isOnline"))


@mcp.tool()
def debugger_get_number_of_cores() -> int:
    """Get number of available cores via Debugger.getNumberOfCores()."""
    _require_session_started("debugger_get_number_of_cores")
    return int(_call_debugger("getNumberOfCores"))


@mcp.tool()
def debugger_get_core_state(core: int = 0) -> int:
    """Get state of one core via Debugger.getCoreState(core)."""
    _require_session_started("debugger_get_core_state")
    return int(_call_debugger("getCoreState", int(core)))


@mcp.tool()
def debugger_session_status() -> dict[str, Any]:
    """Return local lifecycle flags and lightweight backend state hints."""
    state = _session_state()
    cfg = load_config()

    status: dict[str, Any] = {
        "configured": bool(state["configured"]),
        "started": bool(state["started"]),
        "backend_mode": cfg.cspy_mode,
        "registry_host": cfg.registry_host,
        "registry_port": cfg.registry_port,
        "backend_online": None,
        "core_count": None,
        "core_states": None,
    }

    if cfg.cspy_mode == "managed":
        status["managed_server"] = managed_server_status()

    try:
        status["backend_online"] = bool(_call_debugger("isOnline"))
    except Exception as exc:  # noqa: BLE001
        status["backend_online_error"] = str(exc)

    if not state["started"]:
        return _response_envelope(ok=True, tool="debugger_session_status", data=status)

    try:
        core_count = int(_call_debugger("getNumberOfCores"))
        status["core_count"] = core_count
        status["core_states"] = [int(_call_debugger("getCoreState", idx)) for idx in range(max(0, core_count))]
    except Exception as exc:  # noqa: BLE001
        status["core_state_error"] = str(exc)

    return _response_envelope(ok=True, tool="debugger_session_status", data=status)


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

    Args:
        launch_json: JSON object string for a single launch configuration.
            If your launch file contains {"configurations": [...]}, pass one element
            from that array, not the outer wrapper object.

    Returns:
        {"ok": True} on success.

    Preconditions:
        Requires service registry access when THRIFT_AUTO_EVENTHANDLER is enabled
        (default), so the server can auto-register debugger.eventhandler if needed.

    Side effects:
        Configures backend session state and may load target/plugin context.
    """
    try:
        _ensure_debug_eventhandler()
        _ensure_libsupport()
        cfg_obj = _call_debugger("resolveLaunchConfiguration", launch_json)
        _call_debugger("configureSession", cfg_obj)
    except Exception as exc:  # noqa: BLE001
        _set_session_state(configured=False, started=False)
        _clear_runtime_session_caches()
        raise ThriftBridgeError(
            "debugger_configure_session failed. Local lifecycle state was reset; "
            "retry with resolve -> configure -> start once backend is healthy. "
            f"Backend error: {exc}"
        ) from exc

    _clear_runtime_session_caches()
    _set_session_state(configured=True, started=False)
    out = {"ok": True}
    return _response_envelope(ok=True, tool="debugger_configure_session", data=out)


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
    state = _session_state()
    if not state["configured"]:
        raise ThriftBridgeError(
            "debugger_start_smp_session requires prior debugger_configure_session(launch_json)."
        )

    try:
        _ensure_debug_eventhandler()
        _ensure_libsupport()
        _call_debugger("startSMPSession")
    except Exception as exc:  # noqa: BLE001
        _set_session_state(started=False)
        raise ThriftBridgeError(
            "debugger_start_smp_session failed. Local session remains configured but not started. "
            f"Backend error: {exc}"
        ) from exc

    _set_session_state(started=True)
    out = {"ok": True}
    return _response_envelope(ok=True, tool="debugger_start_smp_session", data=out)


@mcp.tool()
def debugger_configure_and_start_session(launch_json: str) -> dict[str, Any]:
    """Run the full happy-path lifecycle: configure then start session.

    Equivalent to:
    1) debugger_configure_session(launch_json)
    2) debugger_start_smp_session()

    Managed-mode behavior:
    - Always performs a strict cleanup first (including managed backend shutdown)
      to force a fresh backend process before resolve/configure/start.
    - No backend session/runtime state is expected to carry over between calls.
    """
    cfg = load_config()
    handoff: dict[str, Any] = {
        "had_active_session": False,
        "stale_backend_online": False,
        "stop_attempted": False,
        "strict_cleanup_used": False,
        "managed_fresh_process": False,
    }

    state_before = _session_state()
    local_active = bool(state_before["configured"] or state_before["started"])

    if cfg.cspy_mode == "managed":
        handoff["managed_fresh_process"] = True
        handoff["had_active_session"] = bool(local_active)
        handoff["stop_attempted"] = True
        cleanup = debugger_strict_cleanup(reset_target=False)
        handoff["strict_cleanup_used"] = True
        handoff["strict_cleanup_ok"] = bool(cleanup.get("ok"))
        handoff["stop_ok"] = bool(cleanup.get("ok"))
        if not bool(cleanup.get("ok")):
            raise ThriftBridgeError(
                "debugger_configure_and_start_session (managed mode) could not establish a clean "
                f"fresh backend handoff. Cleanup result: {cleanup}"
            )
    else:
        backend_online = False
        try:
            backend_online = bool(_call_debugger("isOnline"))
        except Exception:
            backend_online = False

        stale_backend_online = (not local_active) and backend_online

        if local_active or stale_backend_online:
            handoff["had_active_session"] = True
            handoff["stale_backend_online"] = bool(stale_backend_online)
            handoff["stop_attempted"] = True
            try:
                if local_active:
                    stop_result = debugger_stop_session()
                    handoff["stop_ok"] = bool(stop_result.get("ok"))
                else:
                    _call_debugger("stopSession")
                    _set_session_state(configured=False, started=False)
                    _clear_runtime_session_caches()
                    handoff["stop_ok"] = True
            except Exception as exc:  # noqa: BLE001
                handoff["stop_ok"] = False
                handoff["stop_error"] = str(exc)

                cleanup = debugger_strict_cleanup(reset_target=False)
                handoff["strict_cleanup_used"] = True
                handoff["strict_cleanup_ok"] = bool(cleanup.get("ok"))
                if not bool(cleanup.get("ok")):
                    raise ThriftBridgeError(
                        "debugger_configure_and_start_session could not establish a clean handoff from "
                        f"a previous session. Cleanup result: {cleanup}"
                    )

    debugger_configure_session(launch_json)
    debugger_start_smp_session()

    # CSpyServer2.startSession() never reads stopOnSymbol itself -- running to
    # that symbol is a frontend responsibility (both the IAR IDE and the
    # VS Code DAP adapter do this after start). We are the frontend here, so
    # replicate that behavior: run to the requested symbol when present.
    stop_on_symbol = None
    try:
        launch_obj = json.loads(launch_json)
        if isinstance(launch_obj, dict):
            stop_on_symbol = launch_obj.get("stopOnSymbol")
    except (TypeError, ValueError):
        stop_on_symbol = None

    ran_to_symbol = False
    stop_on_symbol_error: str | None = None
    if stop_on_symbol:
        try:
            _call_debugger("runToULE", stop_on_symbol, True)
            ran_to_symbol = True
        except Exception as exc:  # noqa: BLE001
            stop_on_symbol_error = str(exc)

    out = {
        "ok": True,
        "configured": True,
        "started": True,
        "handoff": handoff,
        "stopOnSymbol": stop_on_symbol,
        "ranToSymbol": ran_to_symbol,
    }
    if stop_on_symbol_error is not None:
        out["stopOnSymbolError"] = stop_on_symbol_error
    return _response_envelope(ok=True, tool="debugger_configure_and_start_session", data=out)


@mcp.tool()
def libsupport_get_output(clear: bool = False, max_chars: int = 4000) -> dict[str, Any]:
    """Return captured target program output received via libsupport."""
    limit = max(1, int(max_chars))
    with _LIBSUPPORT_LOCK:
        text = "".join(_LIBSUPPORT_OUTPUT_TEXT)
        text_tail = text[-limit:]
        bytes_tail = bytes(_LIBSUPPORT_OUTPUT_BYTES)
        exit_code = _LIBSUPPORT_EXIT_CODE
        asserts = list(_LIBSUPPORT_ASSERTS)

        if clear:
            _LIBSUPPORT_OUTPUT_TEXT.clear()
            _LIBSUPPORT_OUTPUT_BYTES.clear()
            _LIBSUPPORT_ASSERTS.clear()

    return {
        "text": text_tail,
        "text_len": len(text),
        "bytes_hex": bytes_tail.hex(),
        "bytes_len": len(bytes_tail),
        "exit_code": exit_code,
        "asserts": asserts,
    }


@mcp.tool()
def libsupport_clear_output() -> dict[str, Any]:
    """Clear captured libsupport output and assert history."""
    with _LIBSUPPORT_LOCK:
        _LIBSUPPORT_OUTPUT_TEXT.clear()
        _LIBSUPPORT_OUTPUT_BYTES.clear()
        _LIBSUPPORT_ASSERTS.clear()
    return {"ok": True}


@mcp.tool()
def libsupport_push_input(text: str, append_newline: bool = False) -> dict[str, Any]:
    """Queue text for target stdin requests handled by libsupport."""
    payload = text + ("\n" if append_newline else "")
    encoded = payload.encode("utf-8")
    with _LIBSUPPORT_LOCK:
        _LIBSUPPORT_INPUT_BYTES.extend(encoded)
        queued = len(_LIBSUPPORT_INPUT_BYTES)
    return {
        "ok": True,
        "queued_bytes": queued,
        "added_bytes": len(encoded),
    }


@mcp.tool()
def libsupport_request_input_binary(length: int) -> dict[str, Any]:
    """Request pending input bytes from libsupport service."""
    requested = int(length)
    data = bytes(_call_libsupport("requestInputBinary", requested))
    return {"requested": requested, "returned": len(data), "data_hex": data.hex()}


@mcp.tool()
def libsupport_request_input(length: int) -> str:
    """Request pending input text from libsupport service."""
    return str(_call_libsupport("requestInput", int(length)))


@mcp.tool()
def listwindow_list_services(name_filter: str = "listwindow") -> list[dict[str, Any]]:
    """List services registered in ServiceRegistry, optionally filtered by name.

    Useful to discover listwindow-like services (for example trace/list windows)
    before querying rows.
    """
    _require_session_started("listwindow_list_services")
    return _list_registry_services(name_filter)


@mcp.tool()
def listwindow_get_overview(service_name: str) -> dict[str, Any]:
    """Fetch list window metadata such as display name, columns, and row count."""
    _require_session_started("listwindow_get_overview")
    display_name = str(_call_listwindow(service_name, "getDisplayName"))
    columns = to_plain(_call_listwindow(service_name, "getColumnInfo"))
    spec = to_plain(_call_listwindow(service_name, "getListSpec"))
    is_sliding = bool(_call_listwindow(service_name, "isSliding"))
    if is_sliding:
        chunk = to_plain(_call_listwindow(service_name, "getChunkInfo"))
        n_rows = int(chunk.get("numberOfRows", 0) or 0)
    else:
        chunk = None
        n_rows = int(_call_listwindow(service_name, "getNumberOfRows"))
    return {
        "service_name": service_name,
        "display_name": display_name,
        "is_sliding": is_sliding,
        "row_count": n_rows,
        "chunk_info": chunk,
        "columns": columns,
        "list_spec": spec,
    }


@mcp.tool()
def listwindow_get_rows(service_name: str, first_row: int = 0, max_rows: int = 50) -> dict[str, Any]:
    """Read rows from a ListWindowBackend service.

    Args:
        service_name: Exact ServiceRegistry name for the listwindow backend.
        first_row: Start row index.
        max_rows: Maximum rows to fetch.
    """
    _require_session_started("listwindow_get_rows")
    start = max(0, int(first_row))
    count = max(1, int(max_rows))

    is_sliding = bool(_call_listwindow(service_name, "isSliding"))
    if is_sliding:
        chunk_info = to_plain(_call_listwindow(service_name, "getChunkInfo"))
        row_count = int(chunk_info.get("numberOfRows", 0) or 0)
        # Sliding windows need an explicit navigation step before rows are materialized.
        if row_count == 0:
            min_lines = max(100, count)
            _call_listwindow(service_name, "navigateToFraction", 0.5, 0, int(min_lines))
            chunk_info = to_plain(_call_listwindow(service_name, "getChunkInfo"))
            row_count = int(chunk_info.get("numberOfRows", 0) or 0)
    else:
        chunk_info = None
        row_count = int(_call_listwindow(service_name, "getNumberOfRows"))

    end = min(row_count, start + count)

    if row_count > 0:
        _call_listwindow(service_name, "setVisibleRows", int(start), int(max(start, end - 1)))

    rows = []
    for idx in range(start, end):
        row = _call_listwindow(service_name, "getRow", int(idx))
        rows.append({"index": idx, "row": to_plain(row)})

    return {
        "service_name": service_name,
        "is_sliding": is_sliding,
        "chunk_info": chunk_info,
        "row_count": row_count,
        "first_row": start,
        "returned": len(rows),
        "rows": rows,
    }


@mcp.tool()
def listwindow_sliding_navigate(
    service_name: str,
    fraction: float = 0.5,
    chunk_pos: int = 0,
    min_lines: int = 100,
) -> dict[str, Any]:
    """Navigate a sliding listwindow to materialize chunk rows.

    Useful for trace/list windows that report zero rows until a chunk has been requested.
    """
    _require_session_started("listwindow_sliding_navigate")
    result = to_plain(
        _call_listwindow(
            service_name,
            "navigateToFraction",
            float(fraction),
            int(chunk_pos),
            int(max(1, min_lines)),
        )
    )
    chunk = to_plain(_call_listwindow(service_name, "getChunkInfo"))
    return {
        "service_name": service_name,
        "navigate_result": result,
        "chunk_info": chunk,
        "row_count": int(chunk.get("numberOfRows", 0) or 0),
    }


@mcp.tool()
def listwindow_get_notifications(clear: bool = False) -> dict[str, Any]:
    """Return captured ListWindowFrontend notifications from backend callbacks."""
    _require_session_started("listwindow_get_notifications")
    with _LISTWINDOW_LOCK:
        notes = list(_LISTWINDOW_NOTES)
        toolbar_notes = list(_LISTWINDOW_TOOLBAR_NOTES)
        if clear:
            _LISTWINDOW_NOTES.clear()
            _LISTWINDOW_TOOLBAR_NOTES.clear()

    return {
        "notes": notes,
        "toolbar_notes": toolbar_notes,
        "note_count": len(notes),
        "toolbar_note_count": len(toolbar_notes),
    }


@mcp.tool()
def listwindow_trace_status(service_name: str) -> dict[str, Any]:
    """Get trace-window capabilities and enablement state for a trace listwindow service."""
    _require_session_started("listwindow_trace_status")
    return {
        "service_name": service_name,
        "is_enabled": bool(_call_trace_listwindow(service_name, "isEnabled")),
        "can_enable": bool(_call_trace_listwindow(service_name, "canEnable")),
        "can_clear": bool(_call_trace_listwindow(service_name, "canClear")),
        "can_browse": bool(_call_trace_listwindow(service_name, "canBrowse")),
        "is_browsing": bool(_call_trace_listwindow(service_name, "isBrowsing")),
        "supports_trace_settings": bool(_call_trace_listwindow(service_name, "supportsTraceSettings")),
        "progress": to_plain(_call_trace_listwindow(service_name, "getProgress")),
    }


@mcp.tool()
def listwindow_trace_set_enabled(service_name: str, enabled: bool = True) -> dict[str, Any]:
    """Enable or disable a TraceListWindowBackend service."""
    _require_session_started("listwindow_trace_set_enabled")
    _call_trace_listwindow(service_name, "setEnabled", bool(enabled))
    return {
        "ok": True,
        "service_name": service_name,
        "enabled": bool(_call_trace_listwindow(service_name, "isEnabled")),
    }


@mcp.tool()
def listwindow_trace_clear(service_name: str) -> dict[str, Any]:
    """Clear trace data in a TraceListWindowBackend service when supported."""
    _require_session_started("listwindow_trace_clear")
    _call_trace_listwindow(service_name, "clear")
    return {"ok": True, "service_name": service_name}


@mcp.tool()
def debugger_capabilities() -> dict[str, Any]:
    """Return a compact capability snapshot for the current backend/session."""
    state = _session_state()
    cfg = load_config()

    out: dict[str, Any] = {
        "backend_mode": cfg.cspy_mode,
        "session": state,
        "backend_online": None,
        "core_count": None,
        "debugger_methods": [],
        "services": [],
        "errors": [],
    }

    if cfg.cspy_mode == "managed":
        out["managed_server"] = managed_server_status()

    try:
        out["backend_online"] = bool(_call_debugger("isOnline"))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(
            _error_entry(
                code="CAPABILITY_CHECK_FAILED",
                category="discovery",
                message="isOnline failed",
                retryable=True,
                details=_classify_error(exc),
            )
        )

    try:
        out["debugger_methods"] = debugger_list_methods()
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(
            _error_entry(
                code="CAPABILITY_CHECK_FAILED",
                category="discovery",
                message="debugger_list_methods failed",
                retryable=True,
                details=_classify_error(exc),
            )
        )

    if state["started"]:
        try:
            out["core_count"] = int(_call_debugger("getNumberOfCores"))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(
                _error_entry(
                    code="CAPABILITY_CHECK_FAILED",
                    category="discovery",
                    message="getNumberOfCores failed",
                    retryable=True,
                    details=_classify_error(exc),
                )
            )

        try:
            out["services"] = _list_registry_services("")
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(
                _error_entry(
                    code="CAPABILITY_CHECK_FAILED",
                    category="discovery",
                    message="list services failed",
                    retryable=True,
                    details=_classify_error(exc),
                )
            )

    return _response_envelope(ok=True, tool="debugger_capabilities", data=out)


@mcp.tool()
def debugger_stop_session() -> dict[str, Any]:
    """Stop active debug session via Debugger.stopSession().

    Returns:
        {"ok": True} on success.

    Managed-mode behavior:
    - Also shuts down the managed CSpyServer2 process so the next startup uses
      a fresh backend process.
    - No backend session/runtime state is expected to carry over after this call.
    """
    cfg = load_config()
    managed_mode = cfg.cspy_mode == "managed"

    state = _session_state()
    stop_error: Exception | None = None
    shutdown_error: Exception | None = None
    stop_idempotent_recovered = False
    stop_idempotent_details: dict[str, Any] | None = None

    if not state["configured"] and not state["started"]:
        if managed_mode:
            try:
                shutdown_managed_server()
            except Exception as exc:  # noqa: BLE001
                shutdown_error = exc

        _clear_runtime_session_caches()
        _set_session_state(configured=False, started=False)
        if shutdown_error is not None:
            raise ThriftBridgeError(
                "debugger_stop_session completed local stop reset, but managed backend shutdown failed. "
                f"Backend error: {shutdown_error}"
            ) from shutdown_error

        out = {
            "ok": True,
            "already_stopped": True,
            "managed_backend_restarted_on_next_start": bool(managed_mode),
        }
        return _response_envelope(ok=True, tool="debugger_stop_session", data=out)

    try:
        _call_debugger("stopSession")
    except Exception as exc:  # noqa: BLE001
        stop_error = exc

    if stop_error is not None:
        idempotent, details = _should_treat_stop_error_as_idempotent(stop_error)
        if idempotent:
            stop_idempotent_recovered = True
            stop_idempotent_details = details
            stop_error = None

    if managed_mode:
        try:
            shutdown_managed_server()
        except Exception as exc:  # noqa: BLE001
            shutdown_error = exc

    _set_session_state(configured=False, started=False)
    _clear_runtime_session_caches()

    if stop_error is not None or shutdown_error is not None:
        parts = [
            "debugger_stop_session failed. Local lifecycle state was still reset to stopped.",
        ]
        if stop_error is not None:
            parts.append(f"stopSession error: {_format_cspy_exception_for_humans(stop_error)}")
        if shutdown_error is not None:
            parts.append(f"managed shutdown error: {shutdown_error}")
        message = " ".join(parts)
        cause = shutdown_error if shutdown_error is not None else stop_error
        assert cause is not None
        raise ThriftBridgeError(message) from cause

    out = {
        "ok": True,
        "managed_backend_restarted_on_next_start": bool(managed_mode),
    }
    if stop_idempotent_recovered:
        out["stop_idempotent_recovered"] = True
        out["stop_idempotent_details"] = stop_idempotent_details
    return _response_envelope(ok=True, tool="debugger_stop_session", data=out)


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
    errors: list[dict[str, Any]] = []
    before = _session_state()

    if bool(reset_target) and (before["configured"] or before["started"]):
        try:
            _call_debugger("reset")
        except Exception as exc:  # noqa: BLE001
            errors.append(
                _error_entry(
                    code="CLEANUP_STEP_FAILED",
                    category="cleanup",
                    message="reset failed",
                    retryable=True,
                    details=_classify_error(exc),
                )
            )

    if before["configured"] or before["started"]:
        try:
            _call_debugger("stopSession")
        except Exception as exc:  # noqa: BLE001
            idempotent, details = _should_treat_stop_error_as_idempotent(exc)
            if not idempotent:
                errors.append(
                    _error_entry(
                        code="CLEANUP_STEP_FAILED",
                        category="cleanup",
                        message="stopSession failed",
                        retryable=True,
                        details=_classify_error(exc),
                    )
                )
            else:
                _trace_thrift(
                    "cleanup stopSession returned DkStop error but probe shows already stopped; "
                    f"treating as idempotent success: {_safe_repr(details)}"
                )

    _set_session_state(configured=False, started=False)
    _clear_runtime_session_caches()

    with _LISTWINDOW_LOCK:
        _LISTWINDOW_NOTES.clear()
        _LISTWINDOW_TOOLBAR_NOTES.clear()

    with _LIBSUPPORT_LOCK:
        _LIBSUPPORT_INPUT_BYTES.clear()

    try:
        shutdown_managed_server()
    except Exception as exc:  # noqa: BLE001
        errors.append(
            _error_entry(
                code="CLEANUP_STEP_FAILED",
                category="cleanup",
                message="shutdown_managed_server failed",
                retryable=True,
                details=_classify_error(exc),
            )
        )

    after = _session_state()
    out = {
        "ok": len(errors) == 0,
        "errors": errors,
        "before": before,
        "after": after,
        "managed_server_shutdown": True,
    }
    err = None
    if errors:
        err = _error_entry(
            code="PARTIAL_CLEANUP_FAILED",
            category="cleanup",
            message="Cleanup finished with one or more step failures.",
            retryable=True,
        )
    return _response_envelope(
        ok=bool(out["ok"]),
        tool="debugger_strict_cleanup",
        data=out,
        error=err,
    )


@mcp.tool()
def breakpoints_get_all() -> Any:
    """List all breakpoints from the Breakpoints service."""
    _require_session_started("breakpoints_get_all")
    return to_plain(_call_breakpoints("getBreakpoints"))


@mcp.tool()
def breakpoints_get(id: int) -> Any:
    """Get a single breakpoint by id."""
    _require_session_started("breakpoints_get")
    return to_plain(_call_breakpoints("getBreakpoint", int(id)))


@mcp.tool()
def breakpoints_set_from_descriptor(descriptor: str) -> Any:
    """Create/update a breakpoint from an existing descriptor string.

    Important:
        Descriptor is backend-specific opaque data. The reliable source is
        breakpoints_get_all() -> descriptor, then pass that descriptor back here.
        Do not assume this accepts free-form strings like ULE or JSON.
    """
    _require_session_started("breakpoints_set_from_descriptor")
    try:
        return _ensure_valid_breakpoint_result(
            "setBreakpointFromDescriptor",
            _call_breakpoints("setBreakpointFromDescriptor", descriptor),
            hint=(
                "Use descriptor values returned by breakpoints_get_all(); "
                "free-form descriptor strings are not supported."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise ThriftBridgeError(
            "setBreakpointFromDescriptor failed. Use descriptor values returned by "
            "breakpoints_get_all() (round-trip). Free-form descriptor formats are "
            f"not portable across backend versions. Backend error: {exc}"
        ) from exc


@mcp.tool()
def breakpoints_set_on_ule(ule: str, access_type: int = 1) -> Any:
    """Set a breakpoint/watchpoint on a backend ULE expression.

    Preferred for normal breakpoint creation. For code breakpoints, pass
    access_type=1.

    access_type follows shared.AccessType enum values:
        1 = execute/fetch breakpoint (code breakpoint)
        2 = read watchpoint
        3 = write watchpoint
        4 = read/write watchpoint

    ULE parsing uses the debugger Universal Location Expression parser
    (DkUle::ParseUleString with code-context parsing). In practice this accepts:
        - expression ULEs: main, func+4, *ptr
        - absolute ULEs: 0x100, Memory:0x42
        - source ULEs (full form): {E:/path/file.c}.123.1
        - optional range suffix: <ule>@<size>

    Notes:
        - full source ULE form is the reliable backend format; shorthand file:line
          may work in some flows but is backend-dependent.
        - access_type controls breakpoint/watchpoint category selection
          (fetch/read/write/read-write).
        - "main()" may fail on some backends even when "main" works.
        - this call expects an active configured+started debug session
          (resolve -> configure -> start).
        - descriptor strings are a different API: use breakpoints_set_from_descriptor
          only with descriptors returned by breakpoints_get_all().
    """
    _require_session_started("breakpoints_set_on_ule")
    try:
        return _ensure_valid_breakpoint_result(
            "setBreakpointOnUle",
            _call_breakpoints("setBreakpointOnUle", ule, int(access_type)),
            hint=(
                "Verify ULE syntax in backend format (for source ULE prefer "
                "{path}.line.col), and ensure symbols/locations exist in the current image."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise ThriftBridgeError(
            "setBreakpointOnUle failed. Ensure a configured/running debug kernel and "
            "use a backend-valid ULE (for code breakpoints, access_type=1). "
            f"Backend error: {exc}"
        ) from exc


@mcp.tool()
def breakpoints_set_on_ule_with_category(ule: str, access_type: int, category_id: str) -> Any:
    """Set a breakpoint on ULE with category id."""
    _require_session_started("breakpoints_set_on_ule_with_category")
    try:
        return _ensure_valid_breakpoint_result(
            "setBreakpointOnUleWithCategory",
            _call_breakpoints("setBreakpointOnUleWithCategory", ule, int(access_type), category_id),
            hint=(
                "Category IDs may be translated by backend (for example STD_CODE -> STD_CODE2). "
                "Verify category supports the requested access type."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise ThriftBridgeError(
            "setBreakpointOnUleWithCategory failed. Verify ULE/category for the target "
            f"and ensure kernel is active. Backend error: {exc}"
        ) from exc


@mcp.tool()
def breakpoints_enable(id: int, enable: bool = True) -> bool:
    """Enable or disable a breakpoint by id."""
    _require_session_started("breakpoints_enable")
    return bool(_call_breakpoints("enableBreakpoint", int(id), bool(enable)))


@mcp.tool()
def breakpoints_remove(id: int) -> bool:
    """Remove a breakpoint by id."""
    _require_session_started("breakpoints_remove")
    return bool(_call_breakpoints("removeBreakpoint", int(id)))


@mcp.tool()
def breakpoints_recently_hit() -> Any:
    """Return recently hit breakpoints."""
    _require_session_started("breakpoints_recently_hit")
    return to_plain(_call_breakpoints("getRecentlyHitBreakpoints"))


@mcp.tool()
def contextmanager_get_stack(context_json: str = "", low: int = 0, high: int = 20) -> Any:
    """Get call stack frames for a context.

    context_json example: {"type":"CurrentBase"}
    """
    _require_session_started("contextmanager_get_stack")
    ctx = _context_ref_from_json(context_json)
    return to_plain(_call_contextmanager("getStack", ctx, int(low), int(high)))


@mcp.tool()
def contextmanager_get_stack_depth(context_json: str = "", max_depth: int = 256) -> int:
    """Get stack depth for a context."""
    _require_session_started("contextmanager_get_stack_depth")
    ctx = _context_ref_from_json(context_json)
    return int(_call_contextmanager("getStackDepth", ctx, int(max_depth)))


@mcp.tool()
def contextmanager_get_context_info(context_json: str = "") -> Any:
    """Get context info for a context reference."""
    _require_session_started("contextmanager_get_context_info")
    ctx = _context_ref_from_json(context_json)
    return to_plain(_call_contextmanager("getContextInfo", ctx))


@mcp.tool()
def contextmanager_get_locals(context_json: str = "") -> Any:
    """Get local symbols for a context reference."""
    _require_session_started("contextmanager_get_locals")
    ctx = _context_ref_from_json(context_json)
    return to_plain(_call_contextmanager("getLocals", ctx))


@mcp.tool()
def contextmanager_get_parameters(context_json: str = "") -> Any:
    """Get function parameters for a context reference."""
    _require_session_started("contextmanager_get_parameters")
    ctx = _context_ref_from_json(context_json)
    return to_plain(_call_contextmanager("getParameters", ctx))


@mcp.tool()
def symbols_list_visible(context_json: str = "") -> dict[str, Any]:
    """List visible local and parameter symbols in the selected context."""
    _require_session_started("symbols_list_visible")
    ctx = _context_ref_from_json(context_json)
    locals_syms = to_plain(_call_contextmanager("getLocals", ctx)) or []
    params_syms = to_plain(_call_contextmanager("getParameters", ctx)) or []

    local_names = [str(item.get("name", "")) for item in locals_syms if isinstance(item, dict)]
    param_names = [str(item.get("name", "")) for item in params_syms if isinstance(item, dict)]
    merged = []
    seen = set()
    for name in local_names + param_names:
        if name and name not in seen:
            seen.add(name)
            merged.append(name)

    return {
        "locals": local_names,
        "parameters": param_names,
        "all": merged,
        "count": len(merged),
    }


@mcp.tool()
def symbols_lookup(name: str, context_json: str = "", prefix: bool = False) -> dict[str, Any]:
    """Lookup symbol names and evaluate value for exact matches.

    - Uses ContextManager locals/parameters for discovery.
    - Uses Debugger.evalExpression for exact value evaluation.
    """
    _require_session_started("symbols_lookup")
    if not name.strip():
        raise ThriftBridgeError("name must be non-empty")

    ctx = _context_ref_from_json(context_json)
    visible = symbols_list_visible(context_json)
    candidates = visible.get("all", [])
    query = name.strip()

    if prefix:
        matches = [sym for sym in candidates if sym.startswith(query)]
    else:
        matches = [sym for sym in candidates if sym == query]

    shared = _shared_module()
    evaluations: list[dict[str, Any]] = []
    for sym in matches:
        try:
            value = _call_debugger("evalExpression", ctx, sym, [], shared.ExprFormat.kDefault, False)
            evaluations.append({"name": sym, "value": to_plain(value)})
        except Exception as exc:  # noqa: BLE001
            evaluations.append({"name": sym, "error": str(exc)})

    return {
        "query": query,
        "prefix": bool(prefix),
        "matches": matches,
        "evaluations": evaluations,
    }


@mcp.tool()
def memory_read(zone_id: int, address: int, wordsize: int = 1, bitsize: int = 8, count: int = 16) -> dict[str, Any]:
    """Read memory and return bytes as hex.

    address is in units for the selected zone.
    """
    _require_session_started("memory_read")
    loc = _location(zone_id, address)
    data = bytes(_call_memory("readMemory", loc, int(wordsize), int(bitsize), int(count)))
    return {
        "zone_id": int(zone_id),
        "address": int(address),
        "wordsize": int(wordsize),
        "bitsize": int(bitsize),
        "count": int(count),
        "hex": data.hex(),
        "byte_len": len(data),
    }


@mcp.tool()
def memory_write_hex(
    zone_id: int,
    address: int,
    data_hex: str,
    wordsize: int = 1,
    bitsize: int = 8,
    count: int | None = None,
) -> dict[str, Any]:
    """Write memory from hex string payload."""
    _require_session_started("memory_write_hex")
    payload = bytes.fromhex(data_hex)
    n_count = int(count) if count is not None else len(payload) // max(1, int(wordsize))
    loc = _location(zone_id, address)
    _call_memory("writeMemory", loc, int(wordsize), int(bitsize), int(n_count), payload)
    return {
        "ok": True,
        "zone_id": int(zone_id),
        "address": int(address),
        "count": int(n_count),
        "written_bytes": len(payload),
    }


@mcp.tool()
def disassembly_disassemble_range(
    from_zone_id: int,
    from_address: int,
    to_zone_id: int,
    to_address: int,
    context_json: str = "",
) -> Any:
    """Disassemble instruction range between two locations."""
    _require_session_started("disassembly_disassemble_range")
    frm = _location(from_zone_id, from_address)
    to = _location(to_zone_id, to_address)
    ctx = _context_ref_from_json(context_json)
    return to_plain(_call_disassembly("disassembleRange", frm, to, ctx))


@mcp.tool()
def sourcelookup_get_source_ranges(zone_id: int, address: int) -> Any:
    """Map an execution location to source ranges."""
    _require_session_started("sourcelookup_get_source_ranges")
    loc = _location(zone_id, address)
    return to_plain(_call_sourcelookup("getSourceRanges", loc))


@mcp.tool()
def debugger_load_module(filename: str) -> dict[str, Any]:
    """Load a module/executable into debugger session.

    Args:
        filename: Path to executable/module file.

    Returns:
        {"ok": True, "filename": <input>} on success.
    """
    _require_session_started("debugger_load_module")
    _call_debugger("loadModule", filename)
    return {"ok": True, "filename": filename}


@mcp.tool()
def debugger_get_modules() -> Any:
    """List currently loaded modules.

    Returns:
        JSON-serializable list of module records.
    """
    _require_session_started("debugger_get_modules")
    return to_plain(_call_debugger("getModules"))


@mcp.tool()
def debugger_go() -> dict[str, Any]:
    """Run target execution via Debugger.go().

    Returns:
        {"ok": True} on success.

    Side effects:
        Changes target execution state to running.
    """
    _require_session_started("debugger_go")
    _call_debugger("go")
    return {"ok": True}


@mcp.tool()
def debugger_stop() -> dict[str, Any]:
    """Stop target execution via Debugger.stop().

    Returns:
        {"ok": True} on success.
    """
    _require_session_started("debugger_stop")
    _call_debugger("stop")
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
    _require_session_started("debugger_reset")
    _call_debugger("reset")
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
    _require_session_started("debugger_step_over")
    _call_debugger("stepOver")
    return {"ok": True}


@mcp.tool()
def debugger_get_thread_list() -> Any:
    """Return debugger thread list via Debugger.getThreadList()."""
    _require_session_started("debugger_get_thread_list")
    return to_plain(_call_debugger("getThreadList"))


@mcp.tool()
def debugger_get_cycle_counter(core: int = 0) -> int:
    """Return cycle counter for one core via Debugger.getCycleCounter(core)."""
    _require_session_started("debugger_get_cycle_counter")
    return int(_call_debugger("getCycleCounter", int(core)))


@mcp.tool()
def debugger_eval_expression(
    expression: str,
    context_json: str = "",
    format: int = 0,
    dereference: bool = False,
) -> Any:
    """Evaluate an expression in context via Debugger.evalExpression()."""
    _require_session_started("debugger_eval_expression")
    if not expression.strip():
        raise ThriftBridgeError("expression must be non-empty")

    ctx = _context_ref_from_json(context_json)
    shared = _shared_module()
    expr_format = int(format)
    if expr_format == 0:
        expr_format = int(shared.ExprFormat.kDefault)

    return to_plain(
        _call_debugger(
            "evalExpression",
            ctx,
            expression,
            [],
            expr_format,
            bool(dereference),
        )
    )


@mcp.tool()
def debugger_wait_for_core_state(
    desired_state: int = 0,
    core: int = 0,
    timeout_ms: int = 5000,
    poll_interval_ms: int = 50,
) -> dict[str, Any]:
    """Wait until one core reaches the desired state or timeout expires.

    Returns a structured result and does not raise on timeout.
    """
    _require_session_started("debugger_wait_for_core_state")
    target_state = int(desired_state)
    core_id = int(core)
    timeout_s = max(0.0, float(timeout_ms) / 1000.0)
    poll_s = max(0.0, float(poll_interval_ms) / 1000.0)

    start = time.perf_counter()
    deadline = start + timeout_s
    attempts = 0
    last_state = None

    while True:
        attempts += 1
        last_state = int(_call_debugger("getCoreState", core_id))
        if last_state == target_state:
            elapsed_ms = int((time.perf_counter() - start) * 1000.0)
            out = {
                "ok": True,
                "timed_out": False,
                "core": core_id,
                "desired_state": target_state,
                "state": last_state,
                "attempts": attempts,
                "elapsed_ms": elapsed_ms,
            }
            return _response_envelope(ok=True, tool="debugger_wait_for_core_state", data=out)

        now = time.perf_counter()
        if now >= deadline:
            elapsed_ms = int((now - start) * 1000.0)
            out = {
                "ok": False,
                "timed_out": True,
                "core": core_id,
                "desired_state": target_state,
                "state": last_state,
                "attempts": attempts,
                "elapsed_ms": elapsed_ms,
            }
            return _response_envelope(
                ok=False,
                tool="debugger_wait_for_core_state",
                data=out,
                error=_error_entry(
                    code="TIMEOUT",
                    category="timeout",
                    message="Desired core state was not reached before timeout.",
                    retryable=True,
                ),
            )

        if poll_s > 0.0:
            time.sleep(poll_s)


@mcp.tool()
def debugger_go_and_wait_for_core_state(
    desired_state: int = 0,
    core: int = 0,
    timeout_ms: int = 5000,
    poll_interval_ms: int = 50,
) -> dict[str, Any]:
    """Start execution and wait for a desired core state (for example halted=0)."""
    _require_session_started("debugger_go_and_wait_for_core_state")
    _call_debugger("go")
    result = debugger_wait_for_core_state(
        desired_state=int(desired_state),
        core=int(core),
        timeout_ms=int(timeout_ms),
        poll_interval_ms=int(poll_interval_ms),
    )
    wait_data = dict(result.get("data", {}))
    wait_data["started_running"] = True

    err = None
    if bool(wait_data.get("timed_out")):
        err = _error_entry(
            code="TIMEOUT",
            category="timeout",
            message="Desired core state was not reached before timeout.",
            retryable=True,
        )
    return _response_envelope(
        ok=bool(wait_data.get("ok")),
        tool="debugger_go_and_wait_for_core_state",
        data=wait_data,
        error=err,
    )


@mcp.tool()
def debugger_error_taxonomy() -> dict[str, Any]:
    """Return stable machine-readable error codes used by AI-first tool envelopes."""
    codes = [
        {
            "code": "TIMEOUT",
            "category": "timeout",
            "retryable": True,
            "description": "Requested state was not reached within timeout.",
        },
        {
            "code": "TRANSPORT_CONNECTION_RESET",
            "category": "transport",
            "retryable": True,
            "description": "Backend connection was reset unexpectedly.",
        },
        {
            "code": "TRANSPORT_CONNECTION_REFUSED",
            "category": "transport",
            "retryable": True,
            "description": "Backend endpoint refused connection.",
        },
        {
            "code": "SESSION_NOT_STARTED",
            "category": "lifecycle",
            "retryable": False,
            "description": "Operation requires configured+started session.",
        },
        {
            "code": "CAPABILITY_CHECK_FAILED",
            "category": "discovery",
            "retryable": True,
            "description": "A capability probe call failed; other probes may still succeed.",
        },
        {
            "code": "CLEANUP_STEP_FAILED",
            "category": "cleanup",
            "retryable": True,
            "description": "One cleanup sub-step failed.",
        },
        {
            "code": "PARTIAL_CLEANUP_FAILED",
            "category": "cleanup",
            "retryable": True,
            "description": "Cleanup completed with one or more failed sub-steps.",
        },
        {
            "code": "CSPY_KDCLICENSEVIOLATION",
            "category": "backend",
            "retryable": False,
            "description": "Debugger reported kDcLicenseViolation (typically license/token/entitlement issues).",
        },
        {
            "code": "UNKNOWN_ERROR",
            "category": "unknown",
            "retryable": False,
            "description": "Fallback code for unclassified failures.",
        },
    ]
    return _response_envelope(
        ok=True,
        tool="debugger_error_taxonomy",
        data={
            "codes": codes,
            "dc_result_focus": _focused_dc_result_constants(),
            "dc_result_constants": _all_dc_result_constants(),
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
    _require_session_started("debugger_register_snapshot")
    cfg = load_config()

    groups = _call_debugger("getRegisterGroups")
    if not groups:
        return {"group": group, "available_groups": [], "registers": []}

    selected_group = group if group in groups else groups[0]
    names = _call_debugger("getLocationNamesInGroup", selected_group)

    max_items = max(1, int(limit))
    selected_names = names[:max_items]
    named_locations = [_call_debugger("getNamedLocation", name) for name in selected_names]

    memory_thrift = _find_include_thrift(cfg.include_dirs, "memory.thrift")
    memory_mod = load_thrift_module(str(memory_thrift), tuple(cfg.include_dirs))
    memory_service = getattr(memory_mod, "CSpyMemory", None)
    if memory_service is None:
        raise ThriftBridgeError("Service 'CSpyMemory' not found in memory.thrift")

    mem_host, mem_port = resolve_service_endpoint(cfg, "debugger.memory")
    mem_client = make_client(memory_service, mem_host, mem_port, timeout=cfg.timeout_ms)
    try:
        result_items: list[dict[str, Any]] = []
        for named in named_locations:
            full_bits = int(getattr(named, "fullBitSize", 0) or 0)
            bit_size = full_bits if full_bits > 0 else 32
            word_size = max(1, (bit_size + 7) // 8)
            location = getattr(named, "location", None)

            item: dict[str, Any] = {
                "name": getattr(named, "name", ""),
                "alias": getattr(named, "nameAlias", ""),
                "description": getattr(named, "description", ""),
                "bit_size": bit_size,
                "location": to_plain(location),
                "readonly": bool(getattr(named, "readonly", False)),
                "writeonly": bool(getattr(named, "writeonly", False)),
            }

            try:
                raw = mem_client.readMemory(location, 1, 8, word_size)
                raw_bytes = bytes(raw)
                item["raw_hex"] = raw_bytes.hex()
                item["value_unsigned_le"] = int.from_bytes(raw_bytes, byteorder="little", signed=False)
            except Exception as exc:  # noqa: BLE001
                item["read_error"] = str(exc)

            result_items.append(item)
    finally:
        try:
            mem_client.close()
        except Exception:
            pass

    return {
        "group": selected_group,
        "requested_group": group,
        "available_groups": groups,
        "total_in_group": len(names),
        "returned": len(result_items),
        "registers": result_items,
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
    safe_pre_session_methods = {
        "getVersionString",
        "isOnline",
        "resolveLaunchConfiguration",
        "configureSession",
        "startSMPSession",
        "stopSession",
    }
    if method not in safe_pre_session_methods:
        _require_session_started(f"debugger_call({method})")

    try:
        parsed = json.loads(args_json)
    except json.JSONDecodeError as exc:
        raise ThriftBridgeError(f"Invalid JSON in args_json: {exc}") from exc

    try:
        if isinstance(parsed, list):
            result = _call_debugger(method, *parsed)
        elif isinstance(parsed, dict):
            result = _call_debugger(method, **parsed)
        else:
            result = _call_debugger(method, parsed)
    except Exception as exc:
        if method == "configureSession":
            _set_session_state(configured=False, started=False)
            _clear_runtime_session_caches()
        elif method == "startSMPSession":
            _set_session_state(started=False)
        elif method == "stopSession":
            idempotent, details = _should_treat_stop_error_as_idempotent(exc)
            if idempotent:
                _trace_thrift(
                    "debugger_call(stopSession) got DkStop error but probe shows already stopped; "
                    "treating as idempotent success"
                )
                _set_session_state(configured=False, started=False)
                _clear_runtime_session_caches()
                return {
                    "ok": True,
                    "already_stopped": True,
                    "stop_idempotent_recovered": True,
                    "stop_idempotent_details": details,
                }
            _set_session_state(configured=False, started=False)
            _clear_runtime_session_caches()
        raise ThriftBridgeError(_format_cspy_exception_for_humans(exc)) from exc

    if method == "configureSession":
        _clear_runtime_session_caches()
        _set_session_state(configured=True, started=False)
    elif method == "startSMPSession":
        _set_session_state(started=True)
    elif method == "stopSession":
        _set_session_state(configured=False, started=False)
        _clear_runtime_session_caches()

    return to_plain(result)
