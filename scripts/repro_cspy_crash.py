from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path

from mcp_thrift_server import server
from mcp_thrift_server.config import load_config
from mcp_thrift_server.cspy_server_manager import managed_server_status


def _launch_json() -> str:
    launch_file = Path(__file__).resolve().parents[1] / "tests" / "live_assets" / "launch.json"
    raw = json.loads(launch_file.read_text(encoding="utf-8"))
    cfg = raw["configurations"][0]
    for key in ("program", "projectPath"):
        value = cfg.get(key)
        if value and not Path(value).is_absolute():
            cfg[key] = str((launch_file.parent / value).resolve())
    return json.dumps(cfg)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reproduce CSpyServer2 crash/reset during configure/start")
    parser.add_argument("--loops", type=int, default=40, help="Number of cycles to run")
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop immediately on first failure",
    )
    parser.add_argument(
        "--sleep-s",
        type=float,
        default=0.05,
        help="Delay between cycles in seconds",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    launch_json = _launch_json()
    loops = max(1, int(args.loops))
    failures = 0

    print(f"[repro] starting {loops} configure/start/stop cycles")

    for i in range(1, loops + 1):
        print(f"[repro] cycle {i} begin")
        try:
            out = server.debugger_configure_and_start_session(launch_json)
            print(f"[repro] cycle {i} start ok={out.get('ok')}")

            stop = server.debugger_call("stopSession", "[]")
            print(f"[repro] cycle {i} stopSession result={stop}")

        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[repro] cycle {i} FAILURE: {type(exc).__name__}: {exc}")
            print("[repro] traceback:")
            traceback.print_exc()

            # Snapshot server perspective immediately after failure.
            try:
                status = server.debugger_session_status()
                print(f"[repro] session_status={status}")
            except Exception as status_exc:  # noqa: BLE001
                print(f"[repro] session_status failed: {status_exc}")

            try:
                caps = server.debugger_capabilities()
                print(f"[repro] capabilities={caps}")
            except Exception as cap_exc:  # noqa: BLE001
                print(f"[repro] capabilities failed: {cap_exc}")

            try:
                cfg = load_config()
                if cfg.cspy_mode == "managed":
                    ms = managed_server_status()
                    print(f"[repro] managed_server_status={ms}")
                else:
                    print("[repro] managed_server_status=<skipped in standalone mode>")
            except Exception as ms_exc:  # noqa: BLE001
                print(f"[repro] managed_server_status failed: {ms_exc}")

            if args.stop_on_failure:
                break

        time.sleep(max(0.0, float(args.sleep_s)))

    print(f"[repro] done. failures={failures}/{loops}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
