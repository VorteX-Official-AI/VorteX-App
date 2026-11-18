"""Project file operations: parse model file blocks and apply them safely."""

import os
import re
from pathlib import Path

# The closing fence is optional: a reply cut off at the output limit still yields a file.
FILE_BLOCK = re.compile(
    r"```vortex:(write|delete)\s+path=[\"']([^\"'\n]+)[\"']\s*\r?\n?(.*?)(```|\Z)",
    re.S,
)

# App-owned metadata folder inside every project; never offered to the AI or the preview.
META_DIR = ".vortexai"

CODE_BLOCK = re.compile(
    r"```([A-Za-z0-9_+\-]*)[ \t]*([^\n`]*)\r?\n(.*?)(```|\Z)", re.S
)

LANG_EXT = {
    "html": "html",
    "html5": "html",
    "xhtml": "html",
    "htm": "html",
    "css": "css",
    "js": "js",
    "javascript": "js",
    "mjs": "js",
    "jsx": "jsx",
    "ts": "ts",
    "typescript": "ts",
    "tsx": "tsx",
    "json": "json",
    "python": "py",
    "py": "py",
    "sql": "sql",
    "bash": "sh",
    "sh": "sh",
    "markdown": "md",
    "md": "md",
    "txt": "txt",
    "text": "txt",
    "xml": "xml",
    "svg": "svg",
    "yaml": "yml",
    "yml": "yml",
    "php": "php",
    "java": "java",
    "cs": "cs",
    "go": "go",
    "rs": "rs",
    "c": "c",
    "h": "h",
    "cpp": "cpp",
    "hpp": "hpp",
    "csharp": "cs",
    "kotlin": "kt",
    "gradle": "gradle",
    "kts": "kts",
    "properties": "properties",
    "toml": "toml",
    "ini": "ini",
    "lua": "lua",
    "rb": "rb",
    "ruby": "rb",
    "pl": "pl",
    "dart": "dart",
    "swift": "swift",
    "scala": "scala",
    "groovy": "groovy",
    "bat": "bat",
    "cmd": "bat",
    "powershell": "ps1",
    "ps1": "ps1",
    "glsl": "glsl",
    "vue": "vue",
    "svelte": "svelte",
    "csproj": "csproj",
}

HTML_NAMES = [
    "index.html",
    "about.html",
    "services.html",
    "contact.html",
    "login.html",
    "dashboard.html",
    "pricing.html",
    "checkout.html",
    "cart.html",
    "admin.html",
]

