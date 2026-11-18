"""Minimal PayPal REST client (orders v2) used for plan checkout."""

import base64

import httpx

from .config import PAYPAL_API

TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class PayPalError(Exception):
    pass


def _base_url(mode: str) -> str:
    return PAYPAL_API.get(mode, PAYPAL_API["sandbox"])


async def get_access_token(client_id: str, secret: str, mode: str) -> str:
    credentials = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{_base_url(mode)}/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="grant_type=client_credentials",
        )
    if response.status_code >= 400:
        raise PayPalError(f"PayPal authentication failed ({response.status_code})")
    return response.json()["access_token"]


def _amount(price_cents: int) -> str:
    return f"{price_cents / 100:.2f}"


async def create_order(
    *,
    client_id: str,
    secret: str,
    mode: str,
    price_cents: int,
    currency: str,
    plan_name: str,
    return_url: str,
    cancel_url: str,
) -> dict:
    token = await get_access_token(client_id, secret, mode)
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": plan_name,
                "description": f"VorteX AI — {plan_name} plan",
                "amount": {
                    "currency_code": currency or "USD",
                    "value": _amount(price_cents),
                },
            }
        ],
        "application_context": {
            "brand_name": "VorteX AI",
            "user_action": "PAY_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{_base_url(mode)}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code >= 400:
        raise PayPalError(f"PayPal order creation failed ({response.status_code})")
    return response.json()


async def capture_order(*, client_id: str, secret: str, mode: str, order_id: str) -> dict:
    token = await get_access_token(client_id, secret, mode)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            f"{_base_url(mode)}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    if response.status_code >= 400:
        raise PayPalError(f"PayPal capture failed ({response.status_code})")
    return response.json()
