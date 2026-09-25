"""Command line entry point: ``iar-cspy-mcp`` / ``python -m iar_cspy_mcp``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from iar_cspy import Client, Config

from .server import mcp, set_client


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="iar-cspy-mcp",
        description="MCP server exposing the IAR C-SPY debugger (and IDE services) as tools.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Run MCP server in HTTP mode (streamable-http) on 127.0.0.1 by default.",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Port to use with --web (default from MCP_PORT or 8000).",
    )
    parser.add_argument(
        "--iar-path",
        dest="iar_path",
        default=None,
        help=(
            "Path to an IAR installation or build stage, i.e. the directory with "
            "common/bin under it. The programs the server needs are taken from "
            "there, so this is normally the only path you have to give."
        ),
    )
    parser.add_argument(
        "--cspyserver2",
        dest="cspyserver2",
        default=None,
        help="Path to the CSpyServer2 executable, overriding --iar-path.",
    )
    parser.add_argument(
        "--cspyserver2-args",
        dest="cspyserver2_args",
        default=None,
        help="Optional CSpyServer2 args string (default: -standalone -sockets)",
    )
    parser.add_argument(
        "--service-launcher",
        dest="service_launcher",
        default=None,
        help=(
            "Path to the IarServiceLauncher executable, overriding --iar-path. "
            "It hosts the IDE services (ProjectManager, OptionsService) and owns "
            "the service registry that CSpyServer2 then joins."
        ),
    )
    parser.add_argument(
        "--no-ide-services",
        dest="no_ide_services",
        action="store_true",
        help=(
            "Do not host the IDE services: run the debugger alone, without an "
            "IarServiceLauncher. The project_* and options_* tools are then "
            "unavailable."
        ),
    )
    parser.add_argument(
        "--ide-services",
        dest="ide_services",
        default=None,
        help="Comma-separated IDE services to host: projectmanager, options (default: all).",
    )
    parser.add_argument(
        "--probe-cspyserver2",
        action="store_true",
        help="Start the managed backend, print its registry info, then exit.",
    )
    parser.add_argument(
        "--registry-host",
        dest="registry_host",
        default=None,
        help="Connect to an existing backend registry host (standalone mode).",
    )
    parser.add_argument(
        "--registry-port",
        dest="registry_port",
        type=int,
        default=None,
        help="Connect to an existing backend registry port (standalone mode).",
    )
    parser.add_argument(
        "--registry-service",
        dest="registry_service",
        default=None,
        help="Service name to resolve from registry (default: debugger).",
    )
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace, environ: dict[str, str] | None = None) -> Config:
    """The backend Config for the command line on top of the environment."""
    env = dict(os.environ if environ is None else environ)

    if args.iar_path:
        env["IAR_INSTALL_PATH"] = args.iar_path
    if args.service_launcher:
        env["THRIFT_SERVICE_LAUNCHER_EXE"] = args.service_launcher
    if args.no_ide_services:
        env["THRIFT_HOST_IDE_SERVICES"] = "0"
    if args.ide_services is not None:
        env["THRIFT_IDE_SERVICES"] = args.ide_services

    if args.iar_path or args.cspyserver2 or args.service_launcher:
        env["THRIFT_CSPYSERVER_MODE"] = "managed"
        # Stale standalone-mode registry settings must not pin managed startup
        # to an old port. A fixed registry is still possible with
        # --cspyserver2-args "... -registry <port>".
        env.pop("THRIFT_REGISTRY_PORT", None)
        env.pop("THRIFT_REGISTRY_HOST", None)
    if args.cspyserver2:
        env["THRIFT_CSPYSERVER_EXE"] = args.cspyserver2
    if args.cspyserver2_args:
        env["THRIFT_CSPYSERVER_ARGS"] = args.cspyserver2_args

    if args.registry_host is not None:
        env["THRIFT_CSPYSERVER_MODE"] = "standalone"
        env["THRIFT_REGISTRY_HOST"] = str(args.registry_host)
    if args.registry_port is not None:
        env["THRIFT_CSPYSERVER_MODE"] = "standalone"
        env["THRIFT_REGISTRY_PORT"] = str(int(args.registry_port))
    if args.registry_service is not None:
        env["THRIFT_REGISTRY_SERVICE"] = str(args.registry_service)

    return Config.from_env(env)


class _TraceFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        return f"[{ts}] [thrift] {record.getMessage()}"


def _enable_rpc_trace() -> None:
    """Trace every backend RPC to stderr, where MCP hosts collect server logs."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TraceFormatter())
    logger = logging.getLogger("iar_cspy.rpc")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    config = config_from_args(args)
    client = Client(config, owns_backend=True)
    set_client(client)
    _enable_rpc_trace()

    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if args.web:
        transport = "streamable-http"
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        if args.web_port is not None:
            mcp.settings.port = int(args.web_port)

    if config.mode == "managed":
        host, port = client.backend.start() or ("", 0)
        if args.probe_cspyserver2:
            print(f"Managed CSpyServer2 ready at {host}:{port}")
            print(f"Status: {client.backend.status()}")
            client.close()
            return

    mount_path = os.getenv("MCP_MOUNT_PATH")
    if transport == "stdio":
        print("MCP server ready (stdio). Waiting for an MCP client connection...", file=sys.stderr, flush=True)

    # uvicorn captures SIGTERM, shuts the server down, restores whatever handler
    # was installed beforehand and then re-raises the signal. With the default
    # disposition restored that kills the process outright, so neither the
    # finally below nor the atexit hooks ever run and the backend processes are
    # orphaned. Installing our own handler first means the one uvicorn restores
    # is this one, so the re-raise lands here and the backends get torn down.
    def _terminate(signum: int, _frame: object) -> None:
        client.close()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError):
        pass  # not the main thread, or unsupported on this platform

    try:
        mcp.run(transport=transport, mount_path=mount_path)
    finally:
        # Covers the exit paths that unwind normally, including Ctrl-C;
        # _terminate above covers SIGTERM. Both are idempotent.
        client.close()


if __name__ == "__main__":
    main()
