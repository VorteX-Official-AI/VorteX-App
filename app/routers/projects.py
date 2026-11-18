import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..database import execute, query, query_one
from ..files import META_DIR, allowed_roots, grant_root, resolve_target, run_ops
from ..security import require_user
from ..serve import ensure_server, stop_server

router = APIRouter(prefix="/api/projects", tags=["projects"])

# Writes outside a project folder wait here until the user allows or denies them.
PENDING_WRITES: dict[str, dict] = {}
PENDING_TTL = 30 * 60


class ProjectIn(BaseModel):
    name: str
    root_path: str
    admin_access: bool = False
    computer_access: bool = False
    repo_url: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    admin_access: bool | None = None
    computer_access: bool | None = None
    repo_url: str | None = None


PICK_SCRIPT = """
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
path = filedialog.askdirectory(title="Choose a project folder")
print(path or "", end="")
root.destroy()
"""


def _project_or_404(project_id: int, user: dict) -> dict:
    project = query_one(
        "SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"])
    )
    if not project:
        raise HTTPException(404, "Project not found.")
    return project


def _elevate_restart() -> bool:
    """Relaunch the desktop app through the Windows UAC prompt."""
    if os.name != "nt" or os.environ.get("VORTEX_DESKTOP") != "1":
        return False
    if os.environ.get("VORTEX_ELEVATE_DRY") == "1":
        return True
    script = Path(__file__).resolve().parents[2] / "app.py"
    if not script.is_file():
        return False
    try:
        import ctypes

        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}"', str(script.parent), 1
        )
    except Exception:  # noqa: BLE001 - elevation is best effort
        return False
    if result <= 32:
        return False

    def shutdown() -> None:
        time.sleep(1.4)
        os._exit(0)

    threading.Thread(target=shutdown, daemon=True).start()
    return True


