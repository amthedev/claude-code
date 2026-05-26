from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPrice:
    prompt: float
    completion: float


@dataclass(frozen=True, slots=True)
class CostEstimate:
    model: str
    cost_ratio_vs_claude: float
    savings_vs_claude: float
    within_budget: bool
    note: str

    def to_dict(self) -> dict[str, float | bool | str]:
        return {
            "model": self.model,
            "cost_ratio_vs_claude": round(self.cost_ratio_vs_claude, 4),
            "savings_vs_claude": round(self.savings_vs_claude, 4),
            "within_budget": self.within_budget,
            "note": self.note,
        }


# OpenRouter price strings are dollars per token. These defaults were checked against
# OpenRouter's public models API on 2026-05-26.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "anthropic/claude-opus-4.7": ModelPrice(prompt=0.000005, completion=0.000025),
    "anthropic/claude-opus-4.7-fast": ModelPrice(prompt=0.00003, completion=0.00015),
    "anthropic/claude-sonnet-4.6": ModelPrice(prompt=0.000003, completion=0.000015),
    "deepseek/deepseek-v4-pro": ModelPrice(prompt=0.000000435, completion=0.00000087),
    "deepseek/deepseek-v4-flash": ModelPrice(prompt=0.0000001, completion=0.0000002),
    "deepseek/deepseek-r1": ModelPrice(prompt=0.0000007, completion=0.0000025),
    "google/gemini-2.5-flash-lite": ModelPrice(prompt=0.0000001, completion=0.0000004),
    "moonshotai/kimi-k2.6": ModelPrice(prompt=0.00000073, completion=0.00000349),
    "qwen/qwen3.6-flash": ModelPrice(prompt=0.0000001875, completion=0.000001125),
    "qwen/qwen3-235b-a22b-thinking-2507": ModelPrice(
        prompt=0.0000001495,
        completion=0.000001495,
    ),
    "qwen/qwen3-coder-next": ModelPrice(prompt=0.00000011, completion=0.0000008),
    "qwen/qwen3-coder-30b-a3b-instruct": ModelPrice(prompt=0.00000007, completion=0.00000027),
    "qwen/qwen3-coder-flash": ModelPrice(prompt=0.000000195, completion=0.000000975),
    "stepfun/step-3.5-flash": ModelPrice(prompt=0.0000001, completion=0.0000003),
    "tencent/hy3-preview": ModelPrice(prompt=0.000000066, completion=0.00000026),
}

CLAUDE_BASELINE_MODEL = "anthropic/claude-opus-4.7"


class CostPolicy:
    def __init__(
        self,
        *,
        max_ratio_vs_claude: float,
        prices: dict[str, ModelPrice] | None = None,
        baseline_model: str = CLAUDE_BASELINE_MODEL,
    ) -> None:
        self.max_ratio_vs_claude = max(0.01, max_ratio_vs_claude)
        self.prices = prices or DEFAULT_PRICES
        self.baseline_model = baseline_model

    def estimate(self, model: str) -> CostEstimate:
        price = self.prices.get(model)
        baseline = self.prices[CLAUDE_BASELINE_MODEL]
        if price is None:
            return CostEstimate(
                model=model,
                cost_ratio_vs_claude=1.0,
                savings_vs_claude=0.0,
                within_budget=False,
                note="unknown model price; treating as over budget",
            )

        # Coding workloads often have large prompts and sizable completions. A 1:1
        # blended ratio keeps the guard simple and conservative enough for routing.
        model_blended = price.prompt + price.completion
        baseline_blended = baseline.prompt + baseline.completion
        ratio = model_blended / baseline_blended
        savings = max(0.0, 1.0 - ratio)
        return CostEstimate(
            model=model,
            cost_ratio_vs_claude=ratio,
            savings_vs_claude=savings,
            within_budget=ratio <= self.max_ratio_vs_claude,
            note="priced against Claude Opus 4.7 blended input/output cost",
        )

    def estimate_pipeline(self, label: str, models: list[str]) -> CostEstimate:
        estimates = [self.estimate(model) for model in models]
        ratio = sum(estimate.cost_ratio_vs_claude for estimate in estimates)
        savings = max(0.0, 1.0 - ratio)
        unknowns = [estimate.model for estimate in estimates if not estimate.within_budget]
        if unknowns:
            note = f"pipeline includes over-budget or unknown model(s): {', '.join(unknowns)}"
        else:
            note = "conservative sum of each internal call against one Claude Opus 4.7 call"
        return CostEstimate(
            model=label,
            cost_ratio_vs_claude=ratio,
            savings_vs_claude=savings,
            within_budget=ratio <= self.max_ratio_vs_claude,
            note=note,
        )

    def cheapest_allowed(self, candidates: list[str]) -> str:
        allowed = [candidate for candidate in candidates if self.estimate(candidate).within_budget]
        if not allowed:
            return candidates[0]
        return min(allowed, key=lambda model: self._blended_price(model))

    def strongest_allowed(self, candidates: list[str]) -> str:
        for candidate in candidates:
            if self.estimate(candidate).within_budget:
                return candidate
        return self.cheapest_allowed(candidates)

    def _blended_price(self, model: str) -> float:
        price = self.prices.get(model)
        if price is None:
            return float("inf")
        return price.prompt + price.completion
