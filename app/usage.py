"""Per-model, 24-hour usage buckets.

A plan's token and credit allowance applies to every model separately. Each
(user, model) bucket resets when its 24-hour period ends. The users table keeps
the aggregate totals so older screens and account summaries remain valid.

The buckets themselves live in the shared usage database, which the website
charges too. A user who runs out of credits on the website is therefore out of
credits in the app as well. If the shared file cannot be reached we fall back to
the app's local table so chatting keeps working.
"""

from __future__ import annotations

import time

from . import shared_usage
from .database import execute, query, query_one

DAY_SECONDS = 24 * 60 * 60
DEFAULT_TOKENS = 10_000
DEFAULT_CREDITS = 10_000


def _plan_for(user: dict) -> dict:
    plan = query_one(
        "SELECT slug, name, tokens, credits FROM plans WHERE slug = ?",
        (user.get("plan") or "free",),
    )
    if plan:
        return plan
    return {
        "slug": "free",
        "name": "FREE",
        "tokens": DEFAULT_TOKENS,
        "credits": DEFAULT_CREDITS,
    }


def _all_model_ids() -> list[int]:
    return [int(row["id"]) for row in query("SELECT id FROM models")]


def unlimited_usage() -> dict:
    return {
        "unlimited": True,
        "tokens_left": None,
        "credits_left": None,
        "tokens_total": None,
        "credits_total": None,
        "period_started": None,
        "reset_at": None,
    }


def usage_public(row: dict | None) -> dict:
    if not row:
        return unlimited_usage()
    if row.get("unlimited"):
        return unlimited_usage()
    started = float(row.get("period_started") or 0)
    return {
        "unlimited": False,
        "tokens_left": int(row.get("tokens_left") or 0),
        "credits_left": int(row.get("credits_left") or 0),
        "tokens_total": int(row.get("tokens_total") or 0),
        "credits_total": int(row.get("credits_total") or 0),
        "period_started": started,
        "reset_at": started + DAY_SECONDS if started else None,
    }


def _local_ensure(user: dict, model_pk: int, plan: dict) -> dict:
    tokens_total = max(0, int(plan.get("tokens") or 0))
    credits_total = max(0, int(plan.get("credits") or 0))
    now = time.time()
    row = query_one(
        "SELECT * FROM model_usage WHERE user_id = ? AND model_pk = ?",
        (user["id"], int(model_pk)),
    )
    stale = (
        not row
        or now - float(row.get("period_started") or 0) >= DAY_SECONDS
        or int(row.get("tokens_total") or 0) != tokens_total
        or int(row.get("credits_total") or 0) != credits_total
    )
    if stale:
        execute(
            "INSERT INTO model_usage (user_id, model_pk, tokens_left, credits_left, "
            "tokens_total, credits_total, period_started, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, model_pk) DO UPDATE SET "
            "tokens_left = excluded.tokens_left, credits_left = excluded.credits_left, "
            "tokens_total = excluded.tokens_total, credits_total = excluded.credits_total, "
            "period_started = excluded.period_started, updated_at = excluded.updated_at",
            (
                user["id"],
                int(model_pk),
                tokens_total,
                credits_total,
                tokens_total,
                credits_total,
                now,
                now,
            ),
        )
        row = query_one(
            "SELECT * FROM model_usage WHERE user_id = ? AND model_pk = ?",
            (user["id"], int(model_pk)),
        )
    return row or {"unlimited": False, "tokens_left": 0, "credits_left": 0}


def _mirror_local(user_id: int, bucket: dict) -> None:
    """Keep the app's own table in step for older screens and admin views."""
    execute(
        "INSERT INTO model_usage (user_id, model_pk, tokens_left, credits_left, "
        "tokens_total, credits_total, period_started, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, model_pk) DO UPDATE SET "
        "tokens_left = excluded.tokens_left, credits_left = excluded.credits_left, "
        "tokens_total = excluded.tokens_total, credits_total = excluded.credits_total, "
        "period_started = excluded.period_started, updated_at = excluded.updated_at",
        (
            int(user_id),
            int(bucket["model_pk"]),
            int(bucket.get("tokens_left") or 0),
            int(bucket.get("credits_left") or 0),
            int(bucket.get("tokens_total") or 0),
            int(bucket.get("credits_total") or 0),
            float(bucket.get("period_started") or time.time()),
            time.time(),
        ),
    )


