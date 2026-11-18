import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    DB_PATH,
    DEFAULT_PLANS,
    DEFAULT_SETTINGS,
)
from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    plan TEXT NOT NULL DEFAULT 'free',
    tokens_left INTEGER NOT NULL DEFAULT 10000,
    credits_left INTEGER NOT NULL DEFAULT 10000,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    credits INTEGER NOT NULL,
    tagline TEXT,
    features TEXT NOT NULL DEFAULT '[]',
    badge TEXT,
    visible INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openai',
    base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
    api_key TEXT NOT NULL,
    logo TEXT,
    description TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT 'New chat',
    mode TEXT NOT NULL DEFAULT 'chat',
    model_name TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan_slug TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT 'paypal',
    status TEXT NOT NULL DEFAULT 'pending',
    external_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS model_usage (
    user_id INTEGER NOT NULL,
    model_pk INTEGER NOT NULL,
    tokens_left INTEGER NOT NULL,
    credits_left INTEGER NOT NULL,
    tokens_total INTEGER NOT NULL,
    credits_total INTEGER NOT NULL,
    period_started REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, model_pk),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (model_pk) REFERENCES models(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    notifications INTEGER NOT NULL DEFAULT 1,
    discord_rpc INTEGER NOT NULL DEFAULT 1,
    model_usage INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS vps_connections (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 22,
    username TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'password',
    password TEXT NOT NULL DEFAULT '',
    private_key TEXT NOT NULL DEFAULT '',
    passphrase TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    repo_url TEXT NOT NULL DEFAULT '',
    admin_access INTEGER NOT NULL DEFAULT 0,
    computer_access INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    allow INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_user ON model_usage(user_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with db() as conn:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with db() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with db() as conn:
        cur = conn.execute(sql, tuple(params))
        return int(cur.lastrowid or 0)


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_settings() -> dict[str, str]:
    return {row["key"]: row["value"] for row in query("SELECT key, value FROM settings")}


def get_user_settings(user_id: int) -> dict:
    row = query_one("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    if not row:
        return {"notifications": True, "discord_rpc": True, "model_usage": True}
    return {
        "notifications": bool(row["notifications"]),
        "discord_rpc": bool(row["discord_rpc"]),
        "model_usage": bool(row["model_usage"]),
    }


def save_user_settings(
    user_id: int,
    notifications: bool,
    discord_rpc: bool,
    model_usage: bool,
) -> dict:
    execute(
        "INSERT INTO user_settings (user_id, notifications, discord_rpc, model_usage, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "notifications = excluded.notifications, discord_rpc = excluded.discord_rpc, "
        "model_usage = excluded.model_usage, updated_at = excluded.updated_at",
        (
            user_id,
            1 if notifications else 0,
            1 if discord_rpc else 0,
            1 if model_usage else 0,
            time.time(),
        ),
    )
    return get_user_settings(user_id)


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)

    # Older databases predate multi-provider support.
    columns = {row["name"] for row in query("PRAGMA table_info(models)")}
    if "provider" not in columns:
        execute("ALTER TABLE models ADD COLUMN provider TEXT NOT NULL DEFAULT 'openai'")

    # Code-mode projects attach conversations to a folder on this PC.
    conversation_columns = {row["name"] for row in query("PRAGMA table_info(conversations)")}
    if "project_id" not in conversation_columns:
        execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
    execute("CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id)")

    # Cursor Cloud Agents run against a GitHub repository.
    project_columns = {row["name"] for row in query("PRAGMA table_info(projects)")}
    if "repo_url" not in project_columns:
        execute("ALTER TABLE projects ADD COLUMN repo_url TEXT NOT NULL DEFAULT ''")
    if "computer_access" not in project_columns:
        execute("ALTER TABLE projects ADD COLUMN computer_access INTEGER NOT NULL DEFAULT 0")

    now = time.time()

    for plan in DEFAULT_PLANS:
        execute(
            "INSERT INTO plans (slug, name, price_cents, tokens, credits, tagline, features, badge, visible, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(slug) DO NOTHING",
            (
                plan["slug"],
                plan["name"],
                plan["price_cents"],
                plan["tokens"],
                plan["credits"],
                plan["tagline"],
                json.dumps(plan["features"]),
                plan["badge"],
                plan["visible"],
                plan["sort_order"],
            ),
        )

    for key, value in DEFAULT_SETTINGS.items():
        execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )

    # Only bootstrap an admin when credentials are supplied through the environment.
    if ADMIN_EMAIL and ADMIN_PASSWORD:
        admin = query_one("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,))
        if not admin:
            execute(
                "INSERT INTO users (email, name, password_hash, role, plan, tokens_left, credits_left, created_at) "
                "VALUES (?, ?, ?, 'admin', 'ultra', 999999999, 999999999, ?)",
                (ADMIN_EMAIL, "VorteX Admin", hash_password(ADMIN_PASSWORD), now),
            )
        else:
            execute(
                "UPDATE users SET role = 'admin' WHERE email = ?",
                (ADMIN_EMAIL,),
            )


def user_public(user: dict) -> dict:
    if not user:
        return {}
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "plan": user["plan"],
        "tokens_left": user["tokens_left"],
        "credits_left": user["credits_left"],
    }
