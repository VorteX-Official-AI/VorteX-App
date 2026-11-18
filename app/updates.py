"""Update channel for the desktop build.

A release is published as a tiny JSON manifest (or a GitHub "latest release"
payload) at ``VORTEX_UPDATE_URL``. The app checks that channel, and when a
newer build exists the workspace pops up a message with the release notes, an
animated in-app download and a manual download link.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA_DIR
from .version import __version__

APP_VERSION = __version__

DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/vortex-ai/vortex-ai/main/updates/latest.json"
)
MANIFEST_URL = (os.environ.get("VORTEX_UPDATE_URL") or DEFAULT_MANIFEST_URL).strip()
CHECK_TTL = 30 * 60
REQUEST_TIMEOUT = 8
CHUNK = 256 * 1024

UPDATE_DIR = DATA_DIR / "updates"

_CACHE: dict = {"at": 0.0, "data": None, "error": ""}
_CACHE_LOCK = threading.Lock()

_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD: dict = {
    "state": "idle",
    "percent": 0.0,
    "received": 0,
    "total": 0,
    "speed": 0.0,
    "eta": 0,
    "file": "",
    "path": "",
    "version": "",
    "url": "",
    "error": "",
    "indeterminate": False,
    "started_at": 0.0,
    "finished_at": 0.0,
}
_CANCEL = threading.Event()
_THREAD: threading.Thread | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def platform_key() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def pretty_size(value: int | float | None) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}")
        size /= 1024
    return "—"


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    left, right = _version_tuple(latest), _version_tuple(current)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


def _read_source(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("", "file"):
        path = Path(urllib.request.url2pathname(parsed.path)) if parsed.scheme == "file" else Path(url)
        return path.expanduser().read_bytes()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"VorteX AI/{APP_VERSION}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read(2 * 1024 * 1024)


def _notes(value) -> list[str]:
    if isinstance(value, str):
        rows = [line.strip(" -•\t") for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        rows = [str(item).strip(" -•\t") for item in value]
    else:
        rows = []
    return [row for row in rows if row][:12]


def _download_entry(value) -> dict | None:
    if isinstance(value, str) and value.strip():
        return {"url": value.strip(), "size": 0, "name": ""}
    if isinstance(value, dict):
        url = str(value.get("url") or value.get("browser_download_url") or "").strip()
        if not url:
            return None
        try:
            size = int(value.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return {"url": url, "size": size, "name": str(value.get("name") or "").strip()}
    return None


def _normalise(payload: dict) -> dict:
    """Read either our manifest format or a GitHub release payload."""
    if "assets" in payload and ("tag_name" in payload or "name" in payload):
        latest = str(payload.get("tag_name") or payload.get("name") or "").strip()
        notes = _notes(payload.get("body"))
        published = str(payload.get("published_at") or "").split("T")[0]
        downloads: dict[str, dict] = {}
        for asset in payload.get("assets") or []:
            name = str(asset.get("name") or "")
            lowered = name.lower()
            if not lowered.endswith((".exe", ".msi", ".dmg", ".pkg", ".appimage", ".zip", ".deb")):
                continue
            if "setup" in lowered or lowered.endswith((".msi", ".dmg", ".pkg", ".appimage", ".deb")):
                key = (
                    "windows"
                    if lowered.endswith((".exe", ".msi"))
                    else "macos"
                    if lowered.endswith((".dmg", ".pkg"))
                    else "linux"
                )
                downloads.setdefault(key, _download_entry(asset) or {})
        return {
            "version": latest.lstrip("vV"),
            "notes": notes,
            "published": published,
            "downloads": downloads,
        }

    downloads = {}
    for key, value in (payload.get("downloads") or {}).items():
        entry = _download_entry(value)
        if entry:
            downloads[str(key).lower()] = entry
    return {
        "version": str(payload.get("version") or payload.get("latest") or "").strip().lstrip("vV"),
        "notes": _notes(payload.get("notes") or payload.get("changelog")),
        "published": str(payload.get("published") or payload.get("date") or ""),
        "downloads": downloads,
    }


# --------------------------------------------------------------------------- #
# checking
# --------------------------------------------------------------------------- #

def check(force: bool = False) -> dict:
    """Return the current release status, cached for half an hour."""
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE["data"]
        if cached and not force and now - float(_CACHE["at"]) < CHECK_TTL:
            return cached

    base = {
        "current": APP_VERSION,
        "platform": platform_key(),
        "manifest_url": MANIFEST_URL,
        "channel": bool(MANIFEST_URL),
        "checked_at": now,
        "latest": "",
        "available": False,
        "notes": [],
        "published": "",
        "download": None,
        "error": "",
    }

    try:
        if not MANIFEST_URL:
            raise ValueError("No update channel is configured.")
        payload = json.loads(_read_source(MANIFEST_URL).decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            raise ValueError("The update channel returned an unexpected document.")
        release = _normalise(payload)
        if not release["version"]:
            raise ValueError("The update channel did not name a version.")
        base["latest"] = release["version"]
        base["notes"] = release["notes"]
        base["published"] = release["published"]
        base["available"] = is_newer(release["version"], APP_VERSION)
        download = release["downloads"].get(platform_key())
        if download:
            base["download"] = {**download, "size_label": pretty_size(download.get("size"))}
    except Exception as exc:  # noqa: BLE001 - offline is normal, not a crash
        base["error"] = _friendly_error(exc)

    with _CACHE_LOCK:
        _CACHE.update({"at": now, "data": base, "error": base["error"]})
    return base


def _friendly_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if isinstance(exc, FileNotFoundError):
        return "The update manifest could not be found."
    if "timed out" in lowered or "timeout" in lowered:
        return "The update server took too long to answer."
    if "urlopen error" in lowered or "getaddrinfo" in lowered or "connection" in lowered:
        return "The update server could not be reached."
    if "json" in lowered:
        return "The update channel returned an unreadable manifest."
    return text[:200]


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #

def _safe_name(raw: str, version: str) -> str:
    candidate = Path(urllib.parse.urlparse(raw).path).name or ""
    candidate = re.sub(r"[^A-Za-z0-9._-]", "", candidate)
    if not candidate or "." not in candidate:
        suffix = ".exe" if os.name == "nt" else ".dmg" if sys.platform == "darwin" else ".AppImage"
        candidate = f"VorteX-AI-{version or APP_VERSION}-Setup{suffix}"
    return candidate


def download_state() -> dict:
    with _DOWNLOAD_LOCK:
        state = dict(_DOWNLOAD)
    state["size_label"] = pretty_size(state["total"])
    state["received_label"] = pretty_size(state["received"])
    state["speed_label"] = pretty_size(state["speed"]) + "/s" if state["speed"] else "—"
    state["eta_label"] = _eta_label(state["eta"]) if state["eta"] else "—"
    state["ready"] = state["state"] == "done" and bool(state["path"]) and Path(state["path"]).is_file()
    state["platform"] = platform_key()
    return state


def _eta_label(seconds: float | int) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s left"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes}m {rest:02d}s left"


def _update_state(**values) -> None:
    with _DOWNLOAD_LOCK:
        _DOWNLOAD.update(values)


def start_download() -> dict:
    """Download the installer for this platform in the background."""
    global _THREAD
    info = check()
    entry = info.get("download") or {}
    url = str(entry.get("url") or "").strip()
    if not info.get("available") or not url:
        return {**download_state(), "state": "error", "error": "No update is available to download."}

    with _DOWNLOAD_LOCK:
        if _DOWNLOAD["state"] == "downloading" and _THREAD and _THREAD.is_alive():
            return download_state()

    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_name(entry.get("name") or url, info.get("latest", ""))
    target = UPDATE_DIR / name
    _CANCEL.clear()
    _update_state(
        state="downloading",
        percent=0.0,
        received=0,
        total=int(entry.get("size") or 0),
        speed=0.0,
        eta=0,
        file=name,
        path=str(target),
        version=info.get("latest", ""),
        url=url,
        error="",
        indeterminate=not bool(entry.get("size")),
        started_at=time.time(),
        finished_at=0.0,
    )
    _THREAD = threading.Thread(
        target=_download_worker, args=(url, target, entry), name="vortex-update-download", daemon=True
    )
    _THREAD.start()
    return download_state()


def _download_worker(url: str, target: Path, entry: dict) -> None:
    part = target.with_suffix(target.suffix + ".part")
    received = 0
    total = int(entry.get("size") or 0)
    started = time.time()
    window_at = started
    window_bytes = 0
    speed = 0.0
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": f"VorteX AI/{APP_VERSION}", "Accept": "*/*"}
        )
        with urllib.request.urlopen(request, timeout=30) as response, part.open("wb") as handle:
            header_total = int(response.headers.get("Content-Length") or 0)
            if header_total:
                total = header_total
            _update_state(total=total, indeterminate=not bool(total))
            while True:
                if _CANCEL.is_set():
                    raise _Cancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                now = time.time()
                window_bytes += len(chunk)
                if now - window_at >= 0.4:
                    speed = window_bytes / max(now - window_at, 0.001)
                    window_at, window_bytes = now, 0
                percent = (received / total * 100) if total else 0.0
                eta = ((total - received) / speed) if total and speed > 0 else 0
                _update_state(
                    percent=round(min(percent, 99.4), 2),
                    received=received,
                    speed=round(speed, 1),
                    eta=int(eta),
                )
        if total and received != total:
            raise ValueError("The download stopped before it finished.")
        part.replace(target)
        _update_state(
            state="done",
            percent=100.0,
            received=received,
            total=total or received,
            speed=0.0,
            eta=0,
            indeterminate=False,
            finished_at=time.time(),
            error="",
        )
    except _Cancelled:
        part.unlink(missing_ok=True)
        _update_state(state="cancelled", speed=0.0, eta=0, error="")
    except Exception as exc:  # noqa: BLE001 - surface it in the UI
        part.unlink(missing_ok=True)
        _update_state(
            state="error",
            speed=0.0,
            eta=0,
            error=_friendly_error(exc) if not isinstance(exc, ValueError) else str(exc),
        )


class _Cancelled(Exception):
    """Raised inside the worker when the user cancels."""


def cancel_download() -> dict:
    _CANCEL.set()
    return download_state()


def launch_installer() -> dict:
    state = download_state()
    if not state["ready"]:
        return {"ok": False, "error": "The installer has not finished downloading."}
    path = Path(state["path"])
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            try:
                path.chmod(0o755)
            except OSError:
                pass
            subprocess.Popen([str(path)], start_new_session=True)
        return {"ok": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not open the installer: {exc}"}


def open_manual_download() -> dict:
    info = check()
    entry = info.get("download") or {}
    url = str(entry.get("url") or "").strip() or MANIFEST_URL
    if not url:
        return {"ok": False, "error": "No download link is available yet."}
    try:
        webbrowser.open(url)
        return {"ok": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not open your browser: {exc}", "url": url}
