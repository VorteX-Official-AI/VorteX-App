import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..ai import THINK_PROMPT, estimate_tokens, provider_of, stream_chat
from ..computer import (
    capture as capture_screen,
    parse_actions as parse_computer_actions,
    run_actions as run_computer_actions,
    screenshot_blocks,
)
from ..config import (
    ALLOWED_IMAGE_EXT,
    CHAT_UPLOAD_DIR,
    CHAT_UPLOAD_URL,
    MAX_CHAT_FILE_BYTES,
    MAX_IMAGE_BYTES,
)
from ..todos import parse_todos
from ..database import execute, get_setting, query, query_one, user_public
from ..files import (
    categorize,
    extract_standard_files,
    parse_file_ops,
    read_tree,
    run_ops,
    strip_commands,
    strip_file_ops,
)
from ..runner import parse_commands, run_command
from ..security import require_user
from ..serve import ensure_server
from ..usage import (
    charge_model_usage,
    ensure_model_usage,
    sync_user_totals,
    usage_public,
)
from ..vps import (
    get_connection as get_vps_connection,
    parse_commands as parse_vps_commands,
    run_command as run_vps_command,
    strip_commands as strip_vps_commands,
)
from ..web import gather_context, search_query
from .projects import queue_writes

router = APIRouter(prefix="/api", tags=["chat"])


class ActivityIn(BaseModel):
    steps: list[dict] = []
    duration: float = 0
    failed: bool = False
    label: str = ""
    summary: dict | None = None


PROJECT_PROMPT = (
    "You are an autonomous coding agent working inside the project folder:\n{root}\n\n"
    "Current files:\n{listing}\n\n"
    "You can create, edit and delete real files on this PC by emitting blocks exactly like:\n"
    '```vortex:write path="src/index.html"\n<complete file content>\n```\n'
    'and ```vortex:delete path="old.txt"```.\n'
    "Plain fenced blocks are also saved as files (html becomes index.html, css styles.css, "
    "js app.js, and numbered pages after that), so never dump whole source files into the "
    "chat: write them out and keep the reply to a short summary.\n"
    "Keep each file under about 400 lines and put CSS in styles.css and JavaScript in app.js, "
    "so a long page never gets cut off by the output limit.\n"
    "Any stack is allowed: websites, desktop apps, games (pygame, Unity, Godot), Minecraft mods "
    "(Forge/Fabric), server plugins (Bukkit/Spigot/Paper), launchers, version files, C#, Java, "
    "Kotlin, Rust, Go, C/C++, Lua, PHP, Dart and more. For those, always give explicit paths "
    "in vortex:write blocks — for example src/main/java/com/example/MyMod.java, "
    "src/main/resources/fabric.mod.json, build.gradle, gradle.properties, plugin.yml — and "
    "include every build/config file the project needs. Use vortex:run to compile or test "
    "(gradle build, dotnet build, cargo build, npm run build) and report the result.\n"
    "Paths may be relative to the project root. Every write replaces the whole file, so emit "
    "complete files. Use the folder you are given; only step outside it when the user asks you "
    "to.\n\n"
    "You can also run terminal commands in the project folder by emitting:\n"
    '```vortex:run\npython build.py\n```\n'
    "Commands run through the Windows shell with the project folder as the working directory, so "
    ".bat/.cmd files, npm and python all work. Keep them non-interactive and short (120 seconds "
    "max); never start blocking dev servers.\n\n"
    "When the project has an index.html, the app serves this folder on a localhost port and "
    "shows it in the live preview automatically. Build the site as real files (index.html plus "
    "assets) instead of pasting code in chat. After the blocks, briefly summarise what you changed.\n\n"
    "Before a multi-step task, emit a short task list exactly like:\n"
    '```vortex:todo\n- [ ] Inspect the project\n- [ ] Write the files\n- [ ] Test the result\n```\n'
    "The app ticks these off one at a time as work finishes and removes the list when done. "
    "Keep 3-6 concrete items and never repeat the list in your reply."
)

COMPUTER_PROMPT = (
    "\n\nComputer control is ENABLED for this project. After writing files you may actually "
    "test them by controlling the mouse and keyboard and taking screenshots. Emit a block "
    "like this, one JSON action per line:\n"
    '```vortex:computer\n{"action":"launch","command":"gradlew.bat runClient","wait":45}\n'
    '{"action":"screenshot","name":"game window"}\n{"action":"click","x":640,"y":480}\n'
    '{"action":"type","text":"dev"}\n{"action":"key","keys":["enter"]}\n```\n'
    "Actions: launch (command + optional wait seconds), wait, screenshot, click, double_click, "
    "right_click, move, scroll, type, key (list for a hotkey), and file (op move|copy|delete, "
    "src, dst). Coordinates are real screen pixels. Use screenshots to check the result, then "
    "fix any error you can see before claiming success. The app automatically shows your "
    "screenshots back to you for a verification pass, so always screenshot the final state.\n"
    "For Minecraft mods build first with vortex:run so compile errors are visible, then launch "
    'gradlew.bat runClient --args="--username dev"' + " (Fabric/Forge) or the server jar, "
    "wait for the window, screenshot it and use the test username dev (VORTEX_TEST_USERNAME "
    "is also set). "
    "For web or desktop apps launch them the same way. Only test what the user asked for, "
    "never close other apps, and do not repeat the same failing action more than twice."
)

