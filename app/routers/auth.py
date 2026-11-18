import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..database import execute, query_one, user_public
from ..security import (
    get_current_user,
    hash_password,
    is_valid_email,
    login_session,
    logout_session,
    password_problem,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

FREE_TOKENS = 10_000

MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 15 * 60
LOCKOUT_SECONDS = 5 * 60

ATTEMPTS: dict[str, list[float]] = {}
ATTEMPT_LOCK = threading.Lock()

# A throwaway hash so a missing account costs the same time as a wrong password.
_DUMMY_HASH = hash_password("not-a-real-password-4831")


class RegisterIn(BaseModel):
    name: str = ""
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


def _attempt_key(email: str, request: Request) -> str:
    host = request.client.host if request.client else "local"
    return f"{host}|{email}"


def _lock_seconds(key: str) -> int:
    now = time.time()
    with ATTEMPT_LOCK:
        stamps = [stamp for stamp in ATTEMPTS.get(key, []) if now - stamp < ATTEMPT_WINDOW]
        ATTEMPTS[key] = stamps
        if len(stamps) < MAX_ATTEMPTS:
            return 0
        remaining = LOCKOUT_SECONDS - (now - stamps[-1])
    return max(1, int(remaining))


def _record_failure(key: str) -> None:
    with ATTEMPT_LOCK:
        ATTEMPTS.setdefault(key, []).append(time.time())


def _clear_failures(key: str) -> None:
    with ATTEMPT_LOCK:
        ATTEMPTS.pop(key, None)


@router.post("/register")
def register(payload: RegisterIn, request: Request):
    email = (payload.email or "").strip().lower()
    name = (payload.name or "").strip() or email.split("@")[0]
    password = payload.password or ""

    if not is_valid_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    problem = password_problem(password)
    if problem:
        raise HTTPException(400, problem)
    if query_one("SELECT id FROM users WHERE email = ?", (email,)):
        raise HTTPException(409, "An account with that email already exists.")

    user_id = execute(
        "INSERT INTO users (email, name, password_hash, role, plan, tokens_left, credits_left, created_at) "
        "VALUES (?, ?, ?, 'user', 'free', ?, ?, ?)",
        (email, name[:60], hash_password(password), FREE_TOKENS, FREE_TOKENS, time.time()),
    )
    request.session.clear()
    login_session(request, user_id)
    return {"user": user_public(query_one("SELECT * FROM users WHERE id = ?", (user_id,)))}


@router.post("/login")
def login(payload: LoginIn, request: Request):
    email = (payload.email or "").strip().lower()
    key = _attempt_key(email, request)

    wait = _lock_seconds(key)
    if wait:
        raise HTTPException(
            429, f"Too many sign-in attempts. Try again in {wait} seconds."
        )

    user = query_one("SELECT * FROM users WHERE email = ?", (email,))
    stored = user["password_hash"] if user else _DUMMY_HASH
    matches = verify_password(payload.password or "", stored)
    if not user or not matches:
        _record_failure(key)
        raise HTTPException(401, "Incorrect email or password.")

    _clear_failures(key)
    request.session.clear()
    login_session(request, user["id"])
    return {"user": user_public(user)}


@router.post("/logout")
def logout(request: Request):
    logout_session(request)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in.")
    return {"user": user_public(user)}
