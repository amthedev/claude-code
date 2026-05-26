from __future__ import annotations

import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException

from .config import Settings
from .customers import CustomerPlan, TOKEN_VALUE_MULTIPLIER, estimate_request_tokens, reasoning_token_multiplier
from .security import hash_password, verify_password


MODEL_LABELS = {
    "haiku": "Plano Econômico",
    "sonnet": "Plano Padrão",
    "opus": "Plano Avançado",
}

PUBLIC_MODELS_BY_KEY = {
    "haiku": "claude-code-economy",
    "sonnet": "claude-code-pro",
    "opus": "*",
}

PLAN_CATALOG = (
    {
        "id": "free",
        "name": "Grátis",
        "description": "Para testar o chat com respostas básicas.",
        "price": 0.0,
        "modelKey": "haiku",
        "manualLimit": 1600,
        "checkoutMode": "instant",
    },
    {
        "id": "starter",
        "name": "Pro",
        "description": "Para conversas, estudos e tarefas do dia a dia.",
        "price": 65.00,
        "modelKey": "haiku",
        "manualLimit": 128000,
        "checkoutMode": "mercado_pago",
    },
    {
        "id": "pro",
        "name": "5X",
        "description": "Mais limite e força para trabalho diário.",
        "price": 125.00,
        "modelKey": "sonnet",
        "manualLimit": 400000,
        "checkoutMode": "mercado_pago",
    },
    {
        "id": "twentyx",
        "name": "20X",
        "description": "Mais força e limite para uso pesado.",
        "price": 280.00,
        "modelKey": "opus",
        "manualLimit": 800000,
        "checkoutMode": "mercado_pago",
    },
    {
        "id": "ultra",
        "name": "30X",
        "description": "O maior limite para equipes e rotinas intensas.",
        "price": 390.00,
        "modelKey": "opus",
        "manualLimit": 1200000,
        "checkoutMode": "mercado_pago",
    },
)

MODEL_TOKEN_PRICES = {
    "haiku": 0.000000224,
    "sonnet": 0.00000087,
    "opus": 0.00000087,
}

PLAN_LIMIT_TOKEN_PRICE = MODEL_TOKEN_PRICES["sonnet"]
API_ONLY_GIFT_MARKER = "__api_only__"
API_ONLY_PROFIT_MARGIN = 0.20
API_ONLY_DEFAULT_DURATION_HOURS = 24