COMPUTER_OFF_PROMPT = (
    "\n\nComputer control is OFF for this project, so do not emit vortex:computer blocks. "
    "You can still run build and test commands with vortex:run."
)

VPS_PROMPT = (
    "\n\nThe user has given you SSH access to their VPS:\n"
    "- host: {host}\n- username: {username}\n- label: {name}\n\n"
    "When the user asks you to work on the VPS, install software there, or build and run "
    "something (including LLMs such as Ollama or vLLM), do it over SSH by emitting blocks "
    "exactly like:\n"
    '```vortex:vps\nuname -a && whoami\n```\n'
    "Each block is one non-interactive shell command. Up to 5 run per reply and each has "
    "300 seconds, so split long work into steps, use `apt-get install -y`, `pip install "
    "--break-system-packages` when needed, and start servers or model downloads with "
    "`nohup ... > /tmp/vortex.log 2>&1 &`. To create a file, use a heredoc inside the "
    "command, for example `cat > /opt/app/app.py <<'EOF' ... EOF`. Use sudo only when the "
    "configured user is allowed to, and add `-n` so a password prompt cannot hang.\n"
    "The command output comes back to the app, so inspect failures and fix them in the next "
    "reply. Never print or ask for the saved password or private key. Do not touch the VPS "
    "unless the user's request needs it."
)


class RenameIn(BaseModel):
    title: str = ""


class ChatIn(BaseModel):
    message: str
    conversation_id: int | None = None
    model_id: int | None = None
    project_id: int | None = None
    effort: str = "medium"
    think: bool = False
    web: bool = False
    mode: str = "chat"
    images: list[str] = []
    attachments: list[dict] = []


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


CONTINUE_PROMPT = (
    "Your previous answer was cut off by the model's output limit. Continue exactly where "
    "it stopped. Do not repeat, restart, or summarise anything you already wrote. If a "
    "vortex:write or fenced code block was cut off, continue its remaining content and "
    "close the block; do not open a second block for the same file. Return only the "
    "continuation."
)
LENGTH_FINISH = {"length", "max_tokens", "MAX_TOKENS"}
MAX_CONTINUATIONS = 3


def _merge_usage(total: dict | None, update: dict | None) -> dict | None:
    """Add token counts from another provider round."""
    if not isinstance(update, dict):
        return total
    if not isinstance(total, dict):
        return update
    merged = dict(total)
    for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
        if key in update:
            merged[key] = int(total.get(key) or 0) + int(update.get(key) or 0)
    return merged


def _has_truncated_file_ops(text: str, project: dict | None, mode: str) -> bool:
    if not (project and mode == "code" and text):
        return False
    ops = parse_file_ops(text)
    if not ops:
        ops, _ = extract_standard_files(text)
    return any(op.get("truncated") for op in ops)


def _applied_file_summary(items: list[dict]) -> str:
    labels = [
        ("Deleted " if item.get("action") == "delete" else "Wrote ")
        + Path(item["path"]).name
        for item in items[:4]
    ]
    summary = ", ".join(labels)
    if len(items) > 4:
        summary += f" and {len(items) - 4} more"
    return summary + "."


