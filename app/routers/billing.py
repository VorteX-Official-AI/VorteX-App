import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..config import PAYPAL_API
from ..database import execute, get_setting, query, query_one, user_public
from ..paypal import PayPalError, capture_order, create_order
from ..security import require_user
from ..usage import reset_user_usage

router = APIRouter(prefix="/api/billing", tags=["billing"])


class CheckoutIn(BaseModel):
    plan_slug: str


class CaptureIn(BaseModel):
    order_id: str
    plan_slug: str


def _plan(slug: str) -> dict:
    plan = query_one("SELECT * FROM plans WHERE slug = ? AND visible = 1", (slug,))
    if not plan:
        raise HTTPException(404, "Plan not found.")
    return plan


@router.get("/plans")
def plans():
    rows = query("SELECT * FROM plans WHERE visible = 1 ORDER BY sort_order ASC")
    for row in rows:
        try:
            row["features"] = json.loads(row["features"])
        except json.JSONDecodeError:
            row["features"] = []
    return {"plans": rows}


@router.get("/config")
def config():
    mode = get_setting("paypal_mode", "demo")
    enabled = get_setting("paypal_enabled", "1") == "1"
    client_id = get_setting("paypal_client_id", "")
    secret = get_setting("paypal_client_secret", "")
    paypal_ready = bool(enabled and client_id and secret and mode in PAYPAL_API)
    return {
        "paypal_enabled": enabled,
        "mode": "paypal" if paypal_ready else ("demo" if enabled else "disabled"),
        "paypal_mode": mode,
        "paypal_client_id": client_id if paypal_ready else "",
        "currency": get_setting("paypal_currency", "USD"),
    }


@router.post("/checkout")
async def checkout(payload: CheckoutIn, request: Request, user: dict = Depends(require_user)):
    plan = _plan(payload.plan_slug)
    mode = get_setting("paypal_mode", "demo")
    enabled = get_setting("paypal_enabled", "1") == "1"
    client_id = get_setting("paypal_client_id", "")
    secret = get_setting("paypal_client_secret", "")

    if not enabled:
        raise HTTPException(400, "Payments are currently disabled by the administrator.")

    paypal_ready = bool(client_id and secret and mode in PAYPAL_API)

    if not paypal_ready:
        # Demo checkout: nothing to charge, activate straight away.
        _activate(user, plan, provider="demo", external_id=None)
        return {"type": "demo", "plan": plan["slug"], "user": user_public(
            query_one("SELECT * FROM users WHERE id = ?", (user["id"],))
        )}

    order_row_id = execute(
        "INSERT INTO orders (user_id, plan_slug, amount_cents, provider, status, created_at) "
        "VALUES (?, ?, ?, 'paypal', 'pending', ?)",
        (user["id"], plan["slug"], plan["price_cents"], time.time()),
    )
    try:
        result = await create_order(
            client_id=client_id,
            secret=secret,
            mode=mode,
            price_cents=plan["price_cents"],
            currency=get_setting("paypal_currency", "USD"),
            plan_name=plan["name"],
            return_url=str(request.base_url) + "pricing?paypal=return",
            cancel_url=str(request.base_url) + "pricing?paypal=cancel",
        )
    except PayPalError as exc:
        execute("UPDATE orders SET status = 'failed' WHERE id = ?", (order_row_id,))
        raise HTTPException(502, str(exc)) from exc

    execute(
        "UPDATE orders SET external_id = ? WHERE id = ?",
        (result["id"], order_row_id),
    )
    return {"type": "paypal", "order_id": result["id"], "amount_cents": plan["price_cents"]}


@router.post("/capture")
async def capture(payload: CaptureIn, user: dict = Depends(require_user)):
    plan = _plan(payload.plan_slug)
    mode = get_setting("paypal_mode", "demo")
    client_id = get_setting("paypal_client_id", "")
    secret = get_setting("paypal_client_secret", "")
    if not (client_id and secret and mode in PAYPAL_API):
        raise HTTPException(400, "PayPal is not configured.")

    try:
        result = await capture_order(
            client_id=client_id, secret=secret, mode=mode, order_id=payload.order_id
        )
    except PayPalError as exc:
        raise HTTPException(502, str(exc)) from exc

    if result.get("status") != "COMPLETED":
        raise HTTPException(400, f"Payment was not completed (status: {result.get('status')}).")

    execute(
        "UPDATE orders SET status = 'completed' WHERE external_id = ? AND user_id = ?",
        (payload.order_id, user["id"]),
    )
    _activate(user, plan, provider="paypal", external_id=payload.order_id)
    return {
        "ok": True,
        "user": user_public(query_one("SELECT * FROM users WHERE id = ?", (user["id"],))),
    }


def _activate(user: dict, plan: dict, *, provider: str, external_id: str | None) -> None:
    execute(
        "UPDATE users SET plan = ?, tokens_left = ?, credits_left = ? WHERE id = ?",
        (plan["slug"], plan["tokens"], plan["credits"], user["id"]),
    )
    reset_user_usage(user["id"], plan["slug"])
    if provider == "demo":
        execute(
            "INSERT INTO orders (user_id, plan_slug, amount_cents, provider, status, created_at) "
            "VALUES (?, ?, ?, 'demo', 'completed', ?)",
            (user["id"], plan["slug"], plan["price_cents"], time.time()),
        )
