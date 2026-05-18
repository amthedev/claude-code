from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException, status

from .config import Settings
from .customers import CustomerPlan


MODEL_LABELS = {
    "haiku": "Claude Haiku 4.5",
    "sonnet": "Claude Sonnet 4.6",
    "opus": "Claude Opus 4.7",
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

    def list_gift_cards(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public_gift_card(card) for card in self._read()["gift_cards"]]

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public_account(account) for account in self._read()["accounts"]]

    def create_gift_card(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            code = _normalize_gift_code(values.get("code")) or _generate_gift_code()
            while any(card["code"] == code for card in data["gift_cards"]):
                if values.get("code"):
                    raise HTTPException(status_code=409, detail="Gift card already exists.")
                code = _generate_gift_code()

            card = _gift_card_from_values(values, code, self.settings)
            data["gift_cards"].insert(0, card)
            self._write(data)
            return _public_gift_card(card)

    def update_gift_card(self, card_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            card = _find_by_id(data["gift_cards"], card_id, "Gift card")
            if card.get("usedByAccountId"):
                raise HTTPException(status_code=409, detail="Used gift cards cannot be changed.")
            if "active" in values:
                card["active"] = bool(values["active"])
            self._write(data)
            return _public_gift_card(card)

    def delete_gift_card(self, card_id: str) -> dict[str, str]:
        with self._lock:
            data = self._read()
            before = len(data["gift_cards"])
            data["gift_cards"] = [card for card in data["gift_cards"] if card["id"] != card_id]
            if len(data["gift_cards"]) == before:
                raise HTTPException(status_code=404, detail="Gift card not found.")
            self._write(data)
            return {"status": "deleted"}

    def signup(self, values: dict[str, Any]) -> dict[str, Any]:
        login = str(values.get("login") or "").strip().lower()
        name = str(values.get("name") or "").strip()
        password = str(values.get("password") or "")
        gift_code = _normalize_gift_code(values.get("giftCard") or values.get("gift_card"))

        if not name or not login or not password or not gift_code:
            raise HTTPException(status_code=400, detail="Name, e-mail, password, and gift card are required.")
        if "@" not in login:
            raise HTTPException(status_code=400, detail="Use a valid e-mail.")

        with self._lock:
            data = self._read()
            if any(account["login"].lower() == login for account in data["accounts"]):
                raise HTTPException(status_code=409, detail="This e-mail is already registered.")

            gift_card = next((card for card in data["gift_cards"] if card["code"] == gift_code), None)
            if not gift_card or not gift_card.get("active") or gift_card.get("usedByAccountId"):
                raise HTTPException(status_code=400, detail="Gift card is invalid, paused, or already used.")

            account = _account_from_gift_card(gift_card, name, login, password)
            data["accounts"].append(account)
            gift_card["active"] = False
            gift_card["usedByAccountId"] = account["id"]
            gift_card["usedByLogin"] = login
            gift_card["usedAt"] = _now()
            self._write(data)
            return _public_account(account)

    def login(self, values: dict[str, Any]) -> dict[str, Any]:
        login = str(values.get("login") or "").strip().lower()
        password = str(values.get("password") or "")
        with self._lock:
            data = self._read()
            account = next((item for item in data["accounts"] if item["login"].lower() == login), None)
            if not account or not _verify_password(password, account["passwordHash"]):
                raise HTTPException(status_code=403, detail="Invalid e-mail or password.")
            if not account.get("active"):
                raise HTTPException(status_code=403, detail="Account is paused.")
            return _public_account(account)

    def update_account(self, account_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            account = _find_by_id(data["accounts"], account_id, "Account")
            if "active" in values:
                account["active"] = bool(values["active"])
            if values.get("resetUsage"):
                account["usedToday"] = 0
            self._write(data)
            return _public_account(account)

    def delete_account(self, account_id: str) -> dict[str, str]:
        with self._lock:
            data = self._read()
            before = len(data["accounts"])
            data["accounts"] = [account for account in data["accounts"] if account["id"] != account_id]
            if len(data["accounts"]) == before:
                raise HTTPException(status_code=404, detail="Account not found.")
            self._write(data)
            return {"status": "deleted"}

    def customer_plan_for_token(self, token: str) -> CustomerPlan | None:
        with self._lock:
            for account in self._read()["accounts"]:
                if hmac.compare_digest(token, account.get("apiToken", "")):
                    return CustomerPlan(
                        token=token,
                        name=account["name"],
                        monthly_price_brl=float(account.get("price") or 0),
                        daily_token_limit=int(account.get("dailyLimit") or 0),
                        allowed_model=BACKEND_MODELS.get(account.get("modelKey"), "claude-code-pro"),
                        active=bool(account.get("active")),
                    )
        return None

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"gift_cards": [], "accounts": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Account data file is corrupted.",
            ) from exc
        if not isinstance(data, dict):
            return {"gift_cards": [], "accounts": []}
        gift_cards = data.get("gift_cards") if isinstance(data.get("gift_cards"), list) else []
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        return {"gift_cards": gift_cards, "accounts": accounts}

    def _write(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


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
        "passwordHash": _hash_password(password),
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


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}:{digest}"


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, digest = password_hash.split(":", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(candidate, digest)


def _find_by_id(items: list[dict[str, Any]], item_id: str, label: str) -> dict[str, Any]:
    item = next((candidate for candidate in items if candidate["id"] == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label} not found.")
    return item


def _now() -> str:
    return datetime.now(UTC).isoformat()