def _stored_attachment(item: dict) -> dict | None:
    """Validate an attachment reference and return its safe public metadata."""
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if url.startswith(CHAT_UPLOAD_URL):
        stored = Path(url).name
    elif url.startswith("/api/attachments/"):
        stored = Path(url.split("?", 1)[0]).name
    else:
        return None
    path = CHAT_UPLOAD_DIR / stored
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    provided_name = Path(str(item.get("name") or stored)).name[:180]
    name = provided_name or stored
    kind = "image" if url.startswith(CHAT_UPLOAD_URL) and suffix in ALLOWED_IMAGE_EXT else "file"
    mime = str(item.get("mime") or ALLOWED_IMAGE_EXT.get(suffix) or "application/octet-stream")[:100]
    try:
        size = int(item.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size = 0
    return {"url": url, "name": name, "mime": mime, "kind": kind, "size": size}


def _payload_attachments(payload: ChatIn) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for item in (payload.attachments or [])[:8]:
        normalized = _stored_attachment(item)
        if normalized and normalized["url"] not in seen:
            items.append(normalized)
            seen.add(normalized["url"])
    # Older clients send only image URLs.
    for url in payload.images or []:
        if not isinstance(url, str) or url in seen:
            continue
        normalized = _stored_attachment({"url": url, "name": Path(url).name})
        if normalized:
            items.append(normalized)
            seen.add(normalized["url"])
    return items[:4]


def _meta_attachments(meta: dict) -> list[dict]:
    items = meta.get("attachments")
    if isinstance(items, list) and items:
        return [item for item in (_stored_attachment(entry) for entry in items) if item]
    urls = meta.get("images") if isinstance(meta.get("images"), list) else []
    return [
        item
        for item in (
            _stored_attachment({"url": url, "name": Path(str(url)).name})
            for url in urls
            if isinstance(url, str)
        )
        if item
    ]


def _attachment_notes(items: list[dict]) -> str:
    if not items:
        return ""
    lines = [
        f"- @{item['name']} ({item.get('mime') or 'file'}, {item.get('size') or 0} bytes)"
        for item in items
    ]
    return "Attached files:\n" + "\n".join(lines)


def _image_blocks(urls: list[str]) -> list[dict]:
    blocks: list[dict] = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith(CHAT_UPLOAD_URL):
            continue
        path = CHAT_UPLOAD_DIR / Path(url).name
        if not path.is_file():
            continue
        mime = ALLOWED_IMAGE_EXT.get(path.suffix.lower())
        if not mime:
            continue
        data = path.read_bytes()
        if not data or len(data) > MAX_IMAGE_BYTES:
            continue
        blocks.append(
            {"type": "image", "mime": mime, "data": base64.b64encode(data).decode("ascii")}
        )
    return blocks


@router.post("/uploads/image")
async def upload_image(file: UploadFile = File(...), user: dict = Depends(require_user)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXT:
        raise HTTPException(400, "Attach a PNG, JPG, WEBP or GIF image.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Images must be smaller than 8 MB.")
    name = f"{uuid.uuid4().hex}{suffix}"
    (CHAT_UPLOAD_DIR / name).write_bytes(data)
    return {
        "url": CHAT_UPLOAD_URL + name,
        "mime": ALLOWED_IMAGE_EXT[suffix],
        "name": file.filename or name,
        "kind": "image",
        "size": len(data),
    }


@router.post("/uploads/file")
async def upload_file(file: UploadFile = File(...), user: dict = Depends(require_user)):
    original = Path(file.filename or "file").name or "file"
    suffix = Path(original).suffix.lower()
    if not suffix or len(suffix) > 15 or not suffix[1:].isalnum():
        suffix = ".bin"
    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    is_image = suffix in ALLOWED_IMAGE_EXT
    if is_image and len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Images must be smaller than 8 MB.")
    if len(data) > MAX_CHAT_FILE_BYTES:
        raise HTTPException(400, "Files must be smaller than 20 MB.")
    stored = f"{uuid.uuid4().hex}{suffix}"
    (CHAT_UPLOAD_DIR / stored).write_bytes(data)
    if is_image:
        url = CHAT_UPLOAD_URL + stored
    else:
        url = f"/api/attachments/{stored}?name={quote(original)}"
    return {
        "url": url,
        "mime": ALLOWED_IMAGE_EXT.get(suffix) or (file.content_type or "application/octet-stream")[:100],
        "name": original[:180],
        "kind": "image" if is_image else "file",
        "size": len(data),
    }


@router.get("/attachments/{stored}")
def download_attachment(
    stored: str, name: str = "", user: dict = Depends(require_user)
):
    safe = Path(stored).name
    path = CHAT_UPLOAD_DIR / safe
    if not path.is_file():
        raise HTTPException(404, "Attachment not found.")
    download_name = Path(name).name.replace("\r", "").replace("\n", "")[:180] or safe
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=download_name,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


def _model_public(model: dict) -> dict:
    return {
        "id": model["id"],
        "name": model["name"],
        "model_id": model["model_id"],
        "description": model["description"],
        "logo": model["logo"],
        "enabled": model["enabled"],
    }


@router.get("/models")
def list_models(user: dict = Depends(require_user)):
    rows = query("SELECT * FROM models WHERE enabled = 1 ORDER BY name COLLATE NOCASE")
    models = []
    for row in rows:
        item = _model_public(row)
        item["usage"] = usage_public(ensure_model_usage(user, row["id"]))
        models.append(item)
    sync_user_totals(user["id"])
    return {"models": models}


@router.get("/conversations")
def list_conversations(
    mode: str | None = None,
    project_id: int | None = None,
    user: dict = Depends(require_user),
):
    sql = (
        "SELECT c.id, c.title, c.mode, c.model_name, c.updated_at, c.project_id, "
        "p.name AS project_name FROM conversations c "
        "LEFT JOIN projects p ON p.id = c.project_id WHERE c.user_id = ?"
    )
    params: list = [user["id"]]
    if mode in ("chat", "code"):
        sql += " AND c.mode = ?"
        params.append(mode)
    if project_id:
        sql += " AND c.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY c.updated_at DESC LIMIT 100"
    return {"conversations": query(sql, params)}


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: int, payload: RenameIn, user: dict = Depends(require_user)
):
    row = query_one(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "Conversation not found.")
    title = (payload.title or "").strip()[:80]
    if not title:
        raise HTTPException(400, "Give the chat a name.")
    execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title, time.time(), conversation_id),
    )
    return {"ok": True, "title": title}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, user: dict = Depends(require_user)):
    conversation = query_one(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user["id"]),
    )
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    messages = query(
        "SELECT id, role, content, meta, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    )
    for message in messages:
        if message.get("meta"):
            try:
                message["meta"] = json.loads(message["meta"])
            except json.JSONDecodeError:
                message["meta"] = {}
    return {"conversation": conversation, "messages": messages}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, user: dict = Depends(require_user)):
    conversation = query_one(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user["id"]),
    )
    if not conversation:
        raise HTTPException(404, "Conversation not found.")
    execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return {"ok": True}