def ensure_model_usage(user: dict, model_pk: int) -> dict:
    if user.get("role") == "admin":
        return {"unlimited": True}
    plan = _plan_for(user)
    local = query_one(
        "SELECT tokens_left, credits_left, tokens_total, credits_total, period_started "
        "FROM model_usage WHERE user_id = ? AND model_pk = ?",
        (user["id"], int(model_pk)),
    )
    try:
        bucket = shared_usage.ensure(
            int(user["id"]),
            int(model_pk),
            str(plan.get("slug") or user.get("plan") or "free"),
            int(plan.get("tokens") or 0),
            int(plan.get("credits") or 0),
            _all_model_ids(),
            seed=dict(local) if local else None,
        )
        shared_plan = str(bucket.get("plan") or "")
        if shared_plan and shared_plan != (user.get("plan") or ""):
            # The website upgraded (or reset) this account; adopt its plan here.
            execute(
                "UPDATE users SET plan = ? WHERE id = ?", (shared_plan, user["id"])
            )
            user["plan"] = shared_plan
        _mirror_local(user["id"], bucket)
        return bucket
    except Exception:
        return _local_ensure(user, model_pk, plan)


def sync_user_totals(user_id: int) -> None:
    user = query_one("SELECT role FROM users WHERE id = ?", (user_id,))
    if not user or user.get("role") == "admin":
        return
    totals = None
    try:
        totals = shared_usage.snapshot(int(user_id))
    except Exception:
        totals = None
    if totals and int(totals.get("tokens_total") or 0) >= 0:
        execute(
            "UPDATE users SET tokens_left = ?, credits_left = ? WHERE id = ?",
            (
                max(0, int(totals.get("tokens_left") or 0)),
                max(0, int(totals.get("credits_left") or 0)),
                user_id,
            ),
        )
        return
    totals = query_one(
        "SELECT COUNT(*) AS buckets, COALESCE(SUM(tokens_left), 0) AS tokens_left, "
        "COALESCE(SUM(credits_left), 0) AS credits_left "
        "FROM model_usage WHERE user_id = ?",
        (user_id,),
    )
    if not totals or not int(totals.get("buckets") or 0):
        return
    execute(
        "UPDATE users SET tokens_left = ?, credits_left = ? WHERE id = ?",
        (
            max(0, int(totals["tokens_left"])),
            max(0, int(totals["credits_left"])),
            user_id,
        ),
    )


def charge_model_usage(user: dict, model_pk: int, amount: int) -> dict:
    if user.get("role") == "admin":
        return {"unlimited": True}
    row = ensure_model_usage(user, model_pk)
    if row.get("unlimited"):
        return row
    charge = max(0, int(amount or 0))
    try:
        result = shared_usage.charge(int(user["id"]), int(model_pk), charge)
        if result:
            bucket = dict(result["bucket"])
            _mirror_local(user["id"], bucket)
            account = result.get("account") or {}
            execute(
                "UPDATE users SET tokens_left = ?, credits_left = ? WHERE id = ?",
                (
                    max(0, int(account.get("tokens_left") or 0)),
                    max(0, int(account.get("credits_left") or 0)),
                    user["id"],
                ),
            )
            return bucket
    except Exception:
        pass
    execute(
        "UPDATE model_usage SET tokens_left = MAX(tokens_left - ?, 0), "
        "credits_left = MAX(credits_left - ?, 0), updated_at = ? "
        "WHERE user_id = ? AND model_pk = ?",
        (charge, charge, time.time(), user["id"], int(model_pk)),
    )
    sync_user_totals(user["id"])
    fresh = query_one(
        "SELECT * FROM model_usage WHERE user_id = ? AND model_pk = ?",
        (user["id"], int(model_pk)),
    )
    return fresh or row


def reset_user_usage(user_id: int, plan_slug: str) -> None:
    plan = query_one(
        "SELECT tokens, credits FROM plans WHERE slug = ?", (plan_slug,)
    ) or {"tokens": DEFAULT_TOKENS, "credits": DEFAULT_CREDITS}
    tokens_total = max(0, int(plan.get("tokens") or 0))
    credits_total = max(0, int(plan.get("credits") or 0))
    model_ids = _all_model_ids()
    try:
        shared_usage.reset(
            int(user_id), plan_slug, tokens_total, credits_total, model_ids
        )
    except Exception:
        pass

    now = time.time()
    for model in query("SELECT id FROM models"):
        execute(
            "INSERT INTO model_usage (user_id, model_pk, tokens_left, credits_left, "
            "tokens_total, credits_total, period_started, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, model_pk) DO UPDATE SET "
            "tokens_left = excluded.tokens_left, credits_left = excluded.credits_left, "
            "tokens_total = excluded.tokens_total, credits_total = excluded.credits_total, "
            "period_started = excluded.period_started, updated_at = excluded.updated_at",
            (
                user_id,
                int(model["id"]),
                tokens_total,
                credits_total,
                tokens_total,
                credits_total,
                now,
                now,
            ),
        )
    execute(
        "UPDATE users SET tokens_left = ?, credits_left = ?, plan = ? WHERE id = ?",
        (
            tokens_total * max(1, len(model_ids)),
            credits_total * max(1, len(model_ids)),
            plan_slug,
            user_id,
        ),
    )
