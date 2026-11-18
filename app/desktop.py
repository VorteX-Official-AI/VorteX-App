"""VorteX AI desktop launcher.

    python app.py

Starts the local FastAPI server and opens the workspace in a native window
(Edge WebView2 on Windows, WebKit/GTK elsewhere through pywebview). Falls back
to an Edge/Chrome app window, then to the default browser.
"""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
FROZEN = bool(getattr(sys, "frozen", False))
VENV_PYTHON = BASE_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DEFAULT_PORT = int(os.environ.get("VORTEX_PORT", "8000"))
WINDOW_TITLE = "VorteX AI"
WINDOW_SIZE = (1440, 900)


class DesktopApi:
    """Track active model work and guard the native window while it runs."""

    def __init__(self) -> None:
        self._window = None
        self._busy = False
        self._allow_close = False
        self._lock = threading.Lock()

    def set_busy(self, busy: bool) -> bool:
        with self._lock:
            self._busy = bool(busy)
        return True

    def close_app(self) -> bool:
        with self._lock:
            self._allow_close = True
        window = self._window
        if window is None:
            return False
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - the window may already be closing
            with self._lock:
                self._allow_close = False
            return False
        return True

    def _on_closing(self) -> bool:
        # pywebview cancels a close when a closing handler returns False.
        with self._lock:
            if self._allow_close or not self._busy:
                return True
        self._prompt_close()
        return False

    def _prompt_close(self) -> None:
        window = self._window
        if window is None:
            return

        def show() -> None:
            with self._lock:
                if not self._busy:
                    return
            try:
                window.evaluate_js(
                    "window.vortexShowClosePrompt && window.vortexShowClosePrompt()"
                )
            except Exception:  # noqa: BLE001 - the page may be gone already
                pass

        threading.Thread(target=show, name="vortex-close-prompt", daemon=True).start()


def ensure_streams() -> None:
    """A windowed build has no console; keep logs in the user data folder."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(DATA_DIR / "desktop.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def relaunch_in_venv() -> None:
    """Use the project's virtualenv when started with another interpreter."""
    if FROZEN:
        return
    try:
        if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
            completed = subprocess.run(
                [str(VENV_PYTHON), str(BASE_DIR / "app.py"), *sys.argv[1:]]
            )
            sys.exit(completed.returncode)
    except OSError as exc:
        print(f"Could not switch to the project virtualenv: {exc}")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as response:
            return b"VorteX" in response.read(200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_free_port(port: int, seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_free(port):
            return True
        time.sleep(0.4)
    return False


def pick_port() -> tuple[int, bool]:
    """Return (port, reuse_running_server)."""
    if healthy(DEFAULT_PORT):
        return DEFAULT_PORT, True
    if port_free(DEFAULT_PORT) or wait_for_free_port(DEFAULT_PORT):
        return DEFAULT_PORT, False
    for candidate in range(DEFAULT_PORT + 1, DEFAULT_PORT + 40):
        if port_free(candidate):
            return candidate, False
    raise RuntimeError("No free local port found for the app.")


def start_server(port: int) -> threading.Thread:
    import uvicorn

    from .main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="vortex-server", daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline:
        if healthy(port):
            return thread
        if not thread.is_alive():
            break
        time.sleep(0.25)
    raise RuntimeError("The local server did not start. Check the console for errors.")


def open_native_window(url: str, smoke_seconds: float | None = None) -> bool:
    try:
        import webview
    except ImportError:
        return False

    api = DesktopApi()
    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=(980, 660),
        background_color="#0a0a0b",
        text_select=True,
        js_api=api,
    )
    api._window = window
    window.events.closing += api._on_closing

    if smoke_seconds:
        def report_load() -> None:
            try:
                print("window loaded:", window.evaluate_js("document.title"))
                print("window url:", window.evaluate_js("location.pathname"))
            except Exception as exc:  # noqa: BLE001
                print("window probe failed:", exc)

        window.events.loaded += report_load

        def close_later() -> None:
            time.sleep(smoke_seconds)
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=close_later, daemon=True).start()

    try:
        webview.start(
            gui="edgechromium" if os.name == "nt" else None,
            private_mode=False,
            storage_path=str(DATA_DIR / "webview"),
        )
    except Exception as exc:  # noqa: BLE001 - fall back to a browser window
        print(f"Native window unavailable ({exc}); opening an app window instead.")
        return False
    return True


def open_app_window(url: str) -> None:
    """Browser-based fallback: a chromeless Edge/Chrome app window."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for browser in candidates:
        if browser.is_file():
            subprocess.Popen(
                [
                    str(browser),
                    f"--app={url}",
                    f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
                ]
            )
            return
    import webbrowser

    webbrowser.open(url)


def main() -> int:
    ensure_streams()
    os.environ.setdefault("VORTEX_DESKTOP", "1")
    parser = argparse.ArgumentParser(description="Run VorteX AI as a desktop app.")
    parser.add_argument("--browser", action="store_true", help="open in the default browser")
    parser.add_argument("--server-only", action="store_true", help="run the local server without a window")
    parser.add_argument("--port", type=int, default=None, help="preferred local port")
    parser.add_argument("--smoke", type=float, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    relaunch_in_venv()

    if args.port:
        globals()["DEFAULT_PORT"] = args.port

    # An elevated relaunch may race the old instance shutting down.
    if not args.server_only:
        time.sleep(0.2)
    try:
        port, reused = pick_port()
        if reused:
            print(f"VorteX AI is already running - attaching to http://127.0.0.1:{port}")
        else:
            start_server(port)
            print(f"VorteX AI is running at http://127.0.0.1:{port}")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start VorteX AI: {exc}")
        return 1

    url = f"http://127.0.0.1:{port}/"

    if args.server_only:
        print("Server-only mode. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    try:
        if args.browser:
            import webbrowser

            webbrowser.open(url)
            print("Opened in your browser. Press Ctrl+C to stop the server.")
            while True:
                time.sleep(1)
        if not open_native_window(url, args.smoke):
            open_app_window(url)
            print("Close the app window to stop the server.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    return 0
