"""SSH access to a VPS the user has configured.

The AI never receives the password or private key. It only sees the host and
username in its prompt, then asks the app to run commands through the connection
stored for that user.
"""

from __future__ import annotations

import os
import re
import socket
import tempfile
import time

import paramiko

from .database import execute, query_one

VPS_BLOCK = re.compile(r"```vortex:vps\s*\r?\n(.*?)```", re.S)
MAX_COMMANDS = 5
TIMEOUT_SECONDS = 300
MAX_OUTPUT = 8000


def parse_commands(text: str) -> list[str]:
    commands: list[str] = []
    for raw in VPS_BLOCK.findall(text or ""):
        command = raw.strip()
        if command:
            commands.append(command)
    return commands[:MAX_COMMANDS]


def strip_commands(text: str) -> str:
    return VPS_BLOCK.sub("", text or "").strip()


def get_connection(user_id: int) -> dict | None:
    return query_one("SELECT * FROM vps_connections WHERE user_id = ?", (user_id,))


def public_connection(row: dict | None) -> dict:
    if not row:
        return {
            "configured": False,
            "name": "",
            "host": "",
            "port": 22,
            "username": "",
            "auth": "password",
            "has_password": False,
            "has_key": False,
            "updated_at": None,
        }
    return {
        "configured": True,
        "name": row.get("name") or "",
        "host": row.get("host") or "",
        "port": int(row.get("port") or 22),
        "username": row.get("username") or "",
        "auth": row.get("auth_type") or "password",
        "has_password": bool(row.get("password")),
        "has_key": bool(row.get("private_key")),
        "updated_at": row.get("updated_at"),
    }


def save_connection(
    user_id: int,
    *,
    name: str,
    host: str,
    port: int,
    username: str,
    auth: str,
    password: str,
    private_key: str,
    passphrase: str,
) -> dict:
    existing = get_connection(user_id) or {}
    auth_type = "key" if str(auth).lower() == "key" else "password"
    password = password or (existing.get("password") or "")
    private_key = private_key or (existing.get("private_key") or "")
    passphrase = passphrase or (existing.get("passphrase") or "")
    if auth_type == "password":
        private_key = ""
        passphrase = ""
    else:
        password = ""
    now = time.time()
    execute(
        "INSERT INTO vps_connections (user_id, name, host, port, username, auth_type, "
        "password, private_key, passphrase, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, host = excluded.host, "
        "port = excluded.port, username = excluded.username, auth_type = excluded.auth_type, "
        "password = excluded.password, private_key = excluded.private_key, "
        "passphrase = excluded.passphrase, updated_at = excluded.updated_at",
        (
            user_id,
            (name or "").strip()[:80],
            (host or "").strip()[:255],
            int(port),
            (username or "").strip()[:80],
            auth_type,
            password,
            private_key,
            passphrase,
            existing.get("created_at") or now,
            now,
        ),
    )
    return public_connection(get_connection(user_id))


def delete_connection(user_id: int) -> None:
    execute("DELETE FROM vps_connections WHERE user_id = ?", (user_id,))


def _client(row: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    options = {
        "hostname": row.get("host") or "",
        "port": int(row.get("port") or 22),
        "username": row.get("username") or "",
        "timeout": 20,
        "banner_timeout": 25,
        "auth_timeout": 25,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if (row.get("auth_type") or "password") == "key":
        key_text = row.get("private_key") or ""
        if not key_text.strip():
            raise ValueError("No private key is saved for this VPS.")
        handle, path = tempfile.mkstemp(prefix="vortex-vps-", suffix=".key")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(key_text)
            options["key_filename"] = path
            if row.get("passphrase"):
                options["passphrase"] = row["passphrase"]
            client.connect(**options)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    else:
        options["password"] = row.get("password") or ""
        client.connect(**options)
    return client


def _explain(error: Exception) -> str:
    if isinstance(error, paramiko.AuthenticationException):
        return "The VPS rejected those login details. Check the username, password or key."
    if isinstance(error, paramiko.BadHostKeyException):
        return "The VPS host key changed. Check the server before connecting again."
    if isinstance(error, socket.timeout):
        return "The VPS did not answer in time. Check the IP, port and firewall."
    if isinstance(error, (socket.gaierror, OSError)):
        return f"Could not reach the VPS: {error}"
    if isinstance(error, ValueError):
        return str(error)
    return f"SSH failed: {error}"


def test_connection(row: dict | None) -> dict:
    if not row:
        return {"ok": False, "output": "", "error": "No VPS is configured yet."}
    client = None
    try:
        client = _client(row)
        stdin, stdout, stderr = client.exec_command(
            "printf 'VorteX connected as '; whoami; uname -srm", timeout=25
        )
        stdin.close()
        output = stdout.read().decode("utf-8", "replace")
        output += stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        text = output.strip()[:MAX_OUTPUT]
        if code != 0:
            return {
                "ok": False,
                "output": text,
                "error": text or f"The test command exited with code {code}.",
            }
        return {"ok": True, "output": text, "error": ""}
    except Exception as exc:  # noqa: BLE001 - reported to the settings panel
        return {"ok": False, "output": "", "error": _explain(exc)}
    finally:
        if client is not None:
            client.close()


def run_command(row: dict, command: str) -> dict:
    """Run one non-interactive command over SSH and return its combined output."""
    started = time.time()
    client = None
    try:
        client = _client(row)
        stdin, stdout, stderr = client.exec_command(command, timeout=TIMEOUT_SECONDS)
        stdin.close()
        output = stdout.read().decode("utf-8", "replace")
        output += stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        text = output.strip()
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "\n… output truncated"
        return {
            "command": command,
            "code": code,
            "output": text,
            "error": "",
            "seconds": round(time.time() - started, 1),
        }
    except Exception as exc:  # noqa: BLE001 - surfaced in the chat
        return {
            "command": command,
            "code": -1,
            "output": "",
            "error": _explain(exc),
            "seconds": round(time.time() - started, 1),
        }
    finally:
        if client is not None:
            client.close()
