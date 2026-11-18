"""One usage ledger shared by the desktop app and the website.

Both programs run on the same machine and identify a person by their VorteX app
user id. Keeping the 24-hour per-model buckets in this one database means a user
who runs out of tokens or credits on the website is also out in the app (and the
other way around).

Rows use the app's model ids, which the website also uses through its model
bridge, so the same model bucket is shared by both surfaces.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing

from .config import SHARED_DB_PATH

DAY_SECONDS = 24 * 60 * 60
BUSY_TIMEOUT_MS = 30_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_accounts (
    user_id INTEGER PRIMARY KEY,
    plan TEXT NOT NULL,
    tokens_left INTEGER NOT NULL,
    credits_left INTEGER NOT NULL,
    tokens_total INTEGER NOT NULL,
    credits_total INTEGER NOT NULL,
    period_started REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shared_usage (
    user_id INTEGER NOT NULL,
    model_pk INTEGER NOT NULL,
    tokens_left INTEGER NOT NULL,
    credits_left INTEGER NOT NULL,
    tokens_total INTEGER NOT NULL,
    credits_total INTEGER NOT NULL,
    period_started REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, model_pk)
);

CREATE INDEX IF NOT EXISTS idx_shared_usage_user ON shared_usage(user_id);
"""


def _connect() -> sqlite3.Connection:
    SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(SHARED_DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.executescript(SCHEMA)
    return connection


def _account_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def _totals(connection: sqlite3.Connection, user_id: int) -> dict:
    row = connection.execute(
        "SELECT COUNT(*) AS buckets, COALESCE(SUM(tokens_left), 0) AS tokens_left, "
        "COALESCE(SUM(credits_left), 0) AS credits_left, "
        "COALESCE(SUM(tokens_total), 0) AS tokens_total, "
        "COALESCE(SUM(credits_total), 0) AS credits_total "
        "FROM shared_usage WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    return {
        "buckets": int(row["buckets"] or 0),
        "tokens_left": max(0, int(row["tokens_left"] or 0)),
        "credits_left": max(0, int(row["credits_left"] or 0)),
        "tokens_total": max(0, int(row["tokens_total"] or 0)),
        "credits_total": max(0, int(row["credits_total"] or 0)),
    }


def _clean_ids(model_ids) -> list[int]:
    ids: set[int] = set()
    for value in model_ids or []:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(ids)


def ensure(
    user_id: int,
    model_pk: int,
    plan: str,
    tokens_total: int,
    credits_total: int,
    model_ids=None,
    seed=None,
) -> dict:
    """Return the shared 24-hour bucket for one model.

    A larger plan allowance always wins, so an app that still has the old free
    plan cannot downgrade an account upgraded on the website. When the shared
    plan is ahead, the returned ``plan`` tells the caller to adopt it locally.
    """
    uid = int(user_id)
    model = int(model_pk)
    requested_plan = str(plan or "free")
    requested_tokens = max(0, int(tokens_total or 0))
    requested_credits = max(0, int(credits_total or 0))
    ids = _clean_ids(model_ids)
    now = time.time()
    seed = seed or {}
    seed_left: tuple[int, int] | None = None

    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT * FROM shared_accounts WHERE user_id = ?", (uid,)
            ).fetchone()

            reset = False
            if account is None:
                chosen_plan = requested_plan
                effective_tokens = max(
                    requested_tokens, int(seed.get("tokens_total") or 0)
                )
                effective_credits = max(
                    requested_credits, int(seed.get("credits_total") or 0)
                )
                if seed and now - float(seed.get("period_started") or 0) < DAY_SECONDS:
                    try:
                        seed_left = (
                            max(0, min(int(seed.get("tokens_left") or 0), effective_tokens)),
                            max(0, min(int(seed.get("credits_left") or 0), effective_credits)),
                        )
                    except (TypeError, ValueError):
                        seed_left = None
                period = now
                connection.execute(
                    "INSERT INTO shared_accounts (user_id, plan, tokens_left, credits_left, "
                    "tokens_total, credits_total, period_started, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uid,
                        chosen_plan,
                        effective_tokens,
                        effective_credits,
                        effective_tokens,
                        effective_credits,
                        now,
                        now,
                    ),
                )
            else:
                chosen_plan = str(account["plan"] or requested_plan)
                effective_tokens = max(0, int(account["tokens_total"] or 0))
                effective_credits = max(0, int(account["credits_total"] or 0))
                if requested_tokens > effective_tokens or requested_credits > effective_credits:
                    # A plan upgrade landed on either surface: refill every model.
                    chosen_plan = requested_plan
                    effective_tokens = max(effective_tokens, requested_tokens)
                    effective_credits = max(effective_credits, requested_credits)
                    reset = True
                period = float(account["period_started"] or now)
                if now - period >= DAY_SECONDS:
                    reset = True
                if reset:
                    connection.execute("DELETE FROM shared_usage WHERE user_id = ?", (uid,))
                    period = now

            if ids:
                placeholders = ", ".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM shared_usage WHERE user_id = ? "
                    f"AND model_pk NOT IN ({placeholders})",
                    (uid, *ids),
                )

            bucket = connection.execute(
                "SELECT * FROM shared_usage WHERE user_id = ? AND model_pk = ?",
                (uid, model),
            ).fetchone()
            stale_bucket = (
                bucket is None
                or int(bucket["tokens_total"] or 0) != effective_tokens
                or int(bucket["credits_total"] or 0) != effective_credits
                or now - float(bucket["period_started"] or 0) >= DAY_SECONDS
            )
            if stale_bucket:
                if bucket is None and seed_left is not None:
                    bucket_left_tokens, bucket_left_credits = seed_left
                else:
                    bucket_left_tokens, bucket_left_credits = effective_tokens, effective_credits
                connection.execute(
                    "INSERT INTO shared_usage (user_id, model_pk, tokens_left, credits_left, "
                    "tokens_total, credits_total, period_started, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, model_pk) DO UPDATE SET "
                    "tokens_left = excluded.tokens_left, "
                    "credits_left = excluded.credits_left, "
                    "tokens_total = excluded.tokens_total, "
                    "credits_total = excluded.credits_total, "
                    "period_started = excluded.period_started, "
                    "updated_at = excluded.updated_at",
                    (
                        uid,
                        model,
                        bucket_left_tokens,
                        bucket_left_credits,
                        effective_tokens,
                        effective_credits,
                        now,
                        now,
                    ),
                )
                bucket = connection.execute(
                    "SELECT * FROM shared_usage WHERE user_id = ? AND model_pk = ?",
                    (uid, model),
                ).fetchone()

            totals = _totals(connection, uid)
            connection.execute(
                "UPDATE shared_accounts SET plan = ?, tokens_left = ?, credits_left = ?, "
                "tokens_total = ?, credits_total = ?, updated_at = ? WHERE user_id = ?",
                (
                    chosen_plan,
                    totals["tokens_left"],
                    totals["credits_left"],
                    totals["tokens_total"],
                    totals["credits_total"],
                    now,
                    uid,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    result = dict(bucket) if bucket else {
        "user_id": uid,
        "model_pk": model,
        "tokens_left": effective_tokens,
        "credits_left": effective_credits,
        "tokens_total": effective_tokens,
        "credits_total": effective_credits,
        "period_started": period,
    }
    result["plan"] = chosen_plan
    result["account"] = {
        "plan": chosen_plan,
        "tokens_left": totals["tokens_left"],
        "credits_left": totals["credits_left"],
        "tokens_total": totals["tokens_total"],
        "credits_total": totals["credits_total"],
        "period_started": period,
    }
    return result


def charge(user_id: int, model_pk: int, amount: int) -> dict | None:
    """Deduct the same amount of tokens and credits from one shared bucket."""
    uid = int(user_id)
    model = int(model_pk)
    charge_amount = max(0, int(amount or 0))
    now = time.time()

    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            bucket = connection.execute(
                "SELECT * FROM shared_usage WHERE user_id = ? AND model_pk = ?",
                (uid, model),
            ).fetchone()
            if bucket is None:
                connection.rollback()
                return None
            available = min(
                int(bucket["tokens_left"] or 0), int(bucket["credits_left"] or 0)
            )
            charged = min(charge_amount, max(0, available))
            if charged > 0:
                connection.execute(
                    "UPDATE shared_usage SET tokens_left = MAX(tokens_left - ?, 0), "
                    "credits_left = MAX(credits_left - ?, 0), updated_at = ? "
                    "WHERE user_id = ? AND model_pk = ?",
                    (charged, charged, now, uid, model),
                )

            totals = _totals(connection, uid)
            connection.execute(
                "UPDATE shared_accounts SET tokens_left = ?, credits_left = ?, "
                "tokens_total = ?, credits_total = ?, updated_at = ? WHERE user_id = ?",
                (
                    totals["tokens_left"],
                    totals["credits_left"],
                    totals["tokens_total"],
                    totals["credits_total"],
                    now,
                    uid,
                ),
            )
            fresh = connection.execute(
                "SELECT * FROM shared_usage WHERE user_id = ? AND model_pk = ?",
                (uid, model),
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "charged": charged,
        "bucket": dict(fresh) if fresh else dict(bucket),
        "account": {
            "tokens_left": totals["tokens_left"],
            "credits_left": totals["credits_left"],
            "tokens_total": totals["tokens_total"],
            "credits_total": totals["credits_total"],
        },
    }


def reset(
    user_id: int,
    plan: str,
    tokens_total: int,
    credits_total: int,
    model_ids=None,
) -> dict:
    """Refill every model bucket after a plan purchase or an explicit reset."""
    uid = int(user_id)
    chosen_plan = str(plan or "free")
    tokens = max(0, int(tokens_total or 0))
    credits = max(0, int(credits_total or 0))
    ids = _clean_ids(model_ids)
    now = time.time()

    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM shared_usage WHERE user_id = ?", (uid,))
            for model in ids:
                connection.execute(
                    "INSERT INTO shared_usage (user_id, model_pk, tokens_left, credits_left, "
                    "tokens_total, credits_total, period_started, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid, model, tokens, credits, tokens, credits, now, now),
                )
            aggregate_tokens = tokens * len(ids) if ids else tokens
            aggregate_credits = credits * len(ids) if ids else credits
            connection.execute(
                "INSERT INTO shared_accounts (user_id, plan, tokens_left, credits_left, "
                "tokens_total, credits_total, period_started, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET plan = excluded.plan, "
                "tokens_left = excluded.tokens_left, credits_left = excluded.credits_left, "
                "tokens_total = excluded.tokens_total, credits_total = excluded.credits_total, "
                "period_started = excluded.period_started, updated_at = excluded.updated_at",
                (
                    uid,
                    chosen_plan,
                    aggregate_tokens,
                    aggregate_credits,
                    aggregate_tokens,
                    aggregate_credits,
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "plan": chosen_plan,
        "tokens_left": aggregate_tokens,
        "credits_left": aggregate_credits,
        "tokens_total": aggregate_tokens,
        "credits_total": aggregate_credits,
        "period_started": now,
    }


def snapshot(user_id: int) -> dict | None:
    """Return the account-wide totals without changing any bucket."""
    uid = int(user_id)
    with closing(_connect()) as connection:
        totals = _totals(connection, uid)
        account = connection.execute(
            "SELECT * FROM shared_accounts WHERE user_id = ?", (uid,)
        ).fetchone()
    if not totals["buckets"] and not account:
        return None
    if totals["buckets"]:
        return {
            "plan": str(account["plan"] or "free") if account else "free",
            "tokens_left": totals["tokens_left"],
            "credits_left": totals["credits_left"],
            "tokens_total": totals["tokens_total"],
            "credits_total": totals["credits_total"],
            "period_started": float(account["period_started"] or 0) if account else None,
        }
    return _account_dict(account)


def plan_for(user_id: int) -> str | None:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT plan FROM shared_accounts WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return str(row["plan"]) if row and row["plan"] else None
