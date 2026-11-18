import os
import secrets
import sys
from pathlib import Path


FROZEN = bool(getattr(sys, "frozen", False))


def _resource_root() -> Path:
    """Where the bundled templates and static files live."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    """A writable per-user folder; the install directory may be read-only."""
    override = os.environ.get("VORTEX_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if FROZEN:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return base / "VorteX AI"
    return Path(__file__).resolve().parent.parent / "data"


BASE_DIR = _resource_root()
APP_DIR = BASE_DIR / "app"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"
DATA_DIR = _data_root()
# Uploads follow the user data folder in a packaged build so Program Files can
# stay read-only; the server mounts this directory under /static/uploads.
UPLOAD_DIR = (DATA_DIR / "uploads") if FROZEN else (STATIC_DIR / "uploads")
MODEL_LOGOS_DIR = UPLOAD_DIR / "models"
CHAT_UPLOAD_DIR = UPLOAD_DIR / "chats"
BUNDLED_UPLOAD_DIR = STATIC_DIR / "uploads"
DB_PATH = DATA_DIR / "vortex.db"
# The app and the website both charge this ledger, so an exhausted balance is
# the same on both surfaces.
SHARED_DB_PATH = Path(
    os.environ.get("VORTEX_SHARED_DB", str(DATA_DIR / "shared_usage.db"))
)

for _dir in (DATA_DIR, UPLOAD_DIR, MODEL_LOGOS_DIR, CHAT_UPLOAD_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

CHAT_UPLOAD_URL = "/static/uploads/chats/"
ALLOWED_IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_CHAT_FILE_BYTES = 20 * 1024 * 1024

def _session_secret() -> str:
    """Use VORTEX_SECRET_KEY, or a random key kept in the data folder."""
    configured = os.environ.get("VORTEX_SECRET_KEY")
    if configured:
        return configured
    key_file = DATA_DIR / ".session_secret"
    try:
        if key_file.is_file():
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding="utf-8")
        return generated
    except OSError:
        # A per-process key keeps the app running even on a read-only disk.
        return secrets.token_urlsafe(48)


SECRET_KEY = _session_secret()
SESSION_COOKIE = "vortex_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
WEB_APP_ORIGIN = os.environ.get("VORTEX_WEB_ORIGIN", "http://127.0.0.1:8100").rstrip("/")

APP_NAME = "VorteX AI"
APP_TAGLINE = "The AI workspace that thinks, writes and ships."

# The optional first admin account is created only when both variables are set,
# so a fresh checkout never ships with a known login.
ADMIN_EMAIL = os.environ.get("VORTEX_ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.environ.get("VORTEX_ADMIN_PASSWORD", "")

PAYPAL_API = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

DEFAULT_PLANS = [
    {
        "slug": "basic",
        "name": "BASIC",
        "price_cents": 1099,
        "tokens": 100_000,
        "credits": 100_000,
        "tagline": "For everyday questions and quick builds.",
        "badge": None,
        "features": [
            "100,000 tokens",
            "100,000 credits",
            "Chat + Code mode",
            "Think mode & effort control",
            "All available AI models",
        ],
        "visible": 1,
        "sort_order": 1,
    },
    {
        "slug": "vip",
        "name": "VIP",
        "price_cents": 2599,
        "tokens": 500_000,
        "credits": 500_000,
        "tagline": "For power users shipping every day.",
        "badge": "POPULAR",
        "features": [
            "500,000 tokens",
            "500,000 credits",
            "Chat + Code mode",
            "Think mode & effort control",
            "Priority model routing",
        ],
        "visible": 1,
        "sort_order": 2,
    },
    {
        "slug": "ultra",
        "name": "ULTRA",
        "price_cents": 3599,
        "tokens": 1_000_000,
        "credits": 1_000_000,
        "tagline": "Maximum power for heavy workloads.",
        "badge": "BEST VALUE",
        "features": [
            "1,000,000 tokens",
            "1,000,000 credits",
            "Chat + Code mode",
            "Think mode & effort control",
            "Highest effort priority",
        ],
        "visible": 1,
        "sort_order": 3,
    },
    {
        "slug": "free",
        "name": "FREE",
        "price_cents": 0,
        "tokens": 10_000,
        "credits": 10_000,
        "tagline": "Starter trial included with every account.",
        "badge": None,
        "features": ["10,000 tokens", "10,000 credits", "Chat + Code mode"],
        "visible": 0,
        "sort_order": 0,
    },
]

DEFAULT_SETTINGS = {
    "paypal_enabled": "1",
    "paypal_mode": "demo",
    "paypal_client_id": "",
    "paypal_client_secret": "",
    "paypal_currency": "USD",
    "brand_tagline": APP_TAGLINE,
    "default_effort": "medium",
    "web_enabled": "1",
    "web_provider": "auto",
    "web_pages": "2",
    "web_google_key": "",
    "web_google_cx": "",
    "web_brave_key": "",
    "web_tavily_key": "",
    "web_serper_key": "",
    "web_exa_key": "",
    "web_github_token": "",
    "web_hf_token": "",
    "system_prompt": "You are VorteX AI, a precise and helpful assistant. Answer clearly, use Markdown, and keep code correct.",
}

ALLOWED_LOGO_EXT = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
MAX_LOGO_BYTES = 2 * 1024 * 1024