KNOWN_FILENAME = re.compile(
    r"([A-Za-z0-9_./\\-]+\.(?:html|css|js|jsx|ts|tsx|json|py|sql|sh|md|txt|xml|svg|yml|yaml|"
    r"php|java|kt|kts|cs|go|rs|c|cpp|h|hpp|lua|rb|pl|dart|swift|scala|groovy|gradle|"
    r"properties|toml|ini|cfg|bat|cmd|ps1|csproj|sln|glsl|frag|vert|vue|svelte))",
    re.I,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def parse_file_ops(text: str) -> list[dict]:
    ops: list[dict] = []
    for action, raw_path, body, closing in FILE_BLOCK.findall(text or ""):
        path = raw_path.strip()
        if not path:
            continue
        ops.append(
            {
                "action": action,
                "path": path,
                "content": body.replace("\r\n", "\n").rstrip("\n") + "\n",
                "truncated": not closing,
            }
        )
    return ops[:12]


def strip_file_ops(text: str) -> str:
    cleaned = FILE_BLOCK.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


RUN_BLOCK = re.compile(r"```vortex:run\s*\r?\n?(.*?)(```|\Z)", re.S)


def strip_commands(text: str) -> str:
    """Hide the command blocks after they have been executed."""
    cleaned = RUN_BLOCK.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _filename_hint(header: str, body: str) -> str:
    """Only trust an explicit marker, never filenames mentioned inside code."""
    candidates = [header or ""]
    first_line = (body or "").splitlines()[0] if (body or "").strip() else ""
    stripped = first_line.strip().strip("/*#<>-\u2014 ")
    if stripped.lower().startswith("file:"):
        stripped = stripped[5:].strip()
    candidates.append(stripped)

    for candidate in candidates:
        text = candidate.strip()
        if not text:
            continue
        match = KNOWN_FILENAME.fullmatch(text)
        if match:
            return match.group(1)
    return ""


DEFAULT_STEMS = {
    "css": "styles",
    "js": "app",
    "py": "main",
    "json": "data",
    "md": "README",
    "txt": "notes",
    "sh": "build",
    "yml": "config",
    "yaml": "config",
    "sql": "schema",
    "xml": "data",
    "svg": "icon",
    "php": "index",
    "go": "main",
    "rs": "main",
    "c": "main",
    "cpp": "main",
    "h": "main",
    "hpp": "main",
    "lua": "main",
    "rb": "main",
    "pl": "main",
    "dart": "main",
    "swift": "main",
    "scala": "Main",
    "groovy": "build",
    "bat": "build",
    "ps1": "build",
    "glsl": "shader",
    "vue": "App",
    "svelte": "App",
    "ini": "config",
    "toml": "config",
    "java": "Main",
    "kt": "Main",
    "cs": "Program",
}

FIXED_NAMES = {
    "gradle": "build.gradle",
    "kts": "build.gradle.kts",
    "properties": "gradle.properties",
    "csproj": "project.csproj",
}


def _declared_stem(ext: str, code: str) -> str:
    """Use the class (and Java/Kotlin package) the code declares as its filename."""
    if ext not in {"java", "kt", "kts", "cs", "scala"}:
        return ""
    package = re.search(r"^\s*package\s+([A-Za-z_][\w.]*)", code, re.M)
    declared = re.search(
        r"\b(?:class|interface|enum|record|object|struct)\s+([A-Za-z_]\w*)", code
    )
    if not declared:
        return ""
    stem = declared.group(1)
    if package and ext in {"java", "kt"}:
        return package.group(1).replace(".", "/") + "/" + stem
    return stem


def _default_path(ext: str, code: str) -> str:
    if ext in FIXED_NAMES:
        return FIXED_NAMES[ext]
    stem = _declared_stem(ext, code) or DEFAULT_STEMS.get(ext, "file")
    return f"{stem}.{ext}"


def extract_standard_files(text: str) -> tuple[list[dict], str]:
    """Turn plain ```html / ```css / ```js blocks into files with sensible names."""
    ops: list[dict] = []
    used: set[str] = set()
    counters: dict[str, int] = {}
    spans: list[tuple[int, int]] = []
    source = text or ""

    for match in CODE_BLOCK.finditer(source):
        lang = (match.group(1) or "").lower()
        header = match.group(2) or ""
        code = (match.group(3) or "").rstrip()
        closing = match.group(4) or ""
        ext = LANG_EXT.get(lang)
        if not ext or len(code.strip()) < 20:
            continue

        hint = _filename_hint(header, code)

        if hint:
            path = hint.replace("\\", "/").lstrip("./")
            if not path.lower().endswith("." + ext):
                path = path.rsplit(".", 1)[0] + "." + ext
        else:
            count = counters.get(ext, 0)
            counters[ext] = count + 1
            if ext == "html":
                path = HTML_NAMES[count] if count < len(HTML_NAMES) else f"page-{count + 1}.html"
            else:
                path = _default_path(ext, code)
                if count:
                    stem, _, suffix = path.rpartition(".")
                    path = f"{stem}-{count + 1}.{suffix}"

        if path in used:
            stem = path.rsplit(".", 1)[0]
            path = f"{stem}-{len(used) + 1}.{ext}"
        used.add(path)
        ops.append(
            {
                "action": "write",
                "path": path,
                "content": code + "\n",
                "truncated": not closing,
            }
        )
        spans.append((match.start(), match.end()))

    cleaned = source
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return ops[:12], cleaned


def _inside(root: str, target: str) -> bool:
    try:
        return Path(target).resolve().is_relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False


def resolve_target(root: str, raw: str) -> str:
    raw = raw.strip().strip("\"'")
    target = Path(raw)
    if not target.is_absolute():
        target = Path(root) / raw
    return str(target.resolve())


def allowed_roots(project_id: int) -> list[str]:
    from .database import query

    rows = query(
        "SELECT path FROM project_permissions WHERE project_id = ? AND allow = 1",
        (project_id,),
    )
    return [row["path"] for row in rows]


def grant_root(project_id: int, target: str) -> None:
    """Remember the folder a user approved so we do not ask again for it."""
    from .database import execute
    import time

    folder = str(Path(target).parent if Path(target).suffix else Path(target))
    execute(
        "INSERT INTO project_permissions (project_id, path, allow, created_at) VALUES (?, ?, 1, ?)",
        (project_id, folder, time.time()),
    )


def write_text(path: str, content: str) -> int:
    target = Path(path)
    if target.exists() and target.is_dir():
        raise OSError("That path is a folder.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise OSError("File is larger than 2 MB.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)


def delete_path(path: str) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    if target.is_dir():
        try:
            target.rmdir()
            return True
        except OSError:
            raise OSError("Folder is not empty.")
    target.unlink()
    return True


def read_tree(root: str, limit: int = 200) -> list[dict]:
    """List files in a project so the model can see what exists."""
    base = Path(root)
    if not base.is_dir():
        return []
    entries: list[dict] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            path = Path(current) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append(
                {
                    "path": str(path.relative_to(base)).replace("\\", "/"),
                    "bytes": size,
                }
            )
            if len(entries) >= limit:
                return entries
    return entries


def categorize(project: dict, ops: list[dict]) -> tuple[list[dict], list[dict], list[tuple[str, str]]]:
    """Split operations into auto-applied, needs-permission and denied."""
    root = project["root_path"]
    allowed = allowed_roots(project["id"])
    applied: list[dict] = []
    pending: list[dict] = []
    denied: list[tuple[str, str]] = []

    for op in ops:
        if META_DIR in Path(op["path"]).parts:
            denied.append((op["path"], "reserved"))
            continue
        target = resolve_target(root, op["path"])
        inside = _inside(root, target) or any(_inside(base, target) for base in allowed)
        if inside:
            applied.append({"action": op["action"], "path": target, "content": op["content"]})
        elif project.get("admin_access"):
            pending.append({"action": op["action"], "path": target, "content": op["content"]})
        else:
            denied.append((target, "outside"))

    return applied, pending, denied


def run_ops(items: list[dict]) -> tuple[list[dict], list[str]]:
    done: list[dict] = []
    errors: list[str] = []
    for item in items:
        try:
            if item["action"] == "write":
                size = write_text(item["path"], item["content"])
                done.append({"action": "write", "path": item["path"], "bytes": size})
            else:
                removed = delete_path(item["path"])
                done.append({"action": "delete", "path": item["path"], "bytes": 0, "removed": removed})
        except OSError as exc:
            errors.append(f"{item['path']}: {exc}")
    return done, errors
