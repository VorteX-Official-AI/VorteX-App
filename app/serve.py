"""Serve a project folder on a free localhost port for the live preview."""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVERS: dict[int, dict] = {}
LOCK = threading.Lock()


class PreviewHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        resolved = super().translate_path(path)
        if ".vortexai" in Path(resolved).parts:
            return str(Path(self.directory) / "__vortex_hidden__")
        return resolved

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *args):  # noqa: D401 - keep server logs quiet
        pass


def entry_file(root: str) -> str | None:
    base = Path(root)
    if not base.is_dir():
        return None
    index = base / "index.html"
    if index.is_file():
        return "index.html"
    for candidate in sorted(base.glob("*.html")):
        return candidate.name
    return None


def ensure_server(project_id: int, root: str) -> dict | None:
    """Start (or reuse) a static server for this project and return its URL."""
    entry = entry_file(root)
    if not entry:
        return None

    with LOCK:
        running = SERVERS.get(project_id)
        if running and running["root"] == root and running["thread"].is_alive():
            return {"url": running["url"], "port": running["port"], "entry": running["entry"]}

        if running:
            try:
                running["server"].shutdown()
            except Exception:  # noqa: BLE001
                pass
            SERVERS.pop(project_id, None)

        handler = partial(PreviewHandler, directory=root)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError:
            return None
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}/"
        SERVERS[project_id] = {
            "server": server,
            "thread": thread,
            "root": root,
            "port": port,
            "url": url,
            "entry": entry,
        }
        return {"url": url, "port": port, "entry": entry}


def stop_server(project_id: int) -> None:
    with LOCK:
        running = SERVERS.pop(project_id, None)
    if not running:
        return
    try:
        running["server"].shutdown()
        running["server"].server_close()
    except Exception:  # noqa: BLE001
        pass
