from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .routing import RouteDecision


@dataclass
class UsageBucket:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    modes: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "modes": dict(self.modes),
        }


class UsageStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._total = UsageBucket()
        self._by_model: dict[str, UsageBucket] = defaultdict(UsageBucket)

    def record_request(self, decision: RouteDecision) -> None:
        with self._lock:
            self._total.requests += 1
            self._total.modes[decision.mode] += 1
            bucket = self._by_model[decision.selected_openrouter_model]
            bucket.requests += 1
            bucket.modes[decision.mode] += 1

    def record_response(self, decision: RouteDecision, response: dict[str, Any]) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return

        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        with self._lock:
            self._total.input_tokens += input_tokens
            self._total.output_tokens += output_tokens
            bucket = self._by_model[decision.selected_openrouter_model]
            bucket.input_tokens += input_tokens
            bucket.output_tokens += output_tokens

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self._total.to_dict(),
                "by_model": {model: bucket.to_dict() for model, bucket in self._by_model.items()},
            }
