from __future__ import annotations

import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException, status

from .budget import CLAUDE_BASELINE_MODEL, CostPolicy
from .config import Settings
from .routing import RouteDecision, extract_prompt_text, payload_has_tool_contract


@dataclass(frozen=True, slots=True)
class CustomerPlan:
    token: str
    name: str
    monthly_price_brl: float
    daily_token_limit: int
    allowed_model: str
    active: bool = True
    preferred_model: str = ""
    preferred_reasoning: str = ""

    @property
    def token_hash(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class CustomerReservation:
    token_hash: str
    date: str
    estimated_cost_usd: float
    estimated_tokens: int


TOKEN_VALUE_MULTIPLIER = 8
REASONING_TOKEN_MULTIPLIERS = {
    "fast": 4,
    "normal": TOKEN_VALUE_MULTIPLIER,
    "medium": 12,
    "strong": 16,
    "xstrong": 24,
}


def normalize_reasoning_mode(value: Any) -> str:
    mode = str(value or "normal").strip().lower().replace("-", "_")
    aliases = {
        "auto": "auto",
        "automatic": "auto",
        "automatico": "auto",
        "automático": "auto",
        "rapido": "fast",
        "rápido": "fast",
        "fast": "fast",
        "fraco": "fast",
        "normal": "normal",
        "medio": "medium",
        "médio": "medium",
        "medium": "medium",
        "forte": "strong",
        "strong": "strong",
        "extra": "xstrong",
        "extra_forte": "xstrong",
        "extra forte": "xstrong",
        "xstrong": "xstrong",
    }
    return aliases.get(mode, "normal")


def reasoning_token_multiplier(mode: str, decision: RouteDecision | None = None) -> int:
    normalized = normalize_reasoning_mode(mode)
    if normalized != "auto":
        return REASONING_TOKEN_MULTIPLIERS[normalized]
    if decision is None:
        return TOKEN_VALUE_MULTIPLIER
    if decision.complexity == "critical" or decision.mode == "ultra":
        return REASONING_TOKEN_MULTIPLIERS["strong"]
    if decision.complexity == "high" or decision.task_type in {
        "architecture",
        "debugging",
        "frontend",
        "review",
        "testing",
    }:
        return REASONING_TOKEN_MULTIPLIERS["medium"]
    if decision.complexity == "low" or decision.task_type == "explanation" or decision.mode == "economy":
        return REASONING_TOKEN_MULTIPLIERS["fast"]
    return TOKEN_VALUE_MULTIPLIER


def estimate_reserved_tokens(
    payload: dict[str, Any],
    settings: Settings,
    decision: RouteDecision | None = None,
) -> int:
    base_tokens = estimate_request_tokens(payload, settings)
    if payload_has_tool_contract(payload):
        return base_tokens
    return base_tokens * reasoning_token_multiplier(
        str(payload.get("__gateway_reasoning_mode") or "normal"),
        decision,
    )


def actual_reserved_tokens_from_response(
    response: dict[str, Any],
    payload: dict[str, Any],
    settings: Settings,
    decision: RouteDecision | None = None,
) -> int | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    actual_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    if actual_tokens <= 0:
        return None
    if payload_has_tool_contract(payload) or (decision and decision.use_orchestration):
        return actual_tokens
    return actual_tokens * reasoning_token_multiplier(
        str(payload.get("__gateway_reasoning_mode") or "normal"),
        decision,
    )


def parse_customer_accounts(settings: Settings) -> dict[str, CustomerPlan]:
    raw = settings.customer_accounts.strip()
    if not raw:
        return {}

    plans: dict[str, CustomerPlan] = {}
    for entry in raw.split(";"):
        if not entry.strip():
            continue

        parts = [part.strip() for part in entry.split("|")]
        if len(parts) < 3:
            raise ValueError(
                "CUSTOMER_ACCOUNTS must use token|name|monthly_price_brl|daily_tokens|model|active"
            )

        token = parts[0]
        if not token:
            raise ValueError("CUSTOMER_ACCOUNTS contains an empty token.")

        daily_token_limit = int(parts[3]) if len(parts) > 3 and parts[3] else 0
        allowed_model = parts[4] if len(parts) > 4 and parts[4] else settings.auto_public_model
        active = True
        if len(parts) > 5 and parts[5]:
            active = parts[5].lower() in {"1", "true", "yes", "y", "on", "active", "ativo"}

        plans[token] = CustomerPlan(
            token=token,
            name=parts[1] or "Cliente",
            monthly_price_brl=float(parts[2] or 0),
            daily_token_limit=max(0, daily_token_limit),
            allowed_model=allowed_model,
            active=active,
        )

    return plans


def clamp_customer_payload(payload: dict[str, Any], settings: Settings, plan: CustomerPlan) -> dict[str, Any]:
    limited = dict(payload)
    if plan.allowed_model and plan.allowed_model != "*":
        limited["model"] = plan.allowed_model
    elif not limited.get("model"):
        limited["model"] = settings.auto_public_model
    limited["max_tokens"] = min(_max_tokens(limited, settings), settings.max_request_output_tokens)
    return limited


class CustomerUsageStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.quota_data_file)
        self._lock = Lock()
        self._init_db()

    def reserve(self, plan: CustomerPlan, payload: dict[str, Any], decision: RouteDecision) -> CustomerReservation:
        if not plan.active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer plan is inactive.")

        if not decision.cost_estimate["effective_path"]["within_budget"]:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Request path is outside the configured cost budget.",
            )

        estimated_tokens = estimate_reserved_tokens(payload, self.settings, decision)
        estimated_cost_usd = estimate_request_cost_usd(
            payload,
            decision,
            self.settings,
            estimated_tokens=estimated_tokens,
        )
        daily_budget = daily_cost_budget_usd(plan, self.settings)
        if daily_budget <= 0:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Customer has no daily cost budget configured.",
            )

        day = _today()
        with self._lock:
            with self._connect() as db:
                bucket = self._bucket(db, day, plan.token_hash)
                next_cost = float(bucket["reserved_cost_usd"] or 0) + estimated_cost_usd
                next_tokens = int(bucket["reserved_tokens"] or 0) + estimated_tokens

                if plan.daily_token_limit and next_tokens > plan.daily_token_limit:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Daily token limit reached for this customer.",
                    )

                if next_cost > daily_budget:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Daily money budget reached for this customer.",
                    )

                db.execute(
                    """
                    INSERT INTO customer_usage (
                        day, token_hash, requests, reserved_cost_usd, reserved_tokens
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day, token_hash) DO UPDATE SET
                        requests = excluded.requests,
                        reserved_cost_usd = excluded.reserved_cost_usd,
                        reserved_tokens = excluded.reserved_tokens
                    """,
                    (
                        day,
                        plan.token_hash,
                        int(bucket["requests"] or 0) + 1,
                        round(next_cost, 8),
                        next_tokens,
                    ),
                )
                db.commit()

        return CustomerReservation(
            token_hash=plan.token_hash,
            date=day,
            estimated_cost_usd=estimated_cost_usd,
            estimated_tokens=estimated_tokens,
        )

    def rollback(self, reservation: CustomerReservation | None) -> None:
        self.settle(reservation, actual_tokens=0, actual_cost_usd=0.0, decrement_request=True)

    def settle(
        self,
        reservation: CustomerReservation | None,
        *,
        actual_tokens: int,
        actual_cost_usd: float,
        decrement_request: bool = False,
    ) -> None:
        if reservation is None:
            return
        with self._lock:
            with self._connect() as db:
                bucket = self._bucket(db, reservation.date, reservation.token_hash)
                request_delta = 1 if decrement_request else 0
                db.execute(
                    """
                    INSERT INTO customer_usage (
                        day, token_hash, requests, reserved_cost_usd, reserved_tokens
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(day, token_hash) DO UPDATE SET
                        requests = excluded.requests,
                        reserved_cost_usd = excluded.reserved_cost_usd,
                        reserved_tokens = excluded.reserved_tokens
                    """,
                    (
                        reservation.date,
                        reservation.token_hash,
                        max(0, int(bucket["requests"] or 0) - request_delta),
                        round(
                            max(
                                0.0,
                                float(bucket["reserved_cost_usd"] or 0)
                                - reservation.estimated_cost_usd
                                + max(0.0, actual_cost_usd),
                            ),
                            8,
                        ),
                        max(
                            0,
                            int(bucket["reserved_tokens"] or 0)
                            - reservation.estimated_tokens
                            + max(0, actual_tokens),
                        ),
                    ),
                )
                db.commit()

    def snapshot_for(self, plan: CustomerPlan) -> dict[str, Any]:
        daily_budget = daily_cost_budget_usd(plan, self.settings)
        day = _today()
        with self._lock:
            with self._connect() as db:
                bucket = self._bucket(db, day, plan.token_hash)
                spent = float(bucket["reserved_cost_usd"] or 0)
                tokens = int(bucket["reserved_tokens"] or 0)
                requests = int(bucket["requests"] or 0)

        return {
            "customer": {
                "name": plan.name,
                "allowed_model": plan.allowed_model,
                "monthly_price_brl": plan.monthly_price_brl,
                "daily_token_limit": plan.daily_token_limit,
                "active": plan.active,
            },
            "today": {
                "date": day,
                "requests": requests,
                "reserved_cost_usd": round(spent, 8),
                "daily_cost_budget_usd": round(daily_budget, 8),
                "reserved_tokens": tokens,
                "remaining_cost_usd": round(max(0.0, daily_budget - spent), 8),
                "remaining_tokens": (
                    max(0, plan.daily_token_limit - tokens) if plan.daily_token_limit else None
                ),
            },
        }

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_usage (
                    day TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    reserved_cost_usd REAL NOT NULL DEFAULT 0,
                    reserved_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, token_hash)
                )
                """
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _bucket(self, db: sqlite3.Connection, day: str, token_hash: str) -> sqlite3.Row:
        row = db.execute(
            """
            SELECT requests, reserved_cost_usd, reserved_tokens
              FROM customer_usage
             WHERE day = ? AND token_hash = ?
            """,
            (day, token_hash),
        ).fetchone()
        if row:
            return row
        db.execute(
            """
            INSERT OR IGNORE INTO customer_usage (
                day, token_hash, requests, reserved_cost_usd, reserved_tokens
            ) VALUES (?, ?, 0, 0, 0)
            """,
            (day, token_hash),
        )
        return db.execute(
            """
            SELECT requests, reserved_cost_usd, reserved_tokens
              FROM customer_usage
             WHERE day = ? AND token_hash = ?
            """,
            (day, token_hash),
        ).fetchone()


def daily_cost_budget_usd(plan: CustomerPlan, settings: Settings) -> float:
    if plan.monthly_price_brl <= 0 and plan.daily_token_limit > 0:
        price = CostPolicy(max_ratio_vs_claude=settings.max_cost_ratio_vs_claude).prices.get(
            settings.cheap_code_agent
        )
        token_price = (price.prompt + price.completion) if price else 0.000000336
        return max(0.00000001, plan.daily_token_limit * token_price * settings.cost_reserve_multiplier)
    margin = min(max(settings.customer_profit_margin, 0.50), 0.95)
    exchange = max(0.01, settings.usd_to_brl)
    return max(0.0, plan.monthly_price_brl * (1 - margin) / exchange / 30)


def estimate_request_tokens(payload: dict[str, Any], settings: Settings) -> int:
    input_text = extract_prompt_text(payload)
    input_tokens = math.ceil(len(input_text) / 3.8) + 24
    return max(1, input_tokens) + _max_tokens(payload, settings)


def estimate_request_cost_usd(
    payload: dict[str, Any],
    decision: RouteDecision,
    settings: Settings,
    *,
    estimated_tokens: int | None = None,
) -> float:
    tokens = estimated_tokens or estimate_request_tokens(payload, settings)
    effective_path = decision.cost_estimate["effective_path"]
    ratio = float(effective_path["cost_ratio_vs_claude"])
    baseline = CostPolicy(max_ratio_vs_claude=settings.max_cost_ratio_vs_claude).prices[
        CLAUDE_BASELINE_MODEL
    ]
    blended_baseline = baseline.prompt + baseline.completion
    reserve = tokens * blended_baseline * ratio * settings.cost_reserve_multiplier
    return max(0.00000001, reserve)


def _max_tokens(payload: dict[str, Any], settings: Settings) -> int:
    try:
        requested = int(payload.get("max_tokens") or settings.max_request_output_tokens)
    except (TypeError, ValueError):
        requested = settings.max_request_output_tokens
    return max(1, min(requested, settings.max_request_output_tokens))


def _today() -> str:
    return datetime.now(UTC).date().isoformat()
