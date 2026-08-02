"""Desktop entry point for the locally hosted Prisma generator."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import socket
import subprocess
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
    GENERATOR_APPLIED_ENV,
    RuntimeLayoutError,
    apply_generator_environment,
    initialize_generator_runtime,
    prepare_generator_runtime,
    resolve_runtime_layout,
)
from Prisma.lib.console_output import configure_console_streams  # noqa: E402
from Prisma.lib.export_stage_recovery import reconcile_interrupted_export_stages  # noqa: E402
from Prisma.lib.workspace_lock import WorkspaceLock, WorkspaceLockError  # noqa: E402
from Prisma.generator.cache_admin import safe_clear_dir_except  # noqa: E402


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010
PORT_SEARCH_COUNT = 20
HEALTH_PATH = "/api/system/health"
RESTART_EXIT_CODE = 75
_FROZEN_SHARED_MODULES = (
    "lib.camera_transform",
    "lib.transmission",
    "lib.stack_geometry",
    "lib.photo_stack_model.correction_layer",
    "lib.photo_stack_model.predictor",
)


class LauncherError(RuntimeError):
    """Raised for an actionable launcher failure."""


def default_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def _server_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}"


def _probe_prisma_generator(url: str, *, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + HEALTH_PATH, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("ok") is True
        and payload.get("app") == "prisma-generator"
    )


def _clear_startup_generator_cache(workspace_root: Path) -> dict:
    """Remove session-temporary Generator data while retaining source conversions."""

    cache_root = Path(workspace_root) / "cache"
    started = time.monotonic()
    report = safe_clear_dir_except(
        cache_root,
        root=cache_root,
        preserve_names={"source-images"},
        retries=1,
    )
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    return report


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
    raise LauncherError(f"No available local port was found from {preferred_port} through {last_port}.")


def _guided_setup_test_url(url: str, *, force_guided_setup: bool) -> str:
    if not force_guided_setup:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}force-guided-setup=1"


def _open_browser_when_ready(
    server_url: str,
    *,
    browser_url: str | None = None,
    timeout_seconds: float = 30.0,
) -> None:
    destination = browser_url or server_url
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _probe_prisma_generator(server_url):
            if not webbrowser.open(destination):
                print(f"Prisma is ready. Open this address in your browser: {destination}", flush=True)
            return
        time.sleep(0.1)
    print(f"Prisma did not become ready in {timeout_seconds:.0f} seconds. Check this window for errors.", flush=True)


def _load_generator_server() -> ModuleType:
    try:
        if getattr(sys, "frozen", False):
            for module_name in _FROZEN_SHARED_MODULES:
                importlib.import_module(module_name)
        return importlib.import_module("Prisma.generator.server")
    except Exception as exc:
        raise LauncherError(f"The Prisma generator could not be loaded: {exc}") from exc


def _spawn_restarted_process() -> None:
    """Start a fresh launcher after this process has released its Workspace lock."""

    if getattr(sys, "frozen", False):
        command = [sys.executable, *sys.argv[1:]]
    else:
        command = [sys.executable, sys.argv[0], *sys.argv[1:]]
    environment = os.environ.copy()
    for name in GENERATOR_APPLIED_ENV:
        environment.pop(name, None)
    subprocess.Popen(command, env=environment)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=None, help="Maintainer override for the packaged app root.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Start Prisma without opening the browser.")
    parser.add_argument(
        "--force-guided-setup",
        action="store_true",
        help="Developer option: show Guided Setup without changing saved first-launch state.",
    )
    return parser


def run_generator(
    *,
    app_root: Path,
    host: str,
    preferred_port: int,
    open_browser: bool,
    force_guided_setup: bool = False,
) -> int:
    if host not in {"127.0.0.1", "localhost"}:
        raise LauncherError("The desktop launcher only permits the local host (127.0.0.1).")
    if not 1 <= preferred_port <= 65535:
        raise LauncherError(f"Invalid port: {preferred_port}")

    preferred_url = _server_url(host, preferred_port)
    if _probe_prisma_generator(preferred_url):
        print(f"Prisma is already running at {preferred_url}", flush=True)
        browser_url = _guided_setup_test_url(
            preferred_url,
            force_guided_setup=force_guided_setup,
        )
        if open_browser:
            webbrowser.open(browser_url)
        elif force_guided_setup:
            print(f"Guided Setup test address: {browser_url}", flush=True)
        return 0

    try:
        layout = resolve_runtime_layout(
            app_root=app_root,
            allow_environment_overrides=not bool(getattr(sys, "frozen", False)),
        )
        prepare_generator_runtime(layout)
    except RuntimeLayoutError as exc:
        raise LauncherError(str(exc)) from exc

    workspace_lock = WorkspaceLock(layout.generator_workspace_root, owner="generator")
    try:
        try:
            workspace_lock.acquire()
        except WorkspaceLockError as exc:
            if exc.owner_url and _probe_prisma_generator(exc.owner_url):
                print(f"Prisma is already running at {exc.owner_url}", flush=True)
                browser_url = _guided_setup_test_url(
                    exc.owner_url,
                    force_guided_setup=force_guided_setup,
                )
                if open_browser:
                    webbrowser.open(browser_url)
                elif force_guided_setup:
                    print(f"Guided Setup test address: {browser_url}", flush=True)
                return 0
            raise LauncherError(str(exc)) from exc

        export_recovery = reconcile_interrupted_export_stages(layout.generator_exports_root)
        removed_export_stages = list(export_recovery.get("removed") or [])
        if removed_export_stages:
            print(f"Recovered {len(removed_export_stages)} interrupted export stage(s).", flush=True)
        for finding in export_recovery.get("findings") or []:
            print(
                f"Export recovery preserved {finding.get('path')}: "
                f"{finding.get('status')}{' — ' + finding.get('error') if finding.get('error') else ''}",
                flush=True,
            )

        try:
            cache_report = _clear_startup_generator_cache(
                layout.generator_workspace_root
            )
            print(
                "Cleared "
                f"{cache_report.get('removed', 0)} temporary cache entr"
                f"{'y' if cache_report.get('removed', 0) == 1 else 'ies'} "
                f"in {cache_report.get('elapsed_s', 0):.3f}s; "
                "preserved prepared source images.",
                flush=True,
            )
            for failure in cache_report.get("failures") or []:
                print(
                    "Warning: Prisma could not remove temporary cache entry "
                    f"{failure.get('path')}: {failure.get('error')}",
                    file=sys.stderr,
                    flush=True,
                )
        except (OSError, ValueError) as exc:
            print(
                f"Warning: Prisma could not complete startup cache cleanup: {exc}",
                file=sys.stderr,
                flush=True,
            )

        try:
            report = initialize_generator_runtime(layout, prepare_folders=False)
            apply_generator_environment(
                layout,
                active_model_library_root=report["active_model_library_root"],
                model_library_available=bool(report["model_library_available"]),
                active_library_id=report.get("active_library_id"),
                model_library_error=report.get("model_library_error"),
            )
        except RuntimeLayoutError as exc:
            raise LauncherError(str(exc)) from exc

        port, listener = _reserve_port(host, preferred_port)
        url = _server_url(host, port)
        browser_url = _guided_setup_test_url(
            url,
            force_guided_setup=force_guided_setup,
        )
        workspace_lock.update_metadata(url=url)
        try:
            generator_server = _load_generator_server()
            try:
                import uvicorn
            except ImportError as exc:
                raise LauncherError("The bundled web-server runtime is missing.") from exc

            library_version = str((report.get("model_library") or {}).get("library_version") or "unavailable")
            print("Prisma is starting...", flush=True)
            print(f"Model Library: {library_version}", flush=True)
            if not report.get("model_library_available"):
                print(f"Library Recovery Mode: {report.get('model_library_error')}", flush=True)
            print(f"Address: {url}", flush=True)
            if force_guided_setup:
                print(f"Guided Setup test address: {browser_url}", flush=True)
            print(f"Images: {layout.generator_images_root}", flush=True)
            print(f"Exports: {layout.generator_exports_root}", flush=True)
            print("Keep this window open while using Prisma. Press Ctrl+C or close it to stop.", flush=True)
            if port != preferred_port:
                print(f"Port {preferred_port} was busy, so Prisma selected port {port}.", flush=True)

            if open_browser:
                threading.Thread(
                    target=_open_browser_when_ready,
                    args=(url,),
                    kwargs={"browser_url": browser_url},
                    name="prisma-browser-opener",
                    daemon=True,
                ).start()

            config = uvicorn.Config(
                generator_server.app,
                host=host,
                port=port,
                log_level="info",
                access_log=True,
            )
            uvicorn_server = uvicorn.Server(config)
            restart_requested = threading.Event()

            def request_restart() -> None:
                restart_requested.set()
                uvicorn_server.should_exit = True

            generator_server.configure_restart_callback(request_restart)
            try:
                uvicorn_server.run(sockets=[listener])
            finally:
                generator_server.configure_restart_callback(None)
            return RESTART_EXIT_CODE if restart_requested.is_set() else 0
        finally:
            listener.close()
            logging.shutdown()
    finally:
        workspace_lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_streams()
    args = _parser().parse_args(argv)
    try:
        result = run_generator(
            app_root=(args.app_root or default_app_root()).expanduser().resolve(),
            host=str(args.host),
            preferred_port=int(args.port),
            open_browser=not args.no_browser,
            force_guided_setup=bool(args.force_guided_setup),
        )
        if result == RESTART_EXIT_CODE:
            _spawn_restarted_process()
            return 0
        return result
    except (LauncherError, OSError) as exc:
        print("\nPrisma could not start.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nPrisma stopped.", flush=True)
        return 0


if __name__ == "__main__":
    exit_code = main()
    if exit_code and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