@router.post("/messages/{message_id}/activity")
def save_activity(message_id: int, payload: ActivityIn, user: dict = Depends(require_user)):
    row = query_one(
        "SELECT m.id, m.meta, c.user_id FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id WHERE m.id = ?",
        (message_id,),
    )
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(404, "Message not found.")
    try:
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    steps = []
    for step in payload.steps[:40]:
        if not isinstance(step, dict):
            continue
        title = str(step.get("title") or "").strip()[:160]
        if not title:
            continue
        steps.append(
            {
                "title": title,
                "detail": str(step.get("detail") or "")[:1200],
                "t": round(float(step.get("t") or 0), 1),
            }
        )
    summary = None
    if isinstance(payload.summary, dict):
        summary = {
            "files": [
                {
                    "path": str(item.get("path") or "")[:300],
                    "action": "delete" if item.get("action") == "delete" else "write",
                    "bytes": int(item.get("bytes") or 0),
                }
                for item in (payload.summary.get("files") or [])[:40]
                if isinstance(item, dict) and item.get("path")
            ],
            "commands": [
                {
                    "command": str(item.get("command") or "")[:160],
                    "code": int(item.get("code") or 0),
                }
                for item in (payload.summary.get("commands") or [])[:20]
                if isinstance(item, dict)
            ],
            "preview": str(payload.summary.get("preview") or "")[:300],
        }
    meta["activity"] = {
        "steps": steps,
        "duration": round(float(payload.duration or 0), 1),
        "failed": bool(payload.failed),
        "label": str(payload.label or "")[:80],
        "summary": summary,
    }
    execute("UPDATE messages SET meta = ? WHERE id = ?", (json.dumps(meta), message_id))
    return {"ok": True, "steps": len(steps)}


