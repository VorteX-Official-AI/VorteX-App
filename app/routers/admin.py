import json
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..ai import check_connection, normalize_provider
from ..ai import list_models as list_provider_models
from ..config import ALLOWED_LOGO_EXT, MAX_LOGO_BYTES, MODEL_LOGOS_DIR
from ..database import execute, get_settings, query, query_one, set_setting
from ..security import require_admin
from ..web import test_search

router = APIRouter(prefix="/api/admin", tags=["admin"])


class PaymentIn(BaseModel):
    paypal_enabled: bool = True
    paypal_mode: str = "demo"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_currency: str = "USD"


class WebIn(BaseModel):
    web_enabled: bool = True
    web_provider: str = "auto"
    web_pages: int = 2
    web_google_key: str = ""
    web_google_cx: str = ""
    web_brave_key: str = ""
    web_tavily_key: str = ""
    web_serper_key: str = ""
    web_exa_key: str = ""
    web_github_token: str = ""
    web_hf_token: str = ""


class DiscoverIn(BaseModel):
    provider: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model_pk: int | None = None


class PlanIn(BaseModel):
    slug: str
    name: str
    price_cents: int
    tokens: int
    credits: int
    tagline: str = ""
    features: list[str] = []
    badge: str | None = None
    visible: bool = True


async def _save_logo(logo: UploadFile | None) -> str | None:
    if not logo or not logo.filename:
        return None
    suffix = Path(logo.filename).suffix.lower()
    if suffix not in ALLOWED_LOGO_EXT:
        raise HTTPException(400, "Logo must be a PNG, JPG, WEBP, GIF or SVG file.")
    data = await logo.read()
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(400, "Logo must be smaller than 2 MB.")
    filename = f"{uuid.uuid4().hex}{suffix}"
    (MODEL_LOGOS_DIR / filename).write_bytes(data)
    return f"/static/uploads/models/{filename}"


async def _delete_logo(logo_path: str | None) -> None:
    if not logo_path or not logo_path.startswith("/static/uploads/models/"):
        return
    target = MODEL_LOGOS_DIR / Path(logo_path).name
    try:
        if target.exists():
            target.unlink()
    except OSError:
        pass


@router.get("/stats")
def stats(admin: dict = Depends(require_admin)):
    counts = {
        "users": query_one("SELECT COUNT(*) AS c FROM users")["c"],
        "models": query_one("SELECT COUNT(*) AS c FROM models")["c"],
        "chats": query_one("SELECT COUNT(*) AS c FROM conversations")["c"],
        "messages": query_one("SELECT COUNT(*) AS c FROM messages")["c"],
        "revenue_cents": query_one(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM orders WHERE status = 'completed'"
        )["s"],
        "paid_orders": query_one(
            "SELECT COUNT(*) AS c FROM orders WHERE status = 'completed'"
        )["c"],
    }
    counts["tokens_used"] = query_one(
        "SELECT COALESCE(SUM(LENGTH(content) / 4), 0) AS s FROM messages WHERE role = 'assistant'"
    )["s"]
    recent_users = query(
        "SELECT id, email, name, plan, role, tokens_left, created_at FROM users ORDER BY created_at DESC LIMIT 6"
    )
    plan_breakdown = query(
        "SELECT plan, COUNT(*) AS total FROM users GROUP BY plan ORDER BY total DESC"
    )
    return {
        "counts": counts,
        "recent_users": recent_users,
        "plan_breakdown": plan_breakdown,
    }


@router.get("/models")
def list_models(admin: dict = Depends(require_admin)):
    rows = query("SELECT * FROM models ORDER BY id DESC")
    for row in rows:
        row["api_key_masked"] = (
            (row["api_key"][:6] + "•" * 8 + row["api_key"][-4:]) if row["api_key"] else ""
        )
        row.pop("api_key", None)
    return {"models": rows}


