"""Run terminal commands the model asks for inside a project folder."""

import os
import re
import subprocess
import sys

RUN_BLOCK = re.compile(r"```vortex:run\s*\r?\n(.*?)```", re.S)
MAX_COMMANDS = 5
TIMEOUT_SECONDS = 120
MAX_OUTPUT = 6000


def parse_commands(text: str) -> list[str]:
    commands: list[str] = []
    for raw in RUN_BLOCK.findall(text or ""):
        command = raw.strip()
        if command:
            commands.append(command)
    return commands[:MAX_COMMANDS]


def run_command(command: str, cwd: str) -> dict:
    """Run one command in the project folder and return its output."""
    if not os.path.isdir(cwd):
        return {"command": command, "code": -1, "output": "Project folder is missing.", "timeout": False}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    # Let scripts in the project folder (build.bat, ./gradlew …) resolve by name.
    env["PATH"] = cwd + os.pathsep + env.get("PATH", "")

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            env=env,
        )
    except OSError as exc:
        return {"command": command, "code": -1, "output": f"Could not start: {exc}", "timeout": False}

    timed_out = False
    try:
        output, _ = process.communicate(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = ""

    text = (output or "").strip()
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n… output truncated"
    if timed_out:
        text += f"\n[stopped after {TIMEOUT_SECONDS}s — use a non-interactive, short command]"
    return {
        "command": command,
        "code": process.returncode if process.returncode is not None else -1,
        "output": text,
        "timeout": timed_out,
    }


def shell_label() -> str:
    return "cmd" if sys.platform == "win32" else "sh"
