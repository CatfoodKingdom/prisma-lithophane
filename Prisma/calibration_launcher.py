"""Desktop entry point for the locally hosted Prisma Calibration app."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from types import ModuleType
from typing import Sequence


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from Prisma.lib.runtime_layout import (  # noqa: E402
    RuntimeLayoutError,
    apply_calibration_environment,
    initialize_calibration_runtime,
    prepare_calibration_runtime,
    resolve_runtime_layout,
)
from Prisma.lib.console_output import configure_console_streams  # noqa: E402
from Prisma.lib.workspace_lock import WorkspaceLock, WorkspaceLockError  # noqa: E402


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8016
PORT_SEARCH_COUNT = 20
HEALTH_PATH = "/api/system/health"
BLANK_SCHEMA_NAME = "blank_calibration_schema.sql"
_FROZEN_SHARED_MODULES = (
    "fitting.model_fit_workflow",
    "lib.camera_transform",
    "lib.transmission",
    "lib.photo_stack_model.artifacts",
)


class CalibrationLauncherError(RuntimeError):
    """Raised for an actionable Calibration launcher failure."""


def default_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def bundled_schema_path() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        return bundle_root / "Prisma" / "calibration" / BLANK_SCHEMA_NAME
    return SOURCE_ROOT / "Prisma" / "calibration" / BLANK_SCHEMA_NAME


def _server_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}"


def _probe_prisma_calibration(
    url: str,
    *,
    timeout: float = 0.5,
    expected_app_root: Path | None = None,
) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + HEALTH_PATH, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if expected_app_root is not None:
        actual_app_root = str(payload.get("app_root") or "").strip()
        if not actual_app_root:
            return False
        try:
            if Path(actual_app_root).expanduser().resolve() != Path(expected_app_root).expanduser().resolve():
                return False
        except OSError:
            return False
    return bool(
        payload.get("ok") is True
        and payload.get("app") == "prisma-calibration"
    )


def _reserve_port(host: str, preferred_port: int, *, attempts: int = PORT_SEARCH_COUNT) -> tuple[int, socket.socket]:
    last_port = preferred_port + attempts - 1
    for port in range(preferred_port, preferred_port + attempts):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:  # pragma: no cover - Windows is the release target
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(128)
            return port, listener
        except OSError:
            listener.close()
    raise CalibrationLauncherError(
        f"No available local port was found from {preferred_port} through {last_port}."
    )


def _open_browser_when_ready(url: str, *, timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _probe_prisma_calibration(url):
            if not webbrowser.open(url):
                print(f"Prisma Calibration is ready. Open this address in your browser: {url}", flush=True)
            return
        time.sleep(0.1)
    print(
        f"Prisma Calibration did not become ready in {timeout_seconds:.0f} seconds. "
        "Check this window for errors.",
        flush=True,
    )


def _load_calibration_server() -> ModuleType:
    try:
        if getattr(sys, "frozen", False):
            for module_name in _FROZEN_SHARED_MODULES:
                importlib.import_module(module_name)
        return importlib.import_module("Prisma.calibration.server")
    except Exception as exc:
        raise CalibrationLauncherError(f"Prisma Calibration could not be loaded: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=None, help="Maintainer override for the packaged app root.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Start Calibration without opening the browser.")
    return parser


def run_calibration(*, app_root: Path, host: str, preferred_port: int, open_browser: bool) -> int:
    if host not in {"127.0.0.1", "localhost"}:
        raise CalibrationLauncherError("The desktop launcher only permits the local host (127.0.0.1).")
    if not 1 <= preferred_port <= 65535:
        raise CalibrationLauncherError(f"Invalid port: {preferred_port}")

    preferred_url = _server_url(host, preferred_port)
    if _probe_prisma_calibration(preferred_url, expected_app_root=app_root):
        print(f"Prisma Calibration is already running at {preferred_url}", flush=True)
        if open_browser:
            webbrowser.open(preferred_url)
        return 0

    try:
        layout = resolve_runtime_layout(
            app_root=app_root,
            allow_environment_overrides=not bool(getattr(sys, "frozen", False)),
        )
        prepare_calibration_runtime(layout)
    except RuntimeLayoutError as exc:
        raise CalibrationLauncherError(str(exc)) from exc

    workspace_lock = WorkspaceLock(layout.calibration_workspace_root, owner="calibration")
    try:
        try:
            workspace_lock.acquire()
        except WorkspaceLockError as exc:
            if exc.owner_url and _probe_prisma_calibration(exc.owner_url):
                print(f"Prisma Calibration is already running at {exc.owner_url}", flush=True)
                if open_browser:
                    webbrowser.open(exc.owner_url)
                return 0
            raise CalibrationLauncherError(str(exc)) from exc

        try:
            report = initialize_calibration_runtime(
                layout,
                schema_path=bundled_schema_path(),
                prepare_folders=False,
            )
            apply_calibration_environment(layout)
        except RuntimeLayoutError as exc:
            raise CalibrationLauncherError(str(exc)) from exc

        port, listener = _reserve_port(host, preferred_port)
        url = _server_url(host, port)
        workspace_lock.update_metadata(url=url)
        try:
            calibration_server = _load_calibration_server()
            calibration_server._SERVER_HOST = host
            try:
                import uvicorn
            except ImportError as exc:
                raise CalibrationLauncherError("The bundled web-server runtime is missing.") from exc

            print("Prisma Calibration is starting...", flush=True)
            if report["created_blank_database"]:
                print("Created a new empty Calibration Workspace.", flush=True)
            print(f"Address: {url}", flush=True)
            print(f"Image Inbox: {layout.calibration_inbox_root}", flush=True)
            print(f"Generated Steps: {layout.calibration_steps_root}", flush=True)
            print(f"Backups: {layout.calibration_backups_root}", flush=True)
            print(f"Published Models: {layout.calibration_published_models_root}", flush=True)
            print("Keep this window open while using Calibration. Press Ctrl+C or close it to stop.", flush=True)
            if port != preferred_port:
                print(
                    f"Port {preferred_port} was busy, so Prisma Calibration selected port {port}.",
                    flush=True,
                )

            if open_browser:
                threading.Thread(
                    target=_open_browser_when_ready,
                    args=(url,),
                    name="prisma-calibration-browser-opener",
                    daemon=True,
                ).start()

            config = uvicorn.Config(
                calibration_server.app,
                host=host,
                port=port,
                log_level="info",
                access_log=True,
            )
            uvicorn.Server(config).run(sockets=[listener])
            return 0
        finally:
            listener.close()
            logging.shutdown()
    finally:
        workspace_lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_streams()
    args = _parser().parse_args(argv)
    try:
        return run_calibration(
            app_root=(args.app_root or default_app_root()).expanduser().resolve(),
            host=str(args.host),
            preferred_port=int(args.port),
            open_browser=not args.no_browser,
        )
    except (CalibrationLauncherError, OSError) as exc:
        print("\nPrisma Calibration could not start.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nPrisma Calibration stopped.", flush=True)
        return 0


if __name__ == "__main__":
    exit_code = main()
    if exit_code and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
