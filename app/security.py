import base64
import hashlib
import hmac
import os
import re

from fastapi import HTTPException, Request

PBKDF2_ROUNDS = 260_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = stored.split("$")
    except (AttributeError, ValueError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


COMMON_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "12345678",
    "123456789",
    "1234567890",
    "qwerty123",
    "letmein123",
    "iloveyou1",
    "admin12345",
    "welcome123",
}


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def password_problem(password: str) -> str | None:
    """Return a human-readable reason the password is too weak, or None."""
    value = password or ""
    if len(value) < 10:
        return "Use at least 10 characters."
    if len(value) > 200:
        return "That password is too long (200 characters maximum)."
    if not re.search(r"[A-Za-z]", value):
        return "Include at least one letter."
    if not re.search(r"\d", value):
        return "Include at least one number."
    if value.lower() in COMMON_PASSWORDS:
        return "That password is too common — pick something less guessable."
    return None


def login_session(request: Request, user_id: int) -> None:
    request.session["uid"] = user_id


def logout_session(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request):
    from .database import query_one

    user_id = request.session.get("uid")
    if not user_id:
        return None
    return query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user
