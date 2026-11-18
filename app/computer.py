"""Opt-in desktop control used to test what the app builds.

The model asks for actions with a ```vortex:computer block containing one JSON
object per line. Everything is disabled unless the project has computer_access
enabled, and each batch is capped and audited in the chat activity log.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from .config import CHAT_UPLOAD_DIR, CHAT_UPLOAD_URL
from .files import _inside, allowed_roots, resolve_target

ACTION_BLOCK = re.compile(r"```vortex:computer\s*\r?\n(.*?)```", re.S | re.I)
MAX_ACTIONS = 16
MAX_WAIT = 60
SHOT_PREFIX = "screen-"
SHOT_KEEP = 60


def parse_actions(text: str) -> tuple[list[dict], str]:
    """Parse vortex:computer blocks into action dicts and strip them."""
    actions: list[dict] = []
    for block in ACTION_BLOCK.findall(text or ""):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("action"):
                actions.append(item)
    return actions[:MAX_ACTIONS], ACTION_BLOCK.sub("", text or "").strip()


def available() -> bool:
    """True when the optional desktop-automation packages are installed."""
    try:
        import pyautogui  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency
        return False
    return True


def _screen():
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.2
    return pyautogui


def _prune() -> None:
    shots = sorted(CHAT_UPLOAD_DIR.glob(SHOT_PREFIX + "*.png"), key=lambda p: p.stat().st_mtime)
    for old in shots[:-SHOT_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


def capture(name: str = "") -> dict:
    """Take a screenshot and return it as a chat-upload image."""
    if not available():
        raise RuntimeError("Desktop automation is not installed. Run pip install pyautogui pillow.")
    pyautogui = _screen()
    image = pyautogui.screenshot()
    filename = f"{SHOT_PREFIX}{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
    path = CHAT_UPLOAD_DIR / filename
    image.save(path)
    _prune()
    label = name.strip()[:60] if name else "screen"
    return {
        "action": "screenshot",
        "ok": True,
        "label": "Screenshot: " + label,
        "detail": f"{image.width}x{image.height}",
        "url": CHAT_UPLOAD_URL + filename,
        "path": str(path),
    }


def _number(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _click(action: dict, button: str = "left", clicks: int = 1) -> dict:
    pyautogui = _screen()
    width, height = pyautogui.size()
    x = max(0, min(_number(action.get("x")), width - 1))
    y = max(0, min(_number(action.get("y")), height - 1))
    pyautogui.click(x=x, y=y, clicks=clicks, interval=0.12, button=button)
    return {
        "action": action.get("action"),
        "ok": True,
        "label": f"Clicked {button} at {x}, {y}" + (" twice" if clicks == 2 else ""),
        "detail": "",
    }


def _perform_file(action: dict, project: dict) -> dict:
    root = project["root_path"]
    op = str(action.get("op") or "move").lower()
    raw_source = str(action.get("src") or "").strip()
    if not raw_source:
        raise ValueError("A source path is required.")
    source = resolve_target(root, raw_source)
    if Path(source) == Path(root).resolve():
        raise ValueError("Refusing to modify the project root itself.")
    allowed = [_inside(root, source)]
    if project.get("admin_access"):
        allowed.append(any(_inside(base, source) for base in allowed_roots(project["id"])))
    if not any(allowed):
        raise ValueError("Source is outside the project folder. Enable administrator access first.")

    if op == "delete":
        target = Path(source)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            raise ValueError("That path does not exist.")
        return {"action": "file", "ok": True, "label": "Deleted " + target.name, "detail": str(target)}

    raw_dest = str(action.get("dst") or "")
    if not raw_dest:
        raise ValueError("A destination path is required.")
    dest = resolve_target(root, raw_dest)
    inside = _inside(root, dest)
    if not inside and project.get("admin_access"):
        inside = any(_inside(base, dest) for base in allowed_roots(project["id"]))
    if not inside:
        raise ValueError("Destination is outside the project folder. Enable administrator access first.")

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    if op == "copy":
        if Path(source).is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)
        label = "Copied " + Path(source).name
    else:
        shutil.move(source, dest)
        label = "Moved " + Path(source).name
    return {"action": "file", "ok": True, "label": label, "detail": f"to {dest}"}


def _launch(command: str, cwd: str) -> dict:
    command = (command or "").strip()
    if not command:
        raise ValueError("A launch command is required.")
    env = dict(os.environ)
    env["VORTEX_TEST_USERNAME"] = "dev"
    env["PATH"] = cwd + os.pathsep + env.get("PATH", "")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=flags,
    )
    return {
        "action": "launch",
        "ok": True,
        "label": "Launched " + command.strip()[:70],
        "detail": "PID " + str(process.pid),
        "pid": process.pid,
    }


def _perform(action: dict, project: dict) -> dict:
    pyautogui = _screen()
    root = project["root_path"]
    kind = str(action.get("action") or "").lower()
    if kind == "screenshot":
        return capture(str(action.get("name") or ""))
    if kind == "click":
        return _click(action)
    if kind == "double_click":
        return _click(action, clicks=2)
    if kind == "right_click":
        return _click(action, button="right")
    if kind == "move":
        width, height = pyautogui.size()
        x = max(0, min(_number(action.get("x")), width - 1))
        y = max(0, min(_number(action.get("y")), height - 1))
        pyautogui.moveTo(x, y, duration=0.25)
        return {"action": kind, "ok": True, "label": f"Moved mouse to {x}, {y}", "detail": ""}
    if kind == "scroll":
        amount = max(-2400, min(_number(action.get("amount"), -400), 2400))
        pyautogui.scroll(amount)
        return {"action": kind, "ok": True, "label": f"Scrolled {amount}", "detail": ""}
    if kind == "type":
        text = str(action.get("text") or "")[:2000]
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if index:
                pyautogui.press("enter")
            pyautogui.write(line, interval=0.01)
        return {"action": kind, "ok": True, "label": "Typed " + str(len(text)) + " characters", "detail": ""}
    if kind == "key":
        keys = action.get("keys") or action.get("key") or ""
        if isinstance(keys, str):
            keys = keys.replace("+", " ").split()
        keys = [str(key).strip() for key in keys if str(key).strip()]
        if not keys:
            raise ValueError("No key was given.")
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return {"action": kind, "ok": True, "label": "Pressed " + "+".join(keys), "detail": ""}
    if kind == "launch":
        return _launch(str(action.get("command") or ""), root)
    if kind == "file":
        return _perform_file(action, project)
    if kind == "wait":
        return {
            "action": kind,
            "ok": True,
            "label": "Waited " + str(min(MAX_WAIT, max(0, _number(action.get("seconds"), 1)))) + "s",
            "detail": "",
        }
    raise ValueError("Unknown computer action: " + kind)


async def run_actions(actions: list[dict], project: dict) -> AsyncIterator[dict]:
    """Execute a capped action batch, yielding one result per action."""
    if not actions:
        return
    if not available():
        yield {
            "action": "error",
            "ok": False,
            "label": "Desktop automation is not installed",
            "detail": "Run pip install pyautogui pillow and restart the app.",
        }
        return
    for action in actions[:MAX_ACTIONS]:
        try:
            result = await asyncio.to_thread(_perform, action, project)
        except Exception as exc:  # noqa: BLE001 - report the failure to the chat
            result = {
                "action": str(action.get("action") or "action"),
                "ok": False,
                "label": "Could not " + str(action.get("action") or "run the action"),
                "detail": str(exc)[:280],
            }
        yield result
        if str(action.get("action") or "").lower() == "wait":
            await asyncio.sleep(min(MAX_WAIT, max(0, _number(action.get("seconds"), 1))))
        elif result.get("action") == "launch":
            await asyncio.sleep(min(MAX_WAIT, max(0, _number(action.get("wait"), 0))))
        await asyncio.sleep(0.2)


def screenshot_blocks(urls: list[str], max_bytes: int = 8 * 1024 * 1024) -> list[dict]:
    """Turn screenshot URLs into image blocks the providers can read."""
    import base64

    blocks: list[dict] = []
    for url in urls[-4:]:
        if not url.startswith(CHAT_UPLOAD_URL):
            continue
        path = CHAT_UPLOAD_DIR / Path(url).name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if not data or len(data) > max_bytes:
            continue
        blocks.append(
            {"type": "image", "mime": "image/png", "data": base64.b64encode(data).decode("ascii")}
        )
    return blocks