def _folder_writable(path: str) -> bool:
    probe = Path(path) / ".vortex-write-check"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def write_project_meta(project: dict) -> None:
    """Keep a small, app-owned record of this project inside its folder."""
    folder = Path(project["root_path"]) / META_DIR
    try:
        folder.mkdir(exist_ok=True)
        (folder / "project.json").write_text(
            json.dumps(
                {
                    "project_id": project["id"],
                    "name": project["name"],
                    "root": project["root_path"],
                    "repository": project.get("repo_url") or "",
                    "admin_access": bool(project["admin_access"]),
                    "computer_access": bool(project.get("computer_access")),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (folder / "permissions.json").write_text(
            json.dumps(
                {
                    "admin_access": bool(project["admin_access"]),
                    "project_root": project["root_path"],
                    "allowed_paths": allowed_roots(project["id"]),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (folder / ".gitignore").write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def _clean_project(project: dict) -> dict:
    data = dict(project)
    data["admin_access"] = bool(data.get("admin_access"))
    data["computer_access"] = bool(data.get("computer_access"))
    data["chats"] = query_one(
        "SELECT COUNT(*) AS c FROM conversations WHERE project_id = ?", (project["id"],)
    )["c"]
    return data


@router.get("")
def list_projects(user: dict = Depends(require_user)):
    rows = query(
        "SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)
    )
    return {"projects": [_clean_project(row) for row in rows]}


@router.post("")
def create_project(payload: ProjectIn, user: dict = Depends(require_user)):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "Give the project a name.")
    raw = (payload.root_path or "").strip().strip("\"'")
    if not raw:
        raise HTTPException(400, "Choose a folder for this project.")
    root = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(root):
        raise HTTPException(400, "That folder does not exist on this PC.")
    if not _folder_writable(root):
        raise HTTPException(
            400,
            "That folder is read-only for your Windows account, so files cannot be written "
            "into it. Pick a folder you own (for example inside your user folder), or fix its "
            "permissions before creating the project.",
        )
    now = time.time()
    project_id = execute(
        "INSERT INTO projects (user_id, name, root_path, repo_url, admin_access, computer_access, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user["id"],
            name[:80],
            root,
            (payload.repo_url or "").strip()[:300],
            1 if payload.admin_access else 0,
            1 if payload.computer_access else 0,
            now,
            now,
        ),
    )
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    write_project_meta(project)
    restarting = bool(payload.admin_access) and _elevate_restart()
    return {
        "project": _clean_project(project),
        "restarting": restarting,
    }


@router.patch("/{project_id}")
def update_project(project_id: int, payload: ProjectUpdate, user: dict = Depends(require_user)):
    project = _project_or_404(project_id, user)
    name = (payload.name or project["name"]).strip()[:80] or project["name"]
    admin = project["admin_access"] if payload.admin_access is None else int(payload.admin_access)
    computer = (
        project.get("computer_access", 0)
        if payload.computer_access is None
        else int(payload.computer_access)
    )
    repo = project.get("repo_url") or ""
    if payload.repo_url is not None:
        repo = payload.repo_url.strip()[:300]
    execute(
        "UPDATE projects SET name = ?, admin_access = ?, computer_access = ?, repo_url = ?, "
        "updated_at = ? WHERE id = ?",
        (name, admin, computer, repo, time.time(), project_id),
    )
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    write_project_meta(project)
    return {"project": _clean_project(project)}


@router.delete("/{project_id}")
def delete_project(project_id: int, user: dict = Depends(require_user)):
    _project_or_404(project_id, user)
    stop_server(project_id)
    execute("UPDATE conversations SET project_id = NULL WHERE project_id = ?", (project_id,))
    execute("DELETE FROM project_permissions WHERE project_id = ?", (project_id,))
    execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return {"ok": True}


@router.post("/{project_id}/open")
def open_project_folder(project_id: int, user: dict = Depends(require_user)):
    """Reveal the project folder in the desktop file manager."""
    project = _project_or_404(project_id, user)
    path = (project.get("root_path") or "").strip()
    if not path or not os.path.isdir(path):
        raise HTTPException(404, "That project folder no longer exists.")
    try:
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(path)  # noqa: S606 - opens an existing project folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        raise HTTPException(500, f"Could not open the folder: {exc}")
    return {"ok": True, "path": path}


@router.get("/{project_id}/preview")
def project_preview(project_id: int, user: dict = Depends(require_user)):
    project = _project_or_404(project_id, user)
    preview = ensure_server(project["id"], project["root_path"])
    return preview or {"url": None, "ready": False}


@router.post("/pick-folder")
def pick_folder(user: dict = Depends(require_user)):
    """Open the native Windows folder dialog on this PC and return the choice."""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", PICK_SCRIPT],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "The folder picker timed out."}
    except OSError as exc:
        return {"ok": False, "message": f"Could not open the folder picker: {exc}"}
    path = (completed.stdout or "").strip()
    if not path:
        message = (completed.stderr or "").strip().splitlines()
        return {
            "ok": False,
            "message": "No folder was selected." if not message else message[-1][:200],
        }
    return {"ok": True, "path": os.path.abspath(path)}


def _pending_or_404(token: str, user: dict) -> tuple[str, dict]:
    entry = PENDING_WRITES.get(token)
    if not entry or entry["user_id"] != user["id"]:
        raise HTTPException(404, "That request has expired.")
    if time.time() - entry["created"] > PENDING_TTL:
        PENDING_WRITES.pop(token, None)
        raise HTTPException(404, "That request has expired.")
    return token, entry


@router.post("/writes/{token}/approve")
def approve_writes(token: str, user: dict = Depends(require_user)):
    token, entry = _pending_or_404(token, user)
    project = _project_or_404(entry["project_id"], user)
    done, errors = run_ops(entry["files"])
    PENDING_WRITES.pop(token, None)
    granted = False
    for item in done:
        if not Path(item["path"]).is_relative_to(Path(project["root_path"])):
            grant_root(project["id"], item["path"])
            granted = True
    if granted:
        write_project_meta(project)
    return {"ok": True, "files": done, "errors": errors}


@router.post("/writes/{token}/deny")
def deny_writes(token: str, user: dict = Depends(require_user)):
    token, entry = _pending_or_404(token, user)
    PENDING_WRITES.pop(token, None)
    return {"ok": True, "denied": len(entry["files"])}


def queue_writes(project: dict, files: list[dict], user_id: int) -> tuple[str, list[str]]:
    token = uuid.uuid4().hex
    PENDING_WRITES[token] = {
        "project_id": project["id"],
        "user_id": user_id,
        "files": files,
        "created": time.time(),
    }
    return token, [resolve_target(project["root_path"], item["path"]) for item in files]
