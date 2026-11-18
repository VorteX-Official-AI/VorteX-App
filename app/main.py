import json
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import updates as updater
from .config import (
    APP_NAME,
    BUNDLED_UPLOAD_DIR,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SECRET_KEY,
    STATIC_DIR,
    TEMPLATES_DIR,
    UPLOAD_DIR,
    WEB_APP_ORIGIN,
)
from .database import (
    get_settings,
    get_user_settings,
    init_db,
    query,
    save_user_settings,
    user_public,
)
from .discord_rpc import APPLICATION_ID, presence
from .routers import admin, auth, billing, chat, projects
from .security import get_current_user, require_user
from .version import __version__
from .vps import (
    delete_connection as delete_vps,
    get_connection as get_vps,
    public_connection as public_vps,
    save_connection as save_vps,
    test_connection as test_vps,
)

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie=SESSION_COOKIE,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
)

# A packaged build keeps uploads in the user data folder, so that mount has to
# win over the bundled /static mount.
if UPLOAD_DIR.resolve() != BUNDLED_UPLOAD_DIR.resolve():
    app.mount("/static/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class PreferencesIn(BaseModel):
    notifications: bool = True
    discord_rpc: bool = True
    model_usage: bool = True


class PresenceIn(BaseModel):
    model: str = ""
    mode: str = "chat"
    streaming: bool = False


class VpsIn(BaseModel):
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    auth: str = "password"
    password: str = ""
    private_key: str = ""
    passphrase: str = ""


@app.middleware("http")
async def _fresh_static_assets(request: Request, call_next):
    """The desktop WebView caches aggressively; always revalidate UI assets."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def asset_version() -> int:
    """A version stamp so a cached script can never outlive its source file."""
    latest = 0
    for folder, suffix in ((STATIC_DIR / "js", "*.js"), (STATIC_DIR / "css", "*.css")):
        for path in folder.glob(suffix):
            try:
                latest = max(latest, int(path.stat().st_mtime))
            except OSError:
                continue
    return latest


templates.env.globals["asset_v"] = asset_version

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(projects.router)
app.include_router(admin.router)
app.include_router(billing.router)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _plans() -> list[dict]:
    rows = query("SELECT * FROM plans WHERE visible = 1 ORDER BY sort_order ASC")
    for row in rows:
        try:
            row["features"] = json.loads(row["features"])
        except json.JSONDecodeError:
            row["features"] = []
    return rows


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "app_name": APP_NAME,
            "user": user_public(user),
            "plans": _plans(),
            "preferences": get_user_settings(user["id"]),
            "settings": get_settings(),
            "version": __version__,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"app_name": APP_NAME})


@app.get("/sso/login")
def sso_login(request: Request, return_to: str = ""):
    """Hand a signed-in VorteX user back to the public website."""
    target = urlparse(return_to)
    allowed = urlparse(WEB_APP_ORIGIN)
    if (
        target.scheme not in ("http", "https")
        or target.scheme != allowed.scheme
        or target.netloc != allowed.netloc
    ):
        raise HTTPException(400, "Invalid VorteX website return address.")
    if not get_current_user(request):
        next_path = "/sso/login?" + urlencode({"return_to": return_to})
        return RedirectResponse("/login?" + urlencode({"next": next_path}), status_code=303)
    return RedirectResponse(return_to, status_code=303)


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "pricing.html",
        {
            "app_name": APP_NAME,
            "user": user_public(user) if user else None,
            "plans": _plans(),
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login?next=/admin", status_code=303)
    if user["role"] != "admin":
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"app_name": APP_NAME, "user": user_public(user)},
    )


@app.get("/api/settings")
def read_settings(user: dict = Depends(require_user)):
    return {
        "preferences": get_user_settings(user["id"]),
        "discord_application_id": APPLICATION_ID,
    }


@app.put("/api/settings")
def write_settings(payload: PreferencesIn, user: dict = Depends(require_user)):
    prefs = save_user_settings(
        user["id"],
        notifications=payload.notifications,
        discord_rpc=payload.discord_rpc,
        model_usage=payload.model_usage,
    )
    if not prefs["discord_rpc"]:
        presence.configure(False)
    return {"ok": True, "preferences": prefs, "presence": presence.status()}


@app.get("/api/presence/status")
def presence_status(user: dict = Depends(require_user)):
    return {
        "enabled": bool(get_user_settings(user["id"])["discord_rpc"]),
        "application_id": APPLICATION_ID,
        **presence.status(),
    }


@app.post("/api/presence/test")
def test_presence(user: dict = Depends(require_user)):
    if not get_user_settings(user["id"])["discord_rpc"]:
        return {
            "ok": False,
            "enabled": False,
            "accepted": False,
            "connected": False,
            "application_id": APPLICATION_ID,
            "error": "Turn on Discord Rich Presence first.",
        }
    result = presence.test()
    return {
        "ok": bool(result.get("accepted")),
        "enabled": True,
        "application_id": APPLICATION_ID,
        **result,
    }


@app.get("/api/vps")
def read_vps(user: dict = Depends(require_user)):
    return {"connection": public_vps(get_vps(user["id"]))}


@app.put("/api/vps")
def write_vps(payload: VpsIn, user: dict = Depends(require_user)):
    host = (payload.host or "").strip()
    username = (payload.username or "").strip()
    if not host:
        raise HTTPException(400, "Enter the VPS IP address or hostname.")
    if not username:
        raise HTTPException(400, "Enter the SSH username.")
    if not 1 <= int(payload.port) <= 65535:
        raise HTTPException(400, "The SSH port must be between 1 and 65535.")
    if payload.auth not in ("password", "key"):
        raise HTTPException(400, "Choose password or private key authentication.")
    existing = get_vps(user["id"]) or {}
    if payload.auth == "password":
        has_secret = bool(payload.password or existing.get("password"))
        if not has_secret:
            raise HTTPException(400, "Enter the SSH password or private key.")
    else:
        has_secret = bool(payload.private_key or existing.get("private_key"))
        if not has_secret:
            raise HTTPException(400, "Paste the SSH private key or choose a password.")
    connection = save_vps(
        user["id"],
        name=payload.name,
        host=host,
        port=int(payload.port),
        username=username,
        auth=payload.auth,
        password=payload.password,
        private_key=payload.private_key,
        passphrase=payload.passphrase,
    )
    return {"ok": True, "connection": connection}


@app.delete("/api/vps")
def remove_vps(user: dict = Depends(require_user)):
    delete_vps(user["id"])
    return {"ok": True, "connection": public_vps(None)}


@app.post("/api/vps/test")
def check_vps(user: dict = Depends(require_user)):
    result = test_vps(get_vps(user["id"]))
    return {"ok": bool(result.get("ok")), **result}


@app.post("/api/presence")
def update_presence(payload: PresenceIn, user: dict = Depends(require_user)):
    prefs = get_user_settings(user["id"])
    if not prefs["discord_rpc"]:
        presence.configure(False)
        return {"ok": True, "enabled": False, "application_id": APPLICATION_ID, **presence.status()}
    mode = "Code" if payload.mode == "code" else "Chat"
    model = (payload.model or "").strip()[:64]
    parts = ["VorteX AI", mode]
    if model:
        parts.append(model)
    if payload.streaming:
        parts.append("Generating")
    presence.configure(True, state=" · ".join(parts))
    return {"ok": True, "enabled": True, "application_id": APPLICATION_ID, **presence.status()}


@app.delete("/api/presence")
def stop_presence(user: dict = Depends(require_user)):
    presence.configure(False)
    return {"ok": True, "enabled": False, "application_id": APPLICATION_ID, **presence.status()}


@app.get("/api/updates")
def read_updates(refresh: int = 0, user: dict = Depends(require_user)):
    """Report the installed version and any newer release on the channel."""
    data = updater.check(force=bool(refresh))
    return {**data, "download_state": updater.download_state()}


@app.post("/api/updates/download")
def start_update_download(user: dict = Depends(require_user)):
    return updater.start_download()


@app.get("/api/updates/download")
def read_update_download(user: dict = Depends(require_user)):
    return updater.download_state()


@app.post("/api/updates/download/cancel")
def cancel_update_download(user: dict = Depends(require_user)):
    return updater.cancel_download()


@app.post("/api/updates/install")
def install_update(user: dict = Depends(require_user)):
    return updater.launch_installer()


@app.post("/api/updates/manual")
def manual_update_download(user: dict = Depends(require_user)):
    return updater.open_manual_download()


@app.get("/health")
def health():
    return JSONResponse({"status": "ok", "app": APP_NAME, "version": __version__})