@dataclass(frozen=True, slots=True)
class AccountUsageReservation:
    token: str
    usage_day: str
    estimated_tokens: int


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
            self._reset_stale_usage(db)
            rows = db.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
        return [_public_account(_account_from_row(row)) for row in rows]

    def list_plans(self) -> list[dict[str, Any]]:
        return [_public_plan(plan, self.settings) for plan in PLAN_CATALOG]

    def list_purchases(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM purchases ORDER BY created_at DESC").fetchall()
        return [_purchase_from_row(row) for row in rows]

    def list_purchases_for_token(self, token: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            account_row = db.execute("SELECT id FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if not account_row:
                raise HTTPException(status_code=404, detail="Account not found.")
            rows = db.execute(
                "SELECT * FROM purchases WHERE account_id = ? ORDER BY created_at DESC",
                (account_row["id"],),
            ).fetchall()
        return [_purchase_from_row(row) for row in rows]

    def account_for_token(self, token: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            self._reset_stale_usage(db)
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Account not found.")
            account = self._maybe_promote_public_trial(db, _account_from_row(row))
            return _public_account(account)

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

    def create_api_token(self, values: dict[str, Any]) -> dict[str, Any]:
        account = _api_only_account_from_values(values, self.settings)
        with self._lock, self._connect() as db:
            while self._login_exists(db, account["login"]):
                account["login"] = _api_only_login()
            while db.execute("SELECT 1 FROM accounts WHERE api_token = ?", (account["apiToken"],)).fetchone():
                account["apiToken"] = _generate_api_token()
            db.execute(
                """
                INSERT INTO accounts (
                    id, api_token, name, display_name, login, password_hash, plan, price,
                    model_key, manual_limit, active, gift_card_code, used_today, usage_day,
                    daily_limit, computed_daily_tokens, max_cost_usd, created_at, trial_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _account_values(account),
            )
            db.commit()
        return _public_account(account)

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

        if not name or not login or not password:
            raise HTTPException(status_code=400, detail="Name, e-mail, and password are required.")
        if "@" not in login or len(login) > 254:
            raise HTTPException(status_code=400, detail="Use a valid e-mail.")

        with self._lock, self._connect() as db:
            if self._login_exists(db, login):
                raise HTTPException(status_code=409, detail="This e-mail is already registered.")

            if gift_code:
                gift_card = self._gift_card_by_code(db, gift_code)
                if not gift_card or not gift_card.get("active") or gift_card.get("usedByAccountId"):
                    raise HTTPException(status_code=400, detail="Gift card is invalid, paused, or already used.")
                account = _account_from_gift_card(gift_card, name, login, password)
            else:
                account = _public_signup_account(name, login, password, self.settings)
            db.execute(
                """
                INSERT INTO accounts (
                    id, api_token, name, display_name, login, password_hash, plan, price,
                    model_key, manual_limit, active, gift_card_code, used_today, usage_day,
                    daily_limit, computed_daily_tokens, max_cost_usd, created_at, trial_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _account_values(account),
            )
            if gift_code:
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
            self._reset_stale_usage(db)
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
            account = self._maybe_promote_public_trial(db, account)
            return _public_account(account)

    def admin_configured(self) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT value FROM admin_settings WHERE key = 'password_hash'",
            ).fetchone()
        return bool(row and row["value"])

    def setup_admin(self, values: dict[str, Any]) -> dict[str, Any]:
        username = str(values.get("login") or values.get("username") or "admin").strip()
        password = str(values.get("password") or "")
        if not username or not password:
            raise HTTPException(status_code=400, detail="Admin username and password are required.")

        password_hash = hash_password(password)
        with self._lock, self._connect() as db:
            if self._admin_password_hash(db):
                raise HTTPException(status_code=409, detail="Admin password is already configured.")
            db.execute(
                """
                INSERT INTO admin_settings (key, value)
                VALUES ('username', ?), ('password_hash', ?)
                """,
                (username, password_hash),
            )
            token = self._create_admin_session(db)
            db.commit()
        return {"token": token, "username": username}

    def login_admin(self, values: dict[str, Any]) -> dict[str, Any]:
        login = str(values.get("login") or values.get("username") or "").strip()
        password = str(values.get("password") or "")
        with self._lock, self._connect() as db:
            username = self._admin_setting(db, "username") or "admin"
            password_hash = self._admin_password_hash(db)
            if not password_hash:
                raise HTTPException(status_code=503, detail="Admin password is not configured.")
            ok, needs_rehash = verify_password(password, password_hash)
            if not ok or login != username:
                raise HTTPException(status_code=403, detail="Invalid admin login.")
            if needs_rehash:
                password_hash = hash_password(password)
                db.execute(
                    """
                    INSERT INTO admin_settings (key, value)
                    VALUES ('password_hash', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (password_hash,),
                )
            token = self._create_admin_session(db)
            db.commit()
        return {"token": token, "username": username}

    def admin_session_for_token(self, token: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            now = _now()
            db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
            row = db.execute(
                """
                SELECT token, username, expires_at
                  FROM admin_sessions
                 WHERE token = ? AND expires_at > ?
                """,
                (token, now),
            ).fetchone()
            db.commit()
        return dict(row) if row else None

    def update_account(self, account_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            account = self._find_account(db, account_id)
            if "active" in values:
                account["active"] = bool(values["active"])
            if values.get("resetUsage"):
                account["usedToday"] = 0
                account["usageDay"] = _today()
            db.execute(
                "UPDATE accounts SET active = ?, used_today = ?, usage_day = ? WHERE id = ?",
                (int(account["active"]), int(account["usedToday"]), account["usageDay"], account_id),
            )
            db.commit()
        return _public_account(account)

    def create_purchase(self, token: str, values: dict[str, Any]) -> dict[str, Any]:
        plan_id = str(values.get("planId") or values.get("plan_id") or "").strip().lower()
        plan = _plan_by_id(plan_id)
        if not plan or plan["id"] == "free":
            raise HTTPException(status_code=400, detail="Choose a paid plan.")

        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Account not found.")
            account = _account_from_row(row)
            purchase = _purchase_for_plan(account, plan, self.settings)
            purchase["paymentMethod"] = _normalize_payment_method(values.get("paymentMethod") or values.get("payment_method"))
            payer_document = _normalize_document(
                values.get("payerDocument") or values.get("payer_document") or values.get("cpf")
            )
            if payer_document:
                purchase["payerDocument"] = payer_document
            db.execute(
                """
                INSERT INTO purchases (
                    id, account_id, login, name, plan_id, plan, price, model_key,
                    manual_limit, daily_limit, max_cost_usd, status, payment_method,
                    created_at, paid_at, mercado_pago_preference_id, mercado_pago_payment_id,
                    checkout_url, sandbox_checkout_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _purchase_values(purchase),
            )
            db.commit()
        return purchase

    def update_purchase_checkout(
        self,
        purchase_id: str,
        *,
        preference_id: str,
        checkout_url: str,
        sandbox_checkout_url: str = "",
        payment_method: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            purchase = self._find_purchase(db, purchase_id)
            next_payment_method = payment_method or purchase["paymentMethod"]
            db.execute(
                """
                UPDATE purchases
                   SET mercado_pago_preference_id = ?,
                       checkout_url = ?,
                       sandbox_checkout_url = ?,
                       payment_method = ?
                 WHERE id = ?
                """,
                (preference_id, checkout_url, sandbox_checkout_url, next_payment_method, purchase_id),
            )
            db.commit()
            purchase["mercadoPagoPreferenceId"] = preference_id
            purchase["checkoutUrl"] = checkout_url
            purchase["sandboxCheckoutUrl"] = sandbox_checkout_url
            purchase["paymentMethod"] = next_payment_method
        return purchase

    def update_purchase_payment(
        self,
        purchase_id: str,
        *,
        payment_id: str,
        payment_method: str,
        status: str = "pending",
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            purchase = self._find_purchase(db, purchase_id)
            normalized_status = _purchase_status_from_payment(str(status or "").lower())
            db.execute(
                """
                UPDATE purchases
                   SET mercado_pago_payment_id = ?,
                       payment_method = ?,
                       status = ?
                 WHERE id = ?
                """,
                (payment_id, payment_method, normalized_status, purchase_id),
            )
            db.commit()
            purchase["mercadoPagoPaymentId"] = payment_id
            purchase["paymentMethod"] = payment_method
            purchase["status"] = normalized_status
        return purchase

    def approve_purchase_from_payment(
        self,
        purchase_id: str,
        *,
        payment_id: str,
        status: str,
    ) -> dict[str, Any]:
        if status != "approved":
            with self._lock, self._connect() as db:
                purchase = self._find_purchase(db, purchase_id)
                db.execute(
                    "UPDATE purchases SET mercado_pago_payment_id = ?, status = ? WHERE id = ?",
                    (payment_id, _purchase_status_from_payment(status), purchase_id),
                )
                db.commit()
                purchase["mercadoPagoPaymentId"] = payment_id
                purchase["status"] = _purchase_status_from_payment(status)
            return purchase

        purchase = self.approve_purchase(purchase_id)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE purchases SET mercado_pago_payment_id = ? WHERE id = ?",
                (payment_id, purchase_id),
            )
            db.commit()
        purchase["mercadoPagoPaymentId"] = payment_id
        return purchase

    def approve_purchase_from_payment_for_token(
        self,
        token: str,
        purchase_id: str,
        *,
        payment_id: str,
        status: str,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            account_row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if not account_row:
                raise HTTPException(status_code=404, detail="Account not found.")
            account = _account_from_row(account_row)
            purchase = self._find_purchase(db, purchase_id)
            if purchase["accountId"] != account["id"]:
                raise HTTPException(status_code=404, detail="Purchase not found.")

            status_value = str(status or "").lower()
            normalized_status = _purchase_status_from_payment(status_value)
            if status_value == "approved" and purchase["status"] != "paid":
                upgraded = _apply_plan_to_account(account, purchase)
                paid_at = _now()
                db.execute(
                    """
                    UPDATE accounts
                       SET plan = ?, price = ?, model_key = ?, manual_limit = ?,
                           daily_limit = ?, computed_daily_tokens = ?, max_cost_usd = ?,
                           trial_expires_at = ''
                     WHERE id = ?
                    """,
                    (
                        upgraded["plan"],
                        upgraded["price"],
                        upgraded["modelKey"],
                        upgraded["manualLimit"],
                        upgraded["dailyLimit"],
                        upgraded["computedDailyTokens"],
                        upgraded["maxCostUsd"],
                        upgraded["id"],
                    ),
                )
                db.execute(
                    """
                    UPDATE purchases
                       SET mercado_pago_payment_id = ?, status = 'paid', paid_at = ?
                     WHERE id = ?
                    """,
                    (payment_id, paid_at, purchase_id),
                )
                db.commit()
                account = upgraded
                purchase["status"] = "paid"
                purchase["paidAt"] = paid_at
            else:
                db.execute(
                    "UPDATE purchases SET mercado_pago_payment_id = ?, status = ? WHERE id = ?",
                    (payment_id, normalized_status, purchase_id),
                )
                db.commit()
                purchase["status"] = normalized_status

            purchase["mercadoPagoPaymentId"] = payment_id
            return {"account": _public_account(account), "purchase": purchase}

    def approve_purchase(self, purchase_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            purchase = self._find_purchase(db, purchase_id)
            if purchase["status"] != "paid":
                account = self._find_account(db, purchase["accountId"])
                upgraded = _apply_plan_to_account(account, purchase)
                paid_at = _now()
                db.execute(
                    """
                    UPDATE accounts
                       SET plan = ?, price = ?, model_key = ?, manual_limit = ?,
                           daily_limit = ?, computed_daily_tokens = ?, max_cost_usd = ?,
                           trial_expires_at = ''
                     WHERE id = ?
                    """,
                    (
                        upgraded["plan"],
                        upgraded["price"],
                        upgraded["modelKey"],
                        upgraded["manualLimit"],
                        upgraded["dailyLimit"],
                        upgraded["computedDailyTokens"],
                        upgraded["maxCostUsd"],
                        upgraded["id"],
                    ),
                )
                db.execute(
                    "UPDATE purchases SET status = 'paid', paid_at = ? WHERE id = ?",
                    (paid_at, purchase_id),
                )
                db.commit()
                purchase["status"] = "paid"
                purchase["paidAt"] = paid_at
        return purchase

    def cancel_purchase(self, purchase_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            purchase = self._find_purchase(db, purchase_id)
            if purchase["status"] == "paid":
                raise HTTPException(status_code=409, detail="Paid purchases cannot be canceled.")
            db.execute("UPDATE purchases SET status = 'canceled' WHERE id = ?", (purchase_id,))
            db.commit()
            purchase["status"] = "canceled"
        return purchase

    def delete_account(self, account_id: str) -> dict[str, str]:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Account not found.")
        return {"status": "deleted"}

    def customer_plan_for_token(self, token: str) -> CustomerPlan | None:
        with self._lock, self._connect() as db:
            self._reset_stale_usage(db)
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if row:
                account = self._maybe_promote_public_trial(db, _account_from_row(row))
        if not row:
            return None
        return CustomerPlan(
            token=token,
            name=account["name"],
            monthly_price_brl=float(account.get("price") or 0),
            daily_token_limit=int(account.get("dailyLimit") or 0),
            allowed_model=PUBLIC_MODELS_BY_KEY.get(account.get("modelKey"), self.settings.economy_public_model),
            active=bool(account.get("active")),
            preferred_model=str(account.get("preferredModel") or ""),
            preferred_reasoning=str(account.get("preferredReasoning") or ""),
        )

    def update_preferences_for_token(
        self,
        token: str,
        *,
        model: str | None = None,
        reasoning: str | None = None,
    ) -> dict[str, str] | None:
        updates: list[str] = []
        params: list[Any] = []
        if model is not None:
            updates.append("preferred_model = ?")
            params.append(model)
        if reasoning is not None:
            updates.append("preferred_reasoning = ?")
            params.append(reasoning)
        if not updates:
            return None

        with self._lock, self._connect() as db:
            row = db.execute("SELECT id FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if not row:
                return None
            params.extend([token])
            db.execute(
                f"UPDATE accounts SET {', '.join(updates)} WHERE api_token = ?",
                tuple(params),
            )
            db.commit()
        return {
            "preferredModel": model or "",
            "preferredReasoning": reasoning or "",
        }

    def reserve_usage_for_token(
        self,
        token: str,
        payload: dict[str, Any],
        decision: Any | None = None,
    ) -> AccountUsageReservation | None:
        estimated_tokens = estimate_request_tokens(payload, self.settings) * reasoning_token_multiplier(
            str(payload.get("__gateway_reasoning_mode") or "normal"),
            decision,
        )
        today = _today()
        with self._lock, self._connect() as db:
            self._reset_stale_usage(db)
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if not row:
                return None
            account = self._maybe_promote_public_trial(db, _account_from_row(row))
            if not account.get("active"):
                raise HTTPException(status_code=403, detail="Account is paused.")

            daily_limit = int(account.get("dailyLimit") or 0)
            used_today = int(account.get("usedToday") or 0)
            if daily_limit <= 0:
                raise HTTPException(status_code=402, detail="Account has no daily token limit configured.")
            if used_today + estimated_tokens > daily_limit:
                remaining = max(0, daily_limit - used_today)
                raise HTTPException(
                    status_code=429,
                    detail=f"Limite diário insuficiente. Restam {remaining} tokens.",
                )

            db.execute(
                """
                UPDATE accounts
                   SET used_today = used_today + ?,
                       usage_day = ?
                 WHERE id = ?
                """,
                (estimated_tokens, today, account["id"]),
            )
            db.commit()
        return AccountUsageReservation(token=token, usage_day=today, estimated_tokens=estimated_tokens)

    def rollback_usage(self, reservation: AccountUsageReservation | None) -> None:
        if reservation is None:
            return
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (reservation.token,)).fetchone()
            if not row:
                return
            account = _account_from_row(row)
            if account.get("usageDay") != reservation.usage_day:
                return
            db.execute(
                """
                UPDATE accounts
                   SET used_today = ?
                 WHERE id = ?
                """,
                (max(0, int(account.get("usedToday") or 0) - reservation.estimated_tokens), account["id"]),
            )
            db.commit()

    def usage_snapshot_for_token(self, token: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            self._reset_stale_usage(db)
            row = db.execute("SELECT * FROM accounts WHERE api_token = ?", (token,)).fetchone()
            if row:
                account = self._maybe_promote_public_trial(db, _account_from_row(row))
        if not row:
            return None
        daily_limit = int(account.get("dailyLimit") or 0)
        used_today = int(account.get("usedToday") or 0)
        is_api_only = account.get("giftCardCode") == API_ONLY_GIFT_MARKER
        daily_cost_budget_usd = float(account.get("maxCostUsd") or 0)
        if not is_api_only:
            daily_cost_budget_usd = daily_cost_budget_usd / 30
        return {
            "customer": {
                "name": account["name"],
                "allowed_model": PUBLIC_MODELS_BY_KEY.get(account.get("modelKey"), self.settings.economy_public_model),
                "monthly_price_brl": float(account.get("price") or 0),
                "daily_token_limit": daily_limit,
                "active": bool(account.get("active")),
            },
            "today": {
                "date": account.get("usageDay") or _today(),
                "requests": None,
                "reserved_cost_usd": None,
                "daily_cost_budget_usd": round(daily_cost_budget_usd, 8),
                "reserved_tokens": used_today,
                "remaining_cost_usd": None,
                "remaining_tokens": max(0, daily_limit - used_today),
            },
        }

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
                    usage_day TEXT NOT NULL DEFAULT '',
                    daily_limit INTEGER NOT NULL,
                    computed_daily_tokens INTEGER NOT NULL,
                    max_cost_usd REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    trial_expires_at TEXT NOT NULL DEFAULT '',
                    preferred_model TEXT NOT NULL DEFAULT '',
                    preferred_reasoning TEXT NOT NULL DEFAULT ''
                )
                """
            )
            if not _column_exists(db, "accounts", "usage_day"):
                db.execute("ALTER TABLE accounts ADD COLUMN usage_day TEXT NOT NULL DEFAULT ''")
            if not _column_exists(db, "accounts", "trial_expires_at"):
                db.execute("ALTER TABLE accounts ADD COLUMN trial_expires_at TEXT NOT NULL DEFAULT ''")
            if not _column_exists(db, "accounts", "preferred_model"):
                db.execute("ALTER TABLE accounts ADD COLUMN preferred_model TEXT NOT NULL DEFAULT ''")
            if not _column_exists(db, "accounts", "preferred_reasoning"):
                db.execute("ALTER TABLE accounts ADD COLUMN preferred_reasoning TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE accounts SET usage_day = ? WHERE usage_day = ''", (_today(),))
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS purchases (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    login TEXT NOT NULL,
                    name TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    price REAL NOT NULL,
                    model_key TEXT NOT NULL,
                    manual_limit INTEGER NOT NULL,
                    daily_limit INTEGER NOT NULL,
                    max_cost_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    paid_at TEXT NOT NULL,
                    mercado_pago_preference_id TEXT NOT NULL DEFAULT '',
                    mercado_pago_payment_id TEXT NOT NULL DEFAULT '',
                    checkout_url TEXT NOT NULL DEFAULT '',
                    sandbox_checkout_url TEXT NOT NULL DEFAULT ''
                )
                """
            )
            for column, definition in {
                "mercado_pago_preference_id": "TEXT NOT NULL DEFAULT ''",
                "mercado_pago_payment_id": "TEXT NOT NULL DEFAULT ''",
                "checkout_url": "TEXT NOT NULL DEFAULT ''",
                "sandbox_checkout_url": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if not _column_exists(db, "purchases", column):
                    db.execute(f"ALTER TABLE purchases ADD COLUMN {column} {definition}")
            free_plan = _plan_by_id("free") or PLAN_CATALOG[0]
            free_limit = _calculate_limit(
                free_plan["price"],
                free_plan["modelKey"],
                free_plan["manualLimit"],
                self.settings,
            )
            db.execute(
                """
                UPDATE accounts
                   SET plan = ?,
                       model_key = ?,
                       manual_limit = ?,
                       daily_limit = ?,
                       computed_daily_tokens = ?,
                       max_cost_usd = ?
                 WHERE price <= 0
                   AND gift_card_code = ''
                   AND trial_expires_at = ''
                """,
                (
                    free_plan["name"],
                    free_plan["modelKey"],
                    free_plan["manualLimit"],
                    free_limit["dailyLimit"],
                    free_limit["computedDailyTokens"],
                    free_limit["maxCostUsd"],
                ),
            )
            self._sync_public_trials(db)
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _reset_stale_usage(self, db: sqlite3.Connection) -> None:
        self._sync_public_trials(db)
        now = _now()
        db.execute(
            """
            UPDATE accounts
               SET active = 0
             WHERE gift_card_code = ?
               AND trial_expires_at <> ''
               AND trial_expires_at <= ?
            """,
            (API_ONLY_GIFT_MARKER, now),
        )
        today = _today()
        db.execute(
            """
            UPDATE accounts
               SET used_today = 0,
                   usage_day = ?
             WHERE usage_day <> ?
            """,
            (today, today),
        )

    def _sync_public_trials(self, db: sqlite3.Connection) -> None:
        status = public_trial_status(self.settings)
        rows = db.execute(
            "SELECT * FROM accounts WHERE trial_expires_at <> '' AND gift_card_code = ''"
        ).fetchall()
        for row in rows:
            expires_at = _parse_datetime(row["trial_expires_at"])
            should_downgrade = (
                not status["active"]
                or expires_at is None
                or expires_at <= _now_datetime()
            )
            if not should_downgrade:
                continue
            account = _account_from_row(row)
            downgraded = _free_account_from_existing(account, self.settings)
            db.execute(
                """
                UPDATE accounts
                   SET plan = ?, price = ?, model_key = ?, manual_limit = ?,
                       daily_limit = ?, computed_daily_tokens = ?, max_cost_usd = ?,
                       trial_expires_at = ?
                 WHERE id = ?
                """,
                (
                    downgraded["plan"],
                    downgraded["price"],
                    downgraded["modelKey"],
                    downgraded["manualLimit"],
                    downgraded["dailyLimit"],
                    downgraded["computedDailyTokens"],
                    downgraded["maxCostUsd"],
                    "",
                    downgraded["id"],
                ),
            )

    def _maybe_promote_public_trial(
        self,
        db: sqlite3.Connection,
        account: dict[str, Any],
    ) -> dict[str, Any]:
        if not _free_signup_account_eligible_for_trial(account):
            return account
        promoted = _public_trial_account_from_existing(account, self.settings)
        if promoted is account:
            return account
        db.execute(
            """
            UPDATE accounts
               SET plan = ?, price = ?, model_key = ?, manual_limit = ?,
                   daily_limit = ?, computed_daily_tokens = ?, max_cost_usd = ?,
                   trial_expires_at = ?
             WHERE id = ?
            """,
            (
                promoted["plan"],
                promoted["price"],
                promoted["modelKey"],
                promoted["manualLimit"],
                promoted["dailyLimit"],
                promoted["computedDailyTokens"],
                promoted["maxCostUsd"],
                promoted["trialExpiresAt"],
                promoted["id"],
            ),
        )
        db.commit()
        return promoted

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

    def _find_purchase(self, db: sqlite3.Connection, purchase_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Purchase not found.")
        return _purchase_from_row(row)

    def _admin_setting(self, db: sqlite3.Connection, key: str) -> str:
        row = db.execute("SELECT value FROM admin_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def _admin_password_hash(self, db: sqlite3.Connection) -> str:
        return self._admin_setting(db, "password_hash")

    def _create_admin_session(self, db: sqlite3.Connection) -> str:
        token = f"sk-admin-{secrets.token_urlsafe(36)}"
        username = self._admin_setting(db, "username") or "admin"
        now = _now()
        expires_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() + 60 * 60 * 24 * 14, UTC).isoformat()
        db.execute(
            """
            INSERT INTO admin_sessions (token, username, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, username, now, expires_at),
        )
        return token


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


def _api_only_account_from_values(values: dict[str, Any], settings: Settings) -> dict[str, Any]:
    price = max(0.0, float(values.get("price") or 50))
    duration_hours = int(float(values.get("durationHours") or values.get("duration_hours") or API_ONLY_DEFAULT_DURATION_HOURS))
    duration_hours = max(1, min(duration_hours, 24 * 30))
    model_key = _normalize_model_key(values.get("model") or values.get("modelKey") or "opus")
    name = str(values.get("name") or "Fornecedor API").strip() or "Fornecedor API"
    limit = _calculate_api_only_limit(price, duration_hours, settings)
    expires_at = (_now_datetime() + timedelta(hours=duration_hours)).isoformat()
    return {
        "id": f"acct_{secrets.token_hex(12)}",
        "apiToken": _generate_api_token(),
        "name": name,
        "displayName": name,
        "login": _api_only_login(),
        "passwordHash": hash_password(secrets.token_urlsafe(24)),
        "plan": f"API avulsa {duration_hours}h",
        "price": price,
        "modelKey": model_key,
        "manualLimit": limit["dailyLimit"],
        "active": True,
        "giftCardCode": API_ONLY_GIFT_MARKER,
        "usedToday": 0,
        "usageDay": _today(),
        "dailyLimit": limit["dailyLimit"],
        "computedDailyTokens": limit["computedDailyTokens"],
        "maxCostUsd": limit["maxCostUsd"],
        "createdAt": _now(),
        "trialExpiresAt": expires_at,
    }


def _calculate_api_only_limit(price_brl: float, duration_hours: int, settings: Settings) -> dict[str, float | int]:
    revenue = max(0.0, price_brl)
    cost_budget_brl = revenue * (1 - API_ONLY_PROFIT_MARGIN)
    max_cost_usd = cost_budget_brl / max(0.01, settings.usd_to_brl)
    daily_cost_usd = max_cost_usd / max(1, math.ceil(duration_hours / 24))
    raw_tokens = math.floor(daily_cost_usd / PLAN_LIMIT_TOKEN_PRICE)
    displayed_tokens = max(0, raw_tokens * TOKEN_VALUE_MULTIPLIER)
    return {
        "dailyLimit": displayed_tokens,
        "computedDailyTokens": displayed_tokens,
        "maxCostUsd": max_cost_usd,
    }


def _account_from_gift_card(
    gift_card: dict[str, Any],
    name: str,
    login: str,
    password: str,
) -> dict[str, Any]:
    return {
        "id": f"acct_{secrets.token_hex(12)}",
        "apiToken": _generate_api_token(),
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
        "usageDay": _today(),
        "dailyLimit": gift_card["dailyLimit"],
        "computedDailyTokens": gift_card["computedDailyTokens"],
        "maxCostUsd": gift_card["maxCostUsd"],
        "createdAt": _now(),
        "trialExpiresAt": "",
    }


def _public_signup_account(name: str, login: str, password: str, settings: Settings) -> dict[str, Any]:
    status = public_trial_status(settings)
    if status["active"]:
        return _public_trial_account(name, login, password, settings, status)
    return _free_account(name, login, password, settings)


def _free_account(name: str, login: str, password: str, settings: Settings) -> dict[str, Any]:
    plan = _plan_by_id("free") or PLAN_CATALOG[0]
    limit = _calculate_limit(plan["price"], plan["modelKey"], plan["manualLimit"], settings)
    return {
        "id": f"acct_{secrets.token_hex(12)}",
        "apiToken": _generate_api_token(),
        "name": name,
        "displayName": name,
        "login": login,
        "passwordHash": hash_password(password),
        "plan": plan["name"],
        "price": plan["price"],
        "modelKey": plan["modelKey"],
        "manualLimit": plan["manualLimit"],
        "active": True,
        "giftCardCode": "",
        "usedToday": 0,
        "usageDay": _today(),
        "dailyLimit": limit["dailyLimit"],
        "computedDailyTokens": limit["computedDailyTokens"],
        "maxCostUsd": limit["maxCostUsd"],
        "createdAt": _now(),
        "trialExpiresAt": "",
    }


def _public_trial_account(
    name: str,
    login: str,
    password: str,
    settings: Settings,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = status or public_trial_status(settings)
    plan = _plan_by_id(str(status.get("planId") or "ultra")) or _plan_by_id("ultra") or PLAN_CATALOG[-1]
    manual_limit = int(status.get("dailyLimit") or plan["manualLimit"])
    limit = _calculate_limit(0, plan["modelKey"], manual_limit, settings)
    return {
        "id": f"acct_{secrets.token_hex(12)}",
        "apiToken": _generate_api_token(),
        "name": name,
        "displayName": name,
        "login": login,
        "passwordHash": hash_password(password),
        "plan": str(status.get("label") or settings.public_trial_label or plan["name"]),
        "price": 0.0,
        "modelKey": plan["modelKey"],
        "manualLimit": manual_limit,
        "active": True,
        "giftCardCode": "",
        "usedToday": 0,
        "usageDay": _today(),
        "dailyLimit": limit["dailyLimit"],
        "computedDailyTokens": limit["computedDailyTokens"],
        "maxCostUsd": limit["maxCostUsd"],
        "createdAt": _now(),
        "trialExpiresAt": str(status.get("endAt") or ""),
    }


def _free_account_from_existing(account: dict[str, Any], settings: Settings) -> dict[str, Any]:
    plan = _plan_by_id("free") or PLAN_CATALOG[0]
    limit = _calculate_limit(plan["price"], plan["modelKey"], plan["manualLimit"], settings)
    downgraded = dict(account)
    downgraded.update(
        {
            "plan": plan["name"],
            "price": plan["price"],
            "modelKey": plan["modelKey"],
            "manualLimit": plan["manualLimit"],
            "dailyLimit": limit["dailyLimit"],
            "computedDailyTokens": limit["computedDailyTokens"],
            "maxCostUsd": limit["maxCostUsd"],
            "trialExpiresAt": "",
        }
    )
    return downgraded


def _public_trial_account_from_existing(
    account: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    status = public_trial_status(settings)
    if not status["active"]:
        return account
    plan = _plan_by_id(str(status.get("planId") or "ultra")) or _plan_by_id("ultra") or PLAN_CATALOG[-1]
    manual_limit = int(status.get("dailyLimit") or plan["manualLimit"])
    limit = _calculate_limit(0, plan["modelKey"], manual_limit, settings)
    promoted = dict(account)
    promoted.update(
        {
            "plan": str(status.get("label") or settings.public_trial_label or plan["name"]),
            "price": 0.0,
            "modelKey": plan["modelKey"],
            "manualLimit": manual_limit,
            "dailyLimit": limit["dailyLimit"],
            "computedDailyTokens": limit["computedDailyTokens"],
            "maxCostUsd": limit["maxCostUsd"],
            "trialExpiresAt": str(status.get("endAt") or ""),
        }
    )
    return promoted


def _free_signup_account_eligible_for_trial(account: dict[str, Any]) -> bool:
    if account.get("giftCardCode"):
        return False
    if float(account.get("price") or 0) > 0:
        return False
    return not account.get("trialExpiresAt") and account.get("modelKey") == "haiku"


def _plan_by_id(plan_id: str) -> dict[str, Any] | None:
    for plan in PLAN_CATALOG:
        if plan["id"] == plan_id:
            return dict(plan)
    return None


def public_trial_status(settings: Settings) -> dict[str, Any]:
    plan = _plan_by_id(str(settings.public_trial_plan_id or "ultra").strip().lower())
    if not plan:
        plan = _plan_by_id("ultra") or PLAN_CATALOG[-1]
    end_at = _parse_datetime(settings.public_trial_end_at)
    configured = bool(settings.public_trial_enabled and settings.public_trial_end_at.strip())
    active = bool(settings.public_trial_enabled and end_at and end_at > _now_datetime())
    daily_limit = int(settings.public_trial_daily_limit or plan["manualLimit"])
    label = str(settings.public_trial_label or "Teste grátis 24h").strip() or "Teste grátis 24h"
    return {
        "configured": configured,
        "enabled": bool(settings.public_trial_enabled),
        "active": active,
        "label": label,
        "endAt": end_at.isoformat() if end_at else "",
        "planId": plan["id"],
        "planName": plan["name"],
        "modelKey": plan["modelKey"],
        "dailyLimit": max(0, daily_limit),
        "allowedModel": PUBLIC_MODELS_BY_KEY.get(plan["modelKey"], settings.economy_public_model),
    }


def _public_plan(plan: dict[str, Any], settings: Settings) -> dict[str, Any]:
    limit = _calculate_limit(plan["price"], plan["modelKey"], plan["manualLimit"], settings)
    return {
        **dict(plan),
        "dailyLimit": limit["dailyLimit"],
        "computedDailyTokens": limit["computedDailyTokens"],
        "maxCostUsd": limit["maxCostUsd"],
        "allowedModel": PUBLIC_MODELS_BY_KEY.get(plan["modelKey"], settings.economy_public_model),
    }


def _purchase_for_plan(account: dict[str, Any], plan: dict[str, Any], settings: Settings) -> dict[str, Any]:
    limit = _calculate_limit(plan["price"], plan["modelKey"], plan["manualLimit"], settings)
    return {
        "id": f"purchase_{secrets.token_hex(12)}",
        "accountId": account["id"],
        "login": account["login"],
        "name": account["name"],
        "planId": plan["id"],
        "plan": plan["name"],
        "price": plan["price"],
        "modelKey": plan["modelKey"],
        "manualLimit": plan["manualLimit"],
        "dailyLimit": limit["dailyLimit"],
        "maxCostUsd": limit["maxCostUsd"],
        "status": "pending",
        "paymentMethod": "mercado_pago",
        "createdAt": _now(),
        "paidAt": "",
        "mercadoPagoPreferenceId": "",
        "mercadoPagoPaymentId": "",
        "checkoutUrl": "",
        "sandboxCheckoutUrl": "",
    }


def _apply_plan_to_account(account: dict[str, Any], purchase: dict[str, Any]) -> dict[str, Any]:
    upgraded = dict(account)
    upgraded.update(
        {
            "plan": purchase["plan"],
            "price": purchase["price"],
            "modelKey": purchase["modelKey"],
            "manualLimit": purchase["manualLimit"],
            "dailyLimit": purchase["dailyLimit"],
            "computedDailyTokens": purchase["dailyLimit"],
            "maxCostUsd": purchase["maxCostUsd"],
            "trialExpiresAt": "",
        }
    )
    return upgraded


def _calculate_limit(
    price_brl: float,
    model_key: str,
    manual_limit: int,
    settings: Settings,
) -> dict[str, float | int]:
    monthly_revenue = max(0.0, price_brl)
    if monthly_revenue <= 0 and manual_limit > 0:
        return {
            "dailyLimit": manual_limit,
            "computedDailyTokens": manual_limit,
            "maxCostUsd": 0.0,
        }
    margin = min(max(settings.customer_profit_margin, 0.50), 0.95)
    max_cost_brl = monthly_revenue * (1 - margin)
    max_cost_usd = max_cost_brl / max(0.01, settings.usd_to_brl)
    daily_cost_usd = max_cost_usd / 30
    computed = math.floor(daily_cost_usd / PLAN_LIMIT_TOKEN_PRICE)
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
    api_only = public.get("giftCardCode") == API_ONLY_GIFT_MARKER
    expires_at = _parse_datetime(str(public.get("trialExpiresAt") or ""))
    public["apiOnly"] = api_only
    public["expiresAt"] = public.get("trialExpiresAt") if api_only else ""
    public["publicTrialActive"] = bool(not api_only and expires_at and expires_at > _now_datetime())
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
        "usageDay": row["usage_day"],
        "dailyLimit": row["daily_limit"],
        "computedDailyTokens": row["computed_daily_tokens"],
        "maxCostUsd": row["max_cost_usd"],
        "createdAt": row["created_at"],
        "trialExpiresAt": row["trial_expires_at"],
        "preferredModel": row["preferred_model"],
        "preferredReasoning": row["preferred_reasoning"],
    }


def _purchase_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "accountId": row["account_id"],
        "login": row["login"],
        "name": row["name"],
        "planId": row["plan_id"],
        "plan": row["plan"],
        "price": row["price"],
        "modelKey": row["model_key"],
        "manualLimit": row["manual_limit"],
        "dailyLimit": row["daily_limit"],
        "maxCostUsd": row["max_cost_usd"],
        "status": row["status"],
        "paymentMethod": row["payment_method"],
        "createdAt": row["created_at"],
        "paidAt": row["paid_at"],
        "mercadoPagoPreferenceId": row["mercado_pago_preference_id"],
        "mercadoPagoPaymentId": row["mercado_pago_payment_id"],
        "checkoutUrl": row["checkout_url"],
        "sandboxCheckoutUrl": row["sandbox_checkout_url"],
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
        account["usageDay"],
        account["dailyLimit"],
        account["computedDailyTokens"],
        account["maxCostUsd"],
        account["createdAt"],
        account.get("trialExpiresAt") or "",
    )


def _purchase_values(purchase: dict[str, Any]) -> tuple[Any, ...]:
    return (
        purchase["id"],
        purchase["accountId"],
        purchase["login"],
        purchase["name"],
        purchase["planId"],
        purchase["plan"],
        purchase["price"],
        purchase["modelKey"],
        purchase["manualLimit"],
        purchase["dailyLimit"],
        purchase["maxCostUsd"],
        purchase["status"],
        purchase["paymentMethod"],
        purchase["createdAt"],
        purchase["paidAt"],
        purchase["mercadoPagoPreferenceId"],
        purchase["mercadoPagoPaymentId"],
        purchase["checkoutUrl"],
        purchase["sandboxCheckoutUrl"],
    )


def _normalize_model_key(value: Any) -> str:
    raw = str(value or "").lower()
    if "haiku" in raw or "economy" in raw:
        return "haiku"
    if "opus" in raw or "ultra" in raw:
        return "opus"
    return "sonnet"


def _parse_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _now_datetime() -> datetime:
    return datetime.now(UTC)


def _normalize_document(value: Any) -> str:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if len(digits) in {11, 14}:
        return digits
    return ""


def _normalize_payment_method(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"card", "credit_card", "cartao", "cartão", "subscription", "card_subscription"}:
        return "card_subscription"
    return "pix"


def _purchase_status_from_payment(status: str) -> str:
    if status in {"approved", "paid"}:
        return "paid"
    if status in {"rejected", "cancelled", "canceled", "refunded", "charged_back"}:
        return "canceled"
    return "pending"


def _column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


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


def _generate_api_token() -> str:
    return f"sk-{secrets.token_urlsafe(36)}"


def _api_only_login() -> str:
    return f"api-{secrets.token_hex(8)}@api.local"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _today() -> str:
    return datetime.now(UTC).date().isoformat()
