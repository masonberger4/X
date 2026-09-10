"""CLI: the control panel as a desktop window, with no browser and no command prompt.

Usage: pythonw run_desktop.py [--host 127.0.0.1] [--port 0] [--no-window]

Starts the same server run_app.py starts, then opens it in a native window (pywebview:
Edge on Windows). Closing the window stops the server and any run in progress. --port 0
(the default) picks a free port, so it never clashes with a run_app.py already on 8000;
give a fixed --port
and --host 0.0.0.0 to reach the same window from a phone (HOWTO part 8). --no-window
starts the server, prints its address and waits: for a headless check, or to open the
page in a second browser as well.

In the PyInstaller build (deploy/desktop.spec) this is Pipeline.exe. It runs from the
folder the exe is in, which is where .env, pipeline.db, backups and desktop.log live.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from panel.frozen import data_dir, is_frozen

log = logging.getLogger("run_desktop")

WINDOW_TITLE = "Pipeline"
WINDOW_SIZE = (1200, 850)
START_TIMEOUT = 15.0


def configure_logging() -> None:
    """A windowed process has no console: log to desktop.log beside the data instead."""
    if is_frozen() or sys.stderr is None:
        logging.basicConfig(
            filename=str(data_dir() / "desktop.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(level=logging.INFO)


def pick_port(host: str, port: int) -> int:
    """`port` itself, or a free one when it is 0."""
    if port:
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def start_server(host: str, port: int):
    """Serve panel.app in a background thread; returns (server, thread) once it is up."""
    import uvicorn

    from panel.app import app

    # log_config=None: uvicorn's default logging assumes a console, which a windowed
    # process does not have; the root logger set up above catches its messages.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("the server stopped before it started; see the log")
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(f"the server did not start within {START_TIMEOUT:.0f}s")
    return server, thread


INSTALL_HINT = (
    'pywebview is not installed. Run:  pip install -e ".[desktop]"  (once), then try again.'
)


def window_available() -> bool:
    """Whether the native window can open. Checked before the server starts, so a missing
    install is one clear line rather than a started server and a traceback."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


def open_window(url: str) -> None:
    """Block in the native window until it is closed."""
    import webview

    webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1])
    webview.start()


def wait_forever(thread: threading.Thread) -> None:
    thread.join()


def main(argv: list[str] | None = None) -> int:
    if is_frozen():
        os.chdir(data_dir())  # .env, pipeline.db, the lock and backups are relative paths
    configure_logging()
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1", help="bind address (localhost only by default)")
    ap.add_argument("--port", type=int, default=0, help="port; 0 picks a free one")
    ap.add_argument("--no-window", action="store_true", help="serve only; print the address")
    args = ap.parse_args(argv)

    if not args.no_window and not window_available():
        log.error(INSTALL_HINT)
        print(INSTALL_HINT, file=sys.stderr or sys.stdout)
        return 1
    port = pick_port(args.host, args.port)
    try:
        server, thread = start_server(args.host, port)
    except Exception as exc:
        log.error("could not start: %s", exc)
        return 1
    shown_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{shown_host}:{port}"
    log.info("control panel at %s (data in %s)", url, Path.cwd())
    try:
        if args.no_window:
            print(url, flush=True)
            wait_forever(thread)
        else:
            open_window(url)
    finally:
        stop_run()
        server.should_exit = True
        thread.join(timeout=5)
    return 0


def stop_run() -> None:
    """Closing the window must not leave a step (and the claude CLI it launches) running
    on its own; the operator would keep seeing its windows with no way to stop it."""
    from panel.app import JOBS

    if JOBS.cancel("stopped: the app was closed"):
        log.info("a run was in progress; its step was stopped")


if __name__ == "__main__":
    sys.exit(main())
