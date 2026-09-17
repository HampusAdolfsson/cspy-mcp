import argparse
import os
import sys

from .config import load_config
from .cspy_server_manager import ensure_managed_server, managed_server_status, shutdown_managed_server
from .server import mcp


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MCP Thrift server")
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
        "--cspyserver2",
        dest="cspyserver2",
        default=None,
        help="Path to CSpyServer2 executable. Enables managed mode when provided.",
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
            "Path to IarServiceLauncher. Enables launcher mode: the launcher owns "
            "the service registry and hosts the IDE services (ProjectManager, "
            "OptionsService), and a CSpyServer2 given via --cspyserver2 joins that "
            "same registry."
        ),
    )
    parser.add_argument(
        "--ide-services",
        dest="ide_services",
        default=None,
        help=(
            "Comma-separated IDE services to host: projectmanager, options "
            "(default: all)."
        ),
    )
    parser.add_argument(
        "--probe-cspyserver2",
        action="store_true",
        help="Start managed CSpyServer2, print parsed registry info, then exit.",
    )
    parser.add_argument(
        "--registry-host",
        dest="registry_host",
        default=None,
        help="Connect to an existing backend registry host (external mode).",
    )
    parser.add_argument(
        "--registry-port",
        dest="registry_port",
        type=int,
        default=None,
        help="Connect to an existing backend registry port (external mode).",
    )
    parser.add_argument(
        "--registry-service",
        dest="registry_service",
        default=None,
        help="Service name to resolve from registry (default: debugger).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.web:
        os.environ["MCP_TRANSPORT"] = "streamable-http"
        os.environ.setdefault("MCP_HOST", "127.0.0.1")
        if args.web_port is not None:
            os.environ["MCP_PORT"] = str(int(args.web_port))

    if args.service_launcher:
        os.environ["THRIFT_CSPYSERVER_MODE"] = "launcher"
        os.environ["THRIFT_SERVICE_LAUNCHER_EXE"] = args.service_launcher
        os.environ.pop("THRIFT_REGISTRY_PORT", None)
        os.environ.pop("THRIFT_REGISTRY_HOST", None)
    if args.ide_services is not None:
        os.environ["THRIFT_IDE_SERVICES"] = args.ide_services

    if args.cspyserver2:
        # Launcher mode already implies a managed CSpyServer2, joining the
        # launcher's registry rather than starting one of its own.
        if not args.service_launcher:
            os.environ["THRIFT_CSPYSERVER_MODE"] = "managed"
        os.environ["THRIFT_CSPYSERVER_EXE"] = args.cspyserver2
        # Avoid stale external-mode registry env vars pinning managed startup
        # to an old/conflicting port. Managed mode can still use fixed registry
        # by passing --cspyserver2-args "... -registry <port>".
        if not args.service_launcher:
            os.environ.pop("THRIFT_REGISTRY_PORT", None)
            os.environ.pop("THRIFT_REGISTRY_HOST", None)
    if args.cspyserver2_args:
        os.environ["THRIFT_CSPYSERVER_ARGS"] = args.cspyserver2_args

    if args.registry_host is not None:
        os.environ["THRIFT_CSPYSERVER_MODE"] = "external"
        os.environ["THRIFT_REGISTRY_HOST"] = str(args.registry_host)
    if args.registry_port is not None:
        os.environ["THRIFT_CSPYSERVER_MODE"] = "external"
        os.environ["THRIFT_REGISTRY_PORT"] = str(int(args.registry_port))
    if args.registry_service is not None:
        os.environ["THRIFT_REGISTRY_SERVICE"] = str(args.registry_service)

    cfg = load_config()
    if cfg.cspy_mode in {"managed", "launcher"}:
        host, port = ensure_managed_server(cfg)
        if args.probe_cspyserver2:
            status = managed_server_status()
            print(f"Managed CSpyServer2 ready at {host}:{port}")
            print(f"Status: {status}")
            shutdown_managed_server()
            return

    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mount_path = os.getenv("MCP_MOUNT_PATH")
    if transport == "stdio":
        print(
            "MCP server ready (stdio). Waiting for an MCP client connection...",
            file=sys.stderr,
            flush=True,
        )
    mcp.run(transport=transport, mount_path=mount_path)


if __name__ == "__main__":
    main()