@router.post("/models")
async def create_model(
    name: str = Form(...),
    model_id: str = Form(...),
    provider: str = Form("openai"),
    base_url: str = Form(""),
    api_key: str = Form(...),
    description: str = Form(""),
    enabled: bool = Form(True),
    logo: UploadFile | None = File(None),
    admin: dict = Depends(require_admin),
):
    if not name.strip() or not model_id.strip() or not api_key.strip():
        raise HTTPException(400, "Model name, model ID and API key are required.")
    transport = normalize_provider(provider)
    logo_path = await _save_logo(logo)
    model_pk = execute(
        "INSERT INTO models (name, model_id, provider, base_url, api_key, logo, description, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name.strip()[:60],
            model_id.strip(),
            transport,
            base_url.strip(),
            api_key.strip(),
            logo_path,
            description.strip(),
            1 if enabled else 0,
            time.time(),
            time.time(),
        ),
    )
    return {"ok": True, "id": model_pk}


@router.put("/models/{model_pk}")
async def update_model(
    model_pk: int,
    name: str = Form(...),
    model_id: str = Form(...),
    provider: str = Form("openai"),
    base_url: str = Form(""),
    api_key: str = Form(""),
    description: str = Form(""),
    enabled: bool = Form(True),
    logo: UploadFile | None = File(None),
    admin: dict = Depends(require_admin),
):
    existing = query_one("SELECT * FROM models WHERE id = ?", (model_pk,))
    if not existing:
        raise HTTPException(404, "Model not found.")

    logo_path = existing["logo"]
    new_logo = await _save_logo(logo)
    if new_logo:
        await _delete_logo(existing["logo"])
        logo_path = new_logo

    execute(
        "UPDATE models SET name = ?, model_id = ?, provider = ?, base_url = ?, api_key = ?, logo = ?, "
        "description = ?, enabled = ?, updated_at = ? WHERE id = ?",
        (
            name.strip()[:60],
            model_id.strip(),
            normalize_provider(provider),
            base_url.strip(),
            api_key.strip() or existing["api_key"],
            logo_path,
            description.strip(),
            1 if enabled else 0,
            time.time(),
            model_pk,
        ),
    )
    return {"ok": True}


@router.delete("/models/{model_pk}")
async def delete_model(model_pk: int, admin: dict = Depends(require_admin)):
    existing = query_one("SELECT * FROM models WHERE id = ?", (model_pk,))
    if not existing:
        raise HTTPException(404, "Model not found.")
    await _delete_logo(existing["logo"])
    execute("DELETE FROM models WHERE id = ?", (model_pk,))
    return {"ok": True}


@router.post("/models/discover")
async def discover_models(payload: DiscoverIn, admin: dict = Depends(require_admin)):
    key = payload.api_key.strip()
    if not key and payload.model_pk:
        stored = query_one("SELECT api_key FROM models WHERE id = ?", (payload.model_pk,))
        key = (stored or {}).get("api_key", "")
    model = {
        "provider": payload.provider,
        "base_url": payload.base_url,
        "model_id": "",
        "api_key": key,
    }
    ok, ids, message = await list_provider_models(model)
    return {"ok": ok, "models": ids, "message": message}


@router.post("/models/{model_pk}/test")
async def test_model(model_pk: int, admin: dict = Depends(require_admin)):
    model = query_one("SELECT * FROM models WHERE id = ?", (model_pk,))
    if not model:
        raise HTTPException(404, "Model not found.")
    ok, message = await check_connection(model)
    return {"ok": ok, "message": message}


@router.get("/payments")
def get_payments(admin: dict = Depends(require_admin)):
    settings = get_settings()
    secret = settings.get("paypal_client_secret", "")
    return {
        "paypal_enabled": settings.get("paypal_enabled", "1") == "1",
        "paypal_mode": settings.get("paypal_mode", "demo"),
        "paypal_client_id": settings.get("paypal_client_id", ""),
        "paypal_client_secret_set": bool(secret),
        "paypal_client_secret_masked": ("•" * 12 + secret[-4:]) if secret else "",
        "paypal_currency": settings.get("paypal_currency", "USD"),
    }


