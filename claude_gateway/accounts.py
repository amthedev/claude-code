from __future__ import annotations

import math
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException

from .config import Settings
from .customers import CustomerPlan
from .security import hash_password, verify_password


MODEL_LABELS = {
    "haiku": "Claude Code Economy",
    "sonnet": "Claude Code Pro",
    "opus": "Claude Code Ultra",
}

MODEL_TOKEN_PRICES = {
    "haiku": 0.000000224,
    "sonnet": 0.00000087,
    "opus": 0.00000087,
}

BACKEND_MODELS = {
    "haiku": "claude-code-economy",
    "sonnet": "claude-code-pro",
    "opus": "claude-code-ultra",
}


class AccountStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.account_data_file)
        self._lock = Lock()
        self._init_db()

    def list_gift_cards(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM gift_cards ORDER BY created_at DESC").fetchall()
        return [_public_gift_card(_gift_card_from_row(row)) for row in rows]

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
        return [_public_account(_account_from_row(row)) for row in rows]

    def create_gift_card(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            code = _normalize_gift_code(values.get("code")) or _generate_gift_code()
            while self._gift_code_exists(db, code):
                if values.get("code"):
                    raise HTTPException(status_code=409, detail="Gift card already exists.")
                code = _generate_gift_code()

            card = _gift_card_from_values(values, code, self.settings)
            db.execute(
                """
                INSERT INTO gift_cards (
                    id, code, plan, price, model_key, manual_limit, active, daily_limit,
                    computed_daily_tokens, max_cost_usd, used_by_account_id, used_by_login,
                    used_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _gift_card_values(card),
            )
            db.commit()
        return _public_gift_card(card)

    def update_gift_card(self, card_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            card = self._find_gift_card(db, card_id)
            if card.get("usedByAccountId"):
                raise HTTPException(status_code=409, detail="Used gift cards cannot be changed.")
            if "active" in values:
                card["active"] = bool(values["active"])
                db.execute(
                    "UPDATE gift_cards SET active = ? WHERE id = ?",
                    (int(card["active"]), card_id),
                )
                db.commit()
        return _public_gift_card(card)

    def delete_gift_card(self, card_id: str) -> dict[str, str]:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM gift_cards WHERE id = ?", (card_id,))
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Gift card not found.")
        return {"status": "deleted"}

    def signup(self, values: dict[str, Any]) -> dict[str, Any]:
        login = str(values.get("login") or "").strip().lower()
        name = str(values.get("name") or "").strip()
        password = str(values.get("password") or "")
        gift_code = _normalize_gift_code(values.get("giftCard") or values.get("gift_card"))

        if not name or not login or not password or not gift_code:
            raise HTTPException(status_code=400, detail="Name, e-mail, password, and gift card are required.")
        if "@" not in login or len(login) > 254:
            raise HTTPException(status_code=400, detail="Use a valid e-mail.")

        with self._lock, self._connect() as db:
            if self._login_exists(db, login):
                raise HTTPException(status_code=409, detail="This e-mail is already registered.")

            gift_card = self._gift_card_by_code(db, gift_code)
            if not gift_card or not gift_card.get("active") or gift_card.get("usedByAccountId"):
                raise HTTPException(status_code=400, detail="Gift card is invalid, paused, or already used.")

            account = _account_from_gift_card(gift_card, name, login, password)
            db.execute(
                """
                INSERT INTO accounts (
                    id, api_token, name, display_name, login, password_hash, plan, price,
                    model_key, manual_limit, active, gift_card_code, used_today, daily_limit,
                    computed_daily_tokens, max_cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _account_values(account),
            )
            db.execute(
                """
                UPDATE gift_cards
                   SET active = 0, used_by_account_id = ?, used_by_login = ?, used_at = ?
                 WHERE id = ?
                """,
                (account["id"], login, _now(), gift_card["id"]),
            )
            db.commit()
        return _public_account(account)

    def login(self, values: dict[str, Any]) -> dict[str, Any]:
        login = str(values.get("login") or "").strip().lower()
        password = str(values.get("password") or "")
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE login = ?", (login,)).fetchone()
            account = _account_from_row(row) if row else None
            if not account:
                raise HTTPException(status_code=403, detail="Invalid e-mail or password.")
            ok, needs_rehash = verify_password(password, account["passwordHash"])
            if not ok:
                raise HTTPException(status_code=403, detail="Invalid e-mail or password.")
            if not account.get("active"):
                raise HTTPException(status_code=403, detail="Account is paused.")
            if needs_rehash:
                account["passwordHash"] = hash_password(password)
                db.execute(
                    "UPDATE accounts SET password_hash = ? WHERE id = ?",
                    (account["passwordHash"], account["id"]),
                )
                db.commit()
            return _public_account(account)

    def update_account(self, account_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            account = self._find_account(db, account_id)
            if "active" in values:
                account["active"] = bool(values["active"])
            if values.get("resetUsage"):
                account["usedToday"] = 0
            db.execute(
                "UPDATE accounts SET active = ?, used_today = ? WHERE id = ?",
                (int(account["active"]), int(account["usedToday"]), account_id),
            )
            db.commit()
        return _public_account(account)

    def delete_account(self, account_id: str) -> dict[str, str]:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Account not found.")
        return {"status": "deleted"}

    def customer_plan_for_token(self, token: str) -> CustomerPlan | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
        if not row:
            return None
        account = _account_from_row(row)
        return CustomerPlan(
            token=token,
            name=account["name"],
            monthly_price_brl=float(account.get("price") or 0),
            daily_token_limit=int(account.get("dailyLimit") or 0),
            allowed_model=BACKEND_MODELS.get(account.get("modelKey"), "claude-code-pro"),
            active=bool(account.get("active")),
        )

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA foreign_keys = ON")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS gift_cards (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    plan TEXT NOT NULL,
                    price REAL NOT NULL,
                    model_key TEXT NOT NULL,
                    manual_limit INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    daily_limit INTEGER NOT NULL,
                    computed_daily_tokens INTEGER NOT NULL,
                    max_cost_usd REAL NOT NULL,
                    used_by_account_id TEXT NOT NULL DEFAULT '',
                    used_by_login TEXT NOT NULL DEFAULT '',
                    used_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    api_token TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    login TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    price REAL NOT NULL,
                    model_key TEXT NOT NULL,
                    manual_limit INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    gift_card_code TEXT NOT NULL,
                    used_today INTEGER NOT NULL DEFAULT 0,
                    daily_limit INTEGER NOT NULL,
                    computed_daily_tokens INTEGER NOT NULL,
                    max_cost_usd REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _gift_code_exists(self, db: sqlite3.Connection, code: str) -> bool:
        row = db.execute("SELECT 1 FROM gift_cards WHERE code = ?", (code,)).fetchone()
        return row is not None

    def _login_exists(self, db: sqlite3.Connection, login: str) -> bool:
        row = db.execute("SELECT 1 FROM accounts WHERE login = ?", (login,)).fetchone()
        return row is not None

    def _find_gift_card(self, db: sqlite3.Connection, card_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM gift_cards WHERE id = ?", (card_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Gift card not found.")
        return _gift_card_from_row(row)

    def _gift_card_by_code(self, db: sqlite3.Connection, code: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM gift_cards WHERE code = ?", (code,)).fetchone()
        return _gift_card_from_row(row) if row else None

    def _find_account(self, db: sqlite3.Connection, account_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found.")
        return _account_from_row(row)


def _gift_card_from_values(values: dict[str, Any], code: str, settings: Settings) -> dict[str, Any]:
    model_key = _normalize_model_key(values.get("model") or values.get("modelKey"))
    price = max(0.0, float(values.get("price") or 0))
    manual_limit = int(float(values.get("manualLimit") or values.get("manual_limit") or 0))
    limit = _calculate_limit(price, model_key, manual_limit, settings)
    return {
        "id": f"gift_{secrets.token_hex(12)}",
        "code": code,
        "plan": str(values.get("plan") or MODEL_LABELS[model_key]).strip(),
        "price": price,
        "modelKey": model_key,
        "manualLimit": manual_limit,
        "active": bool(values.get("active", True)),
        "dailyLimit": limit["dailyLimit"],
        "computedDailyTokens": limit["computedDailyTokens"],
        "maxCostUsd": limit["maxCostUsd"],
        "usedByAccountId": "",
        "usedByLogin": "",
        "usedAt": "",
        "createdAt": _now(),
    }


def _account_from_gift_card(
    gift_card: dict[str, Any],
    name: str,
    login: str,
    password: str,
) -> dict[str, Any]:
    return {
        "id": f"acct_{secrets.token_hex(12)}",
        "apiToken": f"cus_{secrets.token_urlsafe(24)}",
        "name": name,
        "displayName": name,
        "login": login,
        "passwordHash": hash_password(password),
        "plan": gift_card["plan"],
        "price": gift_card["price"],
        "modelKey": gift_card["modelKey"],
        "manualLimit": gift_card["manualLimit"],
        "active": True,
        "giftCardCode": gift_card["code"],
        "usedToday": 0,
        "dailyLimit": gift_card["dailyLimit"],
        "computedDailyTokens": gift_card["computedDailyTokens"],
        "maxCostUsd": gift_card["maxCostUsd"],
        "createdAt": _now(),
    }


def _calculate_limit(
    price_brl: float,
    model_key: str,
    manual_limit: int,
    settings: Settings,
) -> dict[str, float | int]:
    monthly_revenue = max(0.0, price_brl)
    max_cost_brl = monthly_revenue * (1 - settings.customer_profit_margin)
    max_cost_usd = max_cost_brl / max(0.01, settings.usd_to_brl)
    daily_cost_usd = max_cost_usd / 30
    computed = math.floor(daily_cost_usd / MODEL_TOKEN_PRICES[model_key])
    daily_limit = min(manual_limit, computed) if manual_limit > 0 else computed
    return {
        "dailyLimit": max(0, daily_limit),
        "computedDailyTokens": max(0, computed),
        "maxCostUsd": max_cost_usd,
    }


def _public_gift_card(card: dict[str, Any]) -> dict[str, Any]:
    return dict(card)


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    public = dict(account)
    public.pop("passwordHash", None)
    return public


def _gift_card_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "plan": row["plan"],
        "price": row["price"],
        "modelKey": row["model_key"],
        "manualLimit": row["manual_limit"],
        "active": bool(row["active"]),
        "dailyLimit": row["daily_limit"],
        "computedDailyTokens": row["computed_daily_tokens"],
        "maxCostUsd": row["max_cost_usd"],
        "usedByAccountId": row["used_by_account_id"],
        "usedByLogin": row["used_by_login"],
        "usedAt": row["used_at"],
        "createdAt": row["created_at"],
    }


def _account_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "apiToken": row["api_token"],
        "name": row["name"],
        "displayName": row["display_name"],
        "login": row["login"],
        "passwordHash": row["password_hash"],
        "plan": row["plan"],
        "price": row["price"],
        "modelKey": row["model_key"],
        "manualLimit": row["manual_limit"],
        "active": bool(row["active"]),
        "giftCardCode": row["gift_card_code"],
        "usedToday": row["used_today"],
        "dailyLimit": row["daily_limit"],
        "computedDailyTokens": row["computed_daily_tokens"],
        "maxCostUsd": row["max_cost_usd"],
        "createdAt": row["created_at"],
    }


def _gift_card_values(card: dict[str, Any]) -> tuple[Any, ...]:
    return (
        card["id"],
        card["code"],
        card["plan"],
        card["price"],
        card["modelKey"],
        card["manualLimit"],
        int(card["active"]),
        card["dailyLimit"],
        card["computedDailyTokens"],
        card["maxCostUsd"],
        card["usedByAccountId"],
        card["usedByLogin"],
        card["usedAt"],
        card["createdAt"],
    )


def _account_values(account: dict[str, Any]) -> tuple[Any, ...]:
    return (
        account["id"],
        account["apiToken"],
        account["name"],
        account["displayName"],
        account["login"],
        account["passwordHash"],
        account["plan"],
        account["price"],
        account["modelKey"],
        account["manualLimit"],
        int(account["active"]),
        account["giftCardCode"],
        account["usedToday"],
        account["dailyLimit"],
        account["computedDailyTokens"],
        account["maxCostUsd"],
        account["createdAt"],
    )


def _normalize_model_key(value: Any) -> str:
    raw = str(value or "").lower()
    if "haiku" in raw or "economy" in raw:
        return "haiku"
    if "opus" in raw or "ultra" in raw:
        return "opus"
    return "sonnet"


def _normalize_gift_code(value: Any) -> str:
    return "-".join(
        part
        for part in "".join(char if char.isalnum() else "-" for char in str(value or "").upper())
        .strip("-")
        .split("-")
        if part
    )


def _generate_gift_code() -> str:
    return f"CLAUDE-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