@router.post("/chat/stream")
async def chat_stream(payload: ChatIn, user: dict = Depends(require_user)):
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(400, "Write a message first.")

    if payload.model_id:
        model = query_one("SELECT * FROM models WHERE id = ? AND enabled = 1", (payload.model_id,))
    else:
        model = query_one("SELECT * FROM models WHERE enabled = 1 ORDER BY id ASC LIMIT 1")
    if not model:
        raise HTTPException(
            400,
            "No AI model is configured yet. An admin needs to add an API key in the admin dashboard.",
        )

    if user["role"] != "admin":
        model_usage = ensure_model_usage(user, model["id"])
        if (
            int(model_usage.get("tokens_left") or 0) <= 0
            or int(model_usage.get("credits_left") or 0) <= 0
        ):
            raise HTTPException(
                402,
                f"You have used all of your tokens and credits for {model['name']}. "
                "They reset every 24 hours, or you can upgrade your plan to keep chatting.",
            )

    project = None
    if payload.project_id:
        project = query_one(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (payload.project_id, user["id"]),
        )
        if not project:
            raise HTTPException(404, "Project not found.")
    if payload.mode == "code" and not project:
        raise HTTPException(
            400, "Create or choose a project first — Code Mode writes files to a folder on this PC."
        )

    conversation = None
    if payload.conversation_id:
        conversation = query_one(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (payload.conversation_id, user["id"]),
        )
    if not conversation:
        title = message.strip().splitlines()[0][:70] or "New chat"
        conversation_id = execute(
            "INSERT INTO conversations (user_id, title, mode, model_name, project_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user["id"],
                title,
                payload.mode or "chat",
                model["name"],
                project["id"] if project else None,
                time.time(),
                time.time(),
            ),
        )
        conversation = query_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))

    # A chat always writes into the project it was created in.
    if conversation.get("project_id") and (
        not project or project["id"] != conversation["project_id"]
    ):
        project = query_one(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (conversation["project_id"], user["id"]),
        )

    attachments = _payload_attachments(payload)
    image_urls = [item["url"] for item in attachments if item["kind"] == "image"]
    execute(
        "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, 'user', ?, ?, ?)",
        (
            conversation["id"],
            message,
            json.dumps(
                {"mode": payload.mode or "chat", "images": image_urls, "attachments": attachments}
            ),
            time.time(),
        ),
    )
    execute(
        "UPDATE conversations SET updated_at = ?, model_name = ?, mode = ?, "
        "project_id = COALESCE(?, project_id) WHERE id = ?",
        (
            time.time(),
            model["name"],
            payload.mode or "chat",
            project["id"] if project else None,
            conversation["id"],
        ),
    )

    history = query(
        "SELECT id, role, content, meta FROM messages WHERE conversation_id = ? "
        "ORDER BY id DESC LIMIT 30",
        (conversation["id"],),
    )
    history.reverse()

    # Re-attach the two most recent images so follow-up questions still see them.
    with_images: list[int] = []
    for row in reversed(history):
        if row["role"] != "user":
            continue
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if any(item["kind"] == "image" for item in _meta_attachments(meta)):
            with_images.append(row["id"])
        if len(with_images) >= 2:
            break

    messages = []
    for row in history:
        content = row["content"]
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        row_attachments = _meta_attachments(meta)
        blocks = []
        if row["id"] in with_images:
            blocks = _image_blocks(
                [item["url"] for item in row_attachments if item["kind"] == "image"]
            )
        text = row["content"]
        notes = _attachment_notes(row_attachments)
        if notes:
            text = text + "\n\n" + notes
        if blocks:
            content = [{"type": "text", "text": text}] + blocks
        else:
            content = text
        messages.append({"role": row["role"], "content": content})

    vps_connection = get_vps_connection(user["id"])
    system_prompt = get_setting("system_prompt", "")
    prefix = []
    if system_prompt:
        prefix.append({"role": "system", "content": system_prompt})
    if payload.think:
        prefix.append({"role": "system", "content": THINK_PROMPT})
    is_cursor = provider_of(model) == "cursor"
    if is_cursor:
        prefix.append(
            {
                "role": "system",
                "content": (
                    "You are a Cursor cloud agent working on a GitHub repository. Make the "
                    "changes directly in the repository and reply with a short summary of what "
                    "you changed. Never paste whole files into the chat."
                ),
            }
        )
    project_tree: list[dict] = []
    if project and payload.mode == "code" and not is_cursor:
        tree = read_tree(project["root_path"], 60)
        project_tree = tree
        listing = (
            "\n".join(f"- {item['path']} ({item['bytes']} B)" for item in tree)
            or "(empty folder)"
        )
        prompt = PROJECT_PROMPT.format(root=project["root_path"], listing=listing)
        prompt += COMPUTER_PROMPT if project.get("computer_access") else COMPUTER_OFF_PROMPT
        prefix.append({"role": "system", "content": prompt})
    if vps_connection:
        prefix.append(
            {
                "role": "system",
                "content": VPS_PROMPT.format(
                    host=vps_connection.get("host") or "the VPS",
                    username=vps_connection.get("username") or "the configured user",
                    name=vps_connection.get("name") or "VPS",
                ),
            }
        )
    messages = prefix + messages
    prefix_len = len(prefix)
    web_query = search_query(message)
    wants_web = (
        bool(payload.web)
        and get_setting("web_enabled", "1") == "1"
        and len(web_query.split()) >= 2
    )

    conversation_id = conversation["id"]
    title = conversation["title"]
    user_id = user["id"]
    is_admin = user["role"] == "admin"

    async def event_stream():
        yield _sse({"type": "meta", "conversation_id": conversation_id, "title": title})
        if project_tree:
            yield _sse(
                {
                    "type": "activity",
                    "kind": "read",
                    "label": "Read the project files",
                    "detail": f"{len(project_tree)} files in {project['root_path']}",
                }
            )
        sources: list[dict] = []
        if wants_web:
            yield _sse({"type": "web", "state": "searching"})
            try:
                context_prompt, sources = await gather_context(web_query)
            except Exception:  # noqa: BLE001 - web access is best effort
                context_prompt, sources = "", []
            if context_prompt:
                messages.insert(prefix_len, {"role": "system", "content": context_prompt})
                yield _sse({"type": "sources", "sources": sources})
            else:
                yield _sse({"type": "web", "state": "empty"})
        chunks: list[str] = []
        usage = None
        error = None
        finish_reason = None
        try:
            async for kind, data in stream_chat(
                model,
                messages,
                effort=payload.effort,
                think=payload.think,
                repo_url=(project or {}).get("repo_url") or "",
            ):
                if kind == "delta":
                    chunks.append(str(data))
                    yield _sse({"type": "delta", "text": data})
                elif kind == "reasoning":
                    yield _sse({"type": "reasoning", "text": data})
                elif kind == "usage" and isinstance(data, dict):
                    usage = data
                elif kind == "finish":
                    finish_reason = str(data)
                elif kind == "error":
                    error = str(data)
                    yield _sse({"type": "error", "text": error})
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            error = str(exc)
            yield _sse({"type": "error", "text": error})

        # Output limits cut both prose and vortex:write blocks. Ask only for the
        # missing remainder and append it, so the user gets one complete reply.
        for attempt in range(MAX_CONTINUATIONS):
            partial = "".join(chunks)
            if (
                error
                or not partial.strip()
                or (
                    finish_reason not in LENGTH_FINISH
                    and not _has_truncated_file_ops(partial, project, payload.mode)
                )
            ):
                break
            yield _sse(
                {
                    "type": "activity",
                    "kind": "run",
                    "label": "Continuing the reply",
                    "detail": f"Fetching the missing part ({attempt + 1}/{MAX_CONTINUATIONS}).",
                }
            )
            finish_reason = None
            round_usage = None
            before = len(chunks)
            try:
                async for kind, data in stream_chat(
                    model,
                    messages
                    + [
                        {"role": "assistant", "content": partial},
                        {"role": "user", "content": CONTINUE_PROMPT},
                    ],
                    effort=payload.effort,
                    think=False,
                    repo_url=(project or {}).get("repo_url") or "",
                ):
                    if kind == "delta":
                        chunks.append(str(data))
                        yield _sse({"type": "delta", "text": data})
                    elif kind == "reasoning":
                        yield _sse({"type": "reasoning", "text": data})
                    elif kind == "usage" and isinstance(data, dict):
                        round_usage = data
                    elif kind == "finish":
                        finish_reason = str(data)
                    elif kind == "error":
                        error = str(data)
                        yield _sse({"type": "error", "text": error})
                usage = _merge_usage(usage, round_usage)
            except Exception as exc:  # noqa: BLE001 - keep the partial reply visible
                error = str(exc)
                yield _sse({"type": "error", "text": error})
                break
            if len(chunks) <= before:
                break

        text = "".join(chunks).strip()
        if (
            finish_reason in LENGTH_FINISH
            and not error
            and not _has_truncated_file_ops(text, project, payload.mode)
        ):
            yield _sse(
                {
                    "type": "error",
                    "text": "Automatic continuation reached its limit before the reply finished. "
                    "The last part may still be missing.",
                }
            )
        original = text
        todos, text = parse_todos(text)
        todo_done = 0

        def tick_todo(state="done"):
            nonlocal todo_done
            if todo_done >= len(todos):
                return None
            payload = {"type": "todo", "index": todo_done, "state": state}
            todo_done += 1
            return _sse(payload)

        if todos:
            yield _sse({"type": "todos", "items": todos})
        if text != original:
            yield _sse({"type": "final_text", "text": text})
        if not text and not error:
            error = "The model returned an empty response. Try again, or raise the effort level."
            yield _sse({"type": "error", "text": error})

        applied: list[dict] = []
        denied_files: list[str] = []
        truncated = False
        if project and payload.mode == "code" and text:
            ops = parse_file_ops(text)
            cleaned_text = strip_file_ops(text) if ops else None
            if not ops:
                ops, cleaned_text = extract_standard_files(text)
            truncated = any(op.get("truncated") for op in ops)
            try:
                ready, pending, rejected = categorize(project, ops)
                applied, file_errors = run_ops(ready)
                if applied and cleaned_text is not None:
                    text = cleaned_text or _applied_file_summary(applied)
                    yield _sse({"type": "final_text", "text": text})
                for item in applied:
                    yield _sse(
                        {
                            "type": "file",
                            "action": item["action"],
                            "path": item["path"],
                            "bytes": item["bytes"],
                        }
                    )
                for path, reason in rejected:
                    denied_files.append(path)
                    yield _sse(
                        {
                            "type": "file-denied",
                            "path": path,
                            "reason": reason,
                            "message": "That path is outside the project folder. Enable administrator access for this project to allow it.",
                        }
                    )
                if pending:
                    token, paths = queue_writes(project, pending, user_id)
                    yield _sse({"type": "permission", "token": token, "paths": paths})
                for problem in file_errors:
                    yield _sse({"type": "file-denied", "path": problem, "reason": "error", "message": problem})
                if file_errors and not applied:
                    yield _sse(
                        {
                            "type": "error",
                            "text": "No files could be written into "
                            + project["root_path"]
                            + ". "
                            + file_errors[0][:180]
                            + " — the folder may be read-only or need administrator access.",
                        }
                    )
                if truncated and applied:
                    yield _sse(
                        {
                            "type": "error",
                            "text": "The reply was still cut off after automatic continuation, so "
                            + ", ".join(item["path"].rsplit("\\", 1)[-1] for item in applied[:3])
                            + " may be incomplete. Ask for the rest, or split the page into "
                            "smaller files (styles.css, app.js).",
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - file work must not break the reply
                yield _sse({"type": "file-denied", "path": "", "reason": "error", "message": str(exc)[:200]})
        if applied:
            todo_event = tick_todo()
            if todo_event:
                yield todo_event

        if project and payload.mode == "code" and text:
            commands = parse_commands(text)
            for command in commands:
                yield _sse({"type": "terminal", "state": "running", "command": command})
                try:
                    result = await asyncio.to_thread(run_command, command, project["root_path"])
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "command": command,
                        "code": -1,
                        "output": str(exc)[:500],
                        "timeout": False,
                    }
                yield _sse(
                    {
                        "type": "terminal",
                        "state": "done",
                        "command": command,
                        "code": result["code"],
                        "output": result["output"],
                        "timeout": result["timeout"],
                    }
                )
            if commands:
                todo_event = tick_todo()
                if todo_event:
                    yield todo_event
            try:
                preview = ensure_server(project["id"], project["root_path"])
            except Exception:  # noqa: BLE001
                preview = None
            if preview:
                yield _sse({"type": "preview", **preview})
            stripped = strip_commands(text)
            if stripped != text:
                text = stripped
                yield _sse({"type": "final_text", "text": text})

        vps_commands = parse_vps_commands(text)
        if vps_commands:
            if not vps_connection:
                yield _sse(
                    {
                        "type": "vps",
                        "state": "error",
                        "command": "",
                        "error": "No VPS is connected. Add the SSH details in Settings first.",
                    }
                )
            else:
                for command in vps_commands:
                    yield _sse(
                        {
                            "type": "vps",
                            "state": "running",
                            "command": command,
                            "host": vps_connection.get("host") or "",
                        }
                    )
                    try:
                        result = await asyncio.to_thread(
                            run_vps_command, vps_connection, command
                        )
                    except Exception as exc:  # noqa: BLE001 - shown in the chat
                        result = {
                            "command": command,
                            "code": -1,
                            "output": "",
                            "error": str(exc)[:300],
                            "seconds": 0,
                        }
                    yield _sse(
                        {
                            "type": "vps",
                            "state": "done",
                            "command": command,
                            "code": result["code"],
                            "output": result["output"],
                            "error": result["error"],
                            "seconds": result["seconds"],
                            "host": vps_connection.get("host") or "",
                        }
                    )
            text = strip_vps_commands(text)
            yield _sse({"type": "final_text", "text": text})

        screenshots: list[str] = []
        computer_actions, text = parse_computer_actions(text)
        if computer_actions:
            if project and project.get("computer_access"):
                yield _sse(
                    {
                        "type": "activity",
                        "kind": "run",
                        "label": "Testing what was built",
                        "detail": f"{len(computer_actions)} computer action(s)",
                    }
                )
                launched = False
                async for result in run_computer_actions(computer_actions, project):
                    if result.get("url"):
                        screenshots.append(result["url"])
                    launched = launched or result.get("action") == "launch"
                    yield _sse({"type": "computer", **result})
                if launched and not screenshots:
                    try:
                        shot = await asyncio.to_thread(capture_screen, "after launch")
                        screenshots.append(shot["url"])
                        yield _sse({"type": "computer", **shot})
                    except Exception as exc:  # noqa: BLE001
                        yield _sse(
                            {
                                "type": "computer",
                                "ok": False,
                                "label": "Could not take a screenshot",
                                "detail": str(exc)[:200],
                            }
                        )
                todo_event = tick_todo()
                if todo_event:
                    yield todo_event
            else:
                yield _sse(
                    {
                        "type": "computer-denied",
                        "message": "Computer control is off for this project. Enable it from the project menu to let the AI test what it builds.",
                    }
                )
            yield _sse({"type": "final_text", "text": text})

        if screenshots and not error and project:
            blocks = screenshot_blocks(screenshots)
            if blocks:
                yield _sse({"type": "verify", "state": "checking"})
                verify_messages = messages + [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "The screenshots from the test run are attached. Inspect them. "
                                    "If the app, game or window shows an error, a crash, a blank "
                                    "screen, the wrong result or something missing, fix it now with "
                                    "vortex:write blocks. If it looks correct, say so in one short "
                                    "sentence. Do not repeat the task list."
                                ),
                            }
                        ]
                        + blocks,
                    },
                ]
                vchunks: list[str] = []
                vusage = None
                try:
                    async with asyncio.timeout(90):
                        async for kind, data in stream_chat(
                            model,
                            verify_messages,
                            effort=payload.effort,
                            think=False,
                            repo_url=(project or {}).get("repo_url") or "",
                        ):
                            if kind == "delta":
                                if not vchunks:
                                    yield _sse({"type": "delta", "text": "\n\n"})
                                vchunks.append(str(data))
                                yield _sse({"type": "delta", "text": data})
                            elif kind == "usage" and isinstance(data, dict):
                                vusage = data
                            elif kind == "error":
                                yield _sse(
                                    {
                                        "type": "activity",
                                        "kind": "error",
                                        "label": "Verification failed",
                                        "detail": str(data)[:300],
                                    }
                                )
                except TimeoutError:
                    yield _sse(
                        {
                            "type": "activity",
                            "kind": "error",
                            "label": "Verification timed out",
                            "detail": "The model did not answer the screenshot check within 90 seconds.",
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - verification is best effort
                    yield _sse(
                        {
                            "type": "activity",
                            "kind": "error",
                            "label": "Verification failed",
                            "detail": str(exc)[:300],
                        }
                    )
                vtext = "".join(vchunks).strip()
                if vtext:
                    _, vtext = parse_todos(vtext)
                    _, vtext = parse_computer_actions(vtext)
                    vops = parse_file_ops(vtext)
                    vclean = strip_file_ops(vtext) if vops else None
                    if not vops:
                        vops, vclean = extract_standard_files(vtext)
                    if vops and project:
                        try:
                            ready, pending, rejected = categorize(project, vops)
                            vapplied, verrors = run_ops(ready)
                            applied.extend(vapplied)
                            for item in vapplied:
                                yield _sse(
                                    {
                                        "type": "file",
                                        "action": item["action"],
                                        "path": item["path"],
                                        "bytes": item["bytes"],
                                    }
                                )
                            for path, reason in rejected:
                                yield _sse(
                                    {
                                        "type": "file-denied",
                                        "path": path,
                                        "reason": reason,
                                        "message": "That path is outside the project folder.",
                                    }
                                )
                            for problem in verrors:
                                yield _sse(
                                    {"type": "file-denied", "path": problem, "reason": "error", "message": problem}
                                )
                            if vapplied and vclean is not None:
                                vtext = vclean
                        except Exception as exc:  # noqa: BLE001
                            yield _sse(
                                {
                                    "type": "activity",
                                    "kind": "error",
                                    "label": "Could not apply a verification fix",
                                    "detail": str(exc)[:200],
                                }
                            )
                    vtext = strip_commands(vtext)
                    text = (text + "\n\n" + vtext).strip()
                    yield _sse({"type": "final_text", "text": text})
                    todo_event = tick_todo()
                    if todo_event:
                        yield todo_event
                if isinstance(vusage, dict):
                    if isinstance(usage, dict):
                        usage = {
                            "total_tokens": int(usage.get("total_tokens") or 0)
                            + int(vusage.get("total_tokens") or 0)
                        }
                    else:
                        usage = vusage

        if error and todos and todo_done < len(todos):
            yield _sse({"type": "todo", "index": todo_done, "state": "failed"})
        else:
            while True:
                todo_event = tick_todo()
                if not todo_event:
                    break
                yield todo_event

        assistant_id = None
        if text:
            assistant_id = execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, 'assistant', ?, ?, ?)",
                (
                    conversation_id,
                    text,
                    json.dumps(
                        {
                            "model": model["name"],
                            "effort": payload.effort,
                            "think": payload.think,
                            "web": bool(sources),
                            "sources": sources,
                            "failed": bool(error),
                            "project_id": project["id"] if project else None,
                            "files": [item["path"] for item in applied],
                        }
                    ),
                    time.time(),
                ),
            )

        # Charge only for work that actually completed. A failure on our side
        # (provider error, quota, empty reply) is billed at zero tokens.
        tokens = 0
        refunded = bool(error)
        if text and not error:
            if usage and isinstance(usage, dict):
                tokens = int(usage.get("total_tokens") or 0)
            else:
                tokens = estimate_tokens(messages, text)
            tokens = max(tokens, 1)

            updated_usage = {"unlimited": True}
            if not is_admin:
                updated_usage = charge_model_usage(user, model["id"], tokens)
        else:
            updated_usage = ensure_model_usage(user, model["id"])

        fresh = user_public(query_one("SELECT * FROM users WHERE id = ?", (user_id,)))
        yield _sse(
            {
                "type": "done",
                "message_id": assistant_id,
                "tokens_used": tokens,
                "refunded": refunded,
                "usage": usage,
                "model_usage": usage_public(updated_usage),
                "user": fresh,
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