@router.put("/payments")
def save_payments(payload: PaymentIn, admin: dict = Depends(require_admin)):
    if payload.paypal_mode not in {"demo", "sandbox", "live"}:
        raise HTTPException(400, "Mode must be demo, sandbox or live.")
    set_setting("paypal_enabled", "1" if payload.paypal_enabled else "0")
    set_setting("paypal_mode", payload.paypal_mode)
    set_setting("paypal_client_id", payload.paypal_client_id.strip())
    if payload.paypal_client_secret.strip():
        set_setting("paypal_client_secret", payload.paypal_client_secret.strip())
    set_setting("paypal_currency", (payload.paypal_currency or "USD").strip().upper()[:3])
    return {"ok": True}


WEB_PROVIDERS = {"auto", "google", "brave", "tavily", "serper", "exa", "bing", "duckduckgo"}
WEB_SECRET_FIELDS = (
    "web_google_key",
    "web_brave_key",
    "web_tavily_key",
    "web_serper_key",
    "web_exa_key",
    "web_github_token",
    "web_hf_token",
)


@router.get("/web")
def get_web(admin: dict = Depends(require_admin)):
    settings = get_settings()
    data = {
        "web_enabled": settings.get("web_enabled", "1") == "1",
        "web_provider": settings.get("web_provider", "auto"),
        "web_pages": int(settings.get("web_pages", "2") or 2),
        "web_google_cx": settings.get("web_google_cx", ""),
    }
    for field in WEB_SECRET_FIELDS:
        secret = settings.get(field, "")
        data[field + "_set"] = bool(secret)
        data[field + "_masked"] = ("•" * 10 + secret[-4:]) if secret else ""
    return data


@router.put("/web")
def save_web(payload: WebIn, admin: dict = Depends(require_admin)):
    if payload.web_provider not in WEB_PROVIDERS:
        raise HTTPException(400, "Unknown search engine.")
    set_setting("web_enabled", "1" if payload.web_enabled else "0")
    set_setting("web_provider", payload.web_provider)
    set_setting("web_pages", str(max(0, min(int(payload.web_pages), 3))))
    set_setting("web_google_cx", payload.web_google_cx.strip())
    for field in WEB_SECRET_FIELDS:
        value = str(getattr(payload, field) or "").strip()
        if value:
            set_setting(field, value)
    return {"ok": True}


@router.post("/web/test")
async def test_web_search(admin: dict = Depends(require_admin)):
    return await test_search("latest Python release")


@router.get("/plans")
def list_plans(admin: dict = Depends(require_admin)):
    rows = query("SELECT * FROM plans ORDER BY sort_order ASC")
    for row in rows:
        try:
            row["features"] = json.loads(row["features"])
        except json.JSONDecodeError:
            row["features"] = []
    return {"plans": rows}


@router.put("/plans/{slug}")
def update_plan(slug: str, payload: PlanIn, admin: dict = Depends(require_admin)):
    existing = query_one("SELECT * FROM plans WHERE slug = ?", (slug,))
    if not existing:
        raise HTTPException(404, "Plan not found.")
    execute(
        "UPDATE plans SET name = ?, price_cents = ?, tokens = ?, credits = ?, tagline = ?, "
        "features = ?, badge = ?, visible = ? WHERE slug = ?",
        (
            payload.name.strip() or existing["name"],
            max(0, payload.price_cents),
            max(0, payload.tokens),
            max(0, payload.credits),
            payload.tagline,
            json.dumps(payload.features),
            payload.badge or None,
            1 if payload.visible else 0,
            slug,
        ),
    )
    return {"ok": True}


@router.get("/users")
def list_users(admin: dict = Depends(require_admin)):
    rows = query(
        "SELECT id, email, name, role, plan, tokens_left, credits_left, created_at "
        "FROM users ORDER BY created_at DESC LIMIT 200"
    )
    return {"users": rows}


@router.get("/orders")
def list_orders(admin: dict = Depends(require_admin)):
    rows = query(
        "SELECT o.*, u.email FROM orders o JOIN users u ON u.id = o.user_id "
        "ORDER BY o.created_at DESC LIMIT 100"
    )
    return {"orders": rows}
