from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import HTTPException, status

from .budget import CLAUDE_BASELINE_MODEL, CostPolicy
from .config import Settings
from .routing import RouteDecision, extract_prompt_text


@dataclass(frozen=True, slots=True)
class CustomerPlan:
    token: str
    name: str
    monthly_price_brl: float
    daily_token_limit: int
    allowed_model: str
    active: bool = True

    @property
    def token_hash(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class CustomerReservation:
    token_hash: str
    date: str
    estimated_cost_usd: float
    estimated_tokens: int


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
    limited["model"] = plan.allowed_model
    limited["max_tokens"] = min(_max_tokens(limited, settings), settings.max_request_output_tokens)
    return limited


class CustomerUsageStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.quota_data_file)
        self._lock = Lock()

    def reserve(self, plan: CustomerPlan, payload: dict[str, Any], decision: RouteDecision) -> CustomerReservation:
        if not plan.active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer plan is inactive.")

        if not decision.cost_estimate["effective_path"]["within_budget"]:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Request path is outside the configured cost budget.",
            )

        estimated_tokens = estimate_request_tokens(payload, self.settings)
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
            data = self._read()
            bucket = self._bucket(data, day, plan.token_hash)
            next_cost = float(bucket.get("reserved_cost_usd") or 0) + estimated_cost_usd
            next_tokens = int(bucket.get("reserved_tokens") or 0) + estimated_tokens

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

            bucket["reserved_cost_usd"] = round(next_cost, 8)
            bucket["reserved_tokens"] = next_tokens
            bucket["requests"] = int(bucket.get("requests") or 0) + 1
            self._write(data)

        return CustomerReservation(
            token_hash=plan.token_hash,
            date=day,
            estimated_cost_usd=estimated_cost_usd,
            estimated_tokens=estimated_tokens,
        )

    def rollback(self, reservation: CustomerReservation | None) -> None:
        if reservation is None:
            return

        with self._lock:
            data = self._read()
            bucket = self._bucket(data, reservation.date, reservation.token_hash)
            bucket["reserved_cost_usd"] = round(
                max(0.0, float(bucket.get("reserved_cost_usd") or 0) - reservation.estimated_cost_usd),
                8,
            )
            bucket["reserved_tokens"] = max(
                0,
                int(bucket.get("reserved_tokens") or 0) - reservation.estimated_tokens,
            )
            bucket["requests"] = max(0, int(bucket.get("requests") or 0) - 1)
            self._write(data)

    def snapshot_for(self, plan: CustomerPlan) -> dict[str, Any]:
        daily_budget = daily_cost_budget_usd(plan, self.settings)
        day = _today()
        with self._lock:
            data = self._read()
            bucket = self._bucket(data, day, plan.token_hash)
            spent = float(bucket.get("reserved_cost_usd") or 0)
            tokens = int(bucket.get("reserved_tokens") or 0)
            requests = int(bucket.get("requests") or 0)

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

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Quota data file is corrupted; refusing requests to protect budget.",
            ) from exc
        if not isinstance(data, dict):
            return {}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _bucket(self, data: dict[str, Any], day: str, token_hash: str) -> dict[str, Any]:
        day_bucket = data.setdefault(day, {})
        if not isinstance(day_bucket, dict):
            day_bucket = {}
            data[day] = day_bucket

        bucket = day_bucket.setdefault(
            token_hash,
            {"requests": 0, "reserved_cost_usd": 0.0, "reserved_tokens": 0},
        )
        if not isinstance(bucket, dict):
            bucket = {"requests": 0, "reserved_cost_usd": 0.0, "reserved_tokens": 0}
            day_bucket[token_hash] = bucket
        return bucket


def daily_cost_budget_usd(plan: CustomerPlan, settings: Settings) -> float:
    margin = min(max(settings.customer_profit_margin, 0.0), 0.95)
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
