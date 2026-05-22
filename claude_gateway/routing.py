from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .budget import CostPolicy
from .config import Settings


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: str
    display_name: str
    mode: str
    description: str


@dataclass(frozen=True, slots=True)
class RouteDecision:
    requested_model: str
    public_model: str
    selected_openrouter_model: str
    mode: str
    task_type: str
    complexity: str
    use_orchestration: bool
    reason: str
    agents: dict[str, str]
    cost_estimate: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FRONTEND_KEYWORDS = {
    "frontend",
    "front-end",
    "ui",
    "ux",
    "react",
    "vue",
    "svelte",
    "css",
    "tailwind",
    "dashboard",
    "landing",
    "component",
    "figma",
    "screen",
    "layout",
}

FILE_EDIT_KEYWORDS = {
    "apply_patch",
    "alterar arquivo",
    "alterar arquivos",
    "commit",
    "diff",
    "edit file",
    "editar arquivo",
    "edite o arquivo",
    "mexer no arquivo",
    "mexer nos arquivos",
    "modifique o arquivo",
    "patch",
    "shell",
    "terminal",
    "workspace",
    "write file",
}

DEBUG_KEYWORDS = {
    "bug",
    "debug",
    "traceback",
    "stack trace",
    "failing",
    "falha",
    "erro",
    "error",
    "exception",
    "test failed",
    "corrija",
    "fix",
}

TEST_KEYWORDS = {
    "ci",
    "coverage",
    "jest",
    "playwright",
    "pytest",
    "regressao",
    "regressão",
    "regression",
    "test",
    "teste",
    "testes",
    "tests",
    "unittest",
    "vitest",
}

REVIEW_KEYWORDS = {"review", "revis", "audit", "security", "risco", "vulnerability"}

ARCHITECTURE_KEYWORDS = {
    "analisar",
    "analise",
    "analyze",
    "architecture",
    "arquitetura",
    "codebase",
    "refactor",
    "refator",
    "migration",
    "migrate",
    "database",
    "projeto",
    "project",
    "repository",
    "repo",
    "schema",
    "multi-file",
}

LOW_COMPLEXITY_KEYWORDS = {
    "explain",
    "explique",
    "resuma",
    "summarize",
    "small",
    "simples",
    "one file",
    "typo",
}

HIGH_COMPLEXITY_KEYWORDS = {
    "critical",
    "crítico",
    "production",
    "pagamento",
    "payment",
    "auth",
    "race condition",
    "concorr",
    "security",
    "large codebase",
    "vários arquivos",
    "multiple files",
}


def model_profiles(settings: Settings) -> list[ModelProfile]:
    return [
        ModelProfile(
            id=settings.economy_public_model,
            display_name="Frontier AI Economy",
            mode="economy",
            description="Caminho econômico para conversas e tarefas simples.",
        ),
        ModelProfile(
            id=settings.pro_public_model,
            display_name="Frontier AI Pro",
            mode="pro",
            description="Mais força para código, análise e trabalho diário.",
        ),
        ModelProfile(
            id=settings.ultra_public_model,
            display_name="Frontier AI Ultra",
            mode="ultra",
            description="Rota reforçada para tarefas críticas e projetos maiores.",
        ),
        ModelProfile(
            id=settings.ui_public_model,
            display_name="Frontier AI UI",
            mode="ui",
            description="Especialista em frontend, layout e experiência visual.",
        ),
        ModelProfile(
            id=settings.auto_public_model,
            display_name="Frontier AI Auto",
            mode="auto",
            description="Escolhe automaticamente entre Economy, Pro, Ultra e UI.",
        ),
    ]


def extract_prompt_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []

    system = payload.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(_content_to_text(system))

    for message in payload.get("messages", []):
        if isinstance(message, dict):
            parts.extend(_content_to_text(message.get("content")))

    return "\n".join(part for part in parts if part).lower()


def payload_has_tool_contract(payload: dict[str, Any]) -> bool:
    if payload.get("tools") or payload.get("tool_choice"):
        return True

    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        for block in _iter_content_blocks(message.get("content")):
            block_type = block.get("type")
            if block_type in {"tool_use", "tool_result"}:
                return True

    return False


def _content_to_text(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    text: list[str] = []
    for block in _iter_content_blocks(content):
        value = block.get("text")
        if isinstance(value, str):
            text.append(value)
    return text


def _iter_content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


class RoutePlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cost_policy = CostPolicy(max_ratio_vs_claude=settings.max_cost_ratio_vs_claude)

    def plan(self, payload: dict[str, Any], *, force_orchestration: bool = False) -> RouteDecision:
        requested_model = str(payload.get("model") or self.settings.auto_public_model)
        task_text = extract_prompt_text(payload)
        task_type = self._task_type(task_text)
        complexity = self._complexity(task_text)

        mode = self._mode_for_requested_model(requested_model, task_type, complexity)
        public_model = self._public_model_for_mode(mode)
        selected_model = self._openrouter_model_for_mode(mode, task_type, complexity)
        external_model_requested = "/" in requested_model and requested_model not in self._public_ids()
        direct_external = external_model_requested and self.settings.allow_direct_external_models
        if direct_external:
            public_model = requested_model
            selected_model = requested_model
            mode = "direct"
        else:
            selected_model = self._budget_safe_model(selected_model, mode, task_type)

        agents = self._agents_for_mode(mode, task_type, complexity)
        selected_cost = self.cost_policy.estimate(selected_model)
        pipeline_cost = self.cost_policy.estimate_pipeline(
            f"{mode}-pipeline",
            self._pipeline_models(mode, agents, selected_model),
        )
        has_tool_contract = payload_has_tool_contract(payload)
        is_streaming = bool(payload.get("stream"))
        deep_stream_request = is_streaming and self._needs_deep_reasoning(task_type, complexity)
        can_orchestrate = (
            self.settings.enable_agent_orchestration
            and not has_tool_contract
            and mode in {"pro", "ultra", "ui"}
            and pipeline_cost.within_budget
            and (not is_streaming or deep_stream_request)
        )
        use_orchestration = can_orchestrate or (
            force_orchestration and mode in {"pro", "ultra", "ui"} and pipeline_cost.within_budget
        )
        if direct_external and not force_orchestration:
            use_orchestration = False
        effective_cost = pipeline_cost if use_orchestration else selected_cost

        reason = self._reason(mode, task_type, complexity, is_streaming, has_tool_contract)
        return RouteDecision(
            requested_model=requested_model,
            public_model=public_model,
            selected_openrouter_model=selected_model,
            mode=mode,
            task_type=task_type,
            complexity=complexity,
            use_orchestration=use_orchestration,
            reason=reason,
            agents=agents,
            cost_estimate={
                "selected_model": selected_cost.to_dict(),
                "effective_path": effective_cost.to_dict(),
                "pipeline": pipeline_cost.to_dict(),
            },
        )

    def _public_ids(self) -> set[str]:
        return {profile.id for profile in model_profiles(self.settings)}

    def _mode_for_requested_model(self, requested_model: str, task_type: str, complexity: str) -> str:
        public_to_mode = {profile.id: profile.mode for profile in model_profiles(self.settings)}
        explicit_mode = public_to_mode.get(requested_model)
        if explicit_mode and explicit_mode != "auto":
            return explicit_mode

        lower = requested_model.lower()
        if "haiku" in lower:
            return "economy"
        if "sonnet" in lower:
            return "pro"
        if "opus" in lower:
            return "ultra"

        if task_type == "frontend":
            return "ui"
        if task_type in {"file_edit", "testing", "debugging"} and complexity != "critical":
            return "pro"
        if complexity == "low" or task_type == "explanation":
            return "economy"
        if complexity == "critical":
            return "ultra"
        return "pro"

    def _public_model_for_mode(self, mode: str) -> str:
        return {
            "economy": self.settings.economy_public_model,
            "pro": self.settings.pro_public_model,
            "ultra": self.settings.ultra_public_model,
            "ui": self.settings.ui_public_model,
            "auto": self.settings.auto_public_model,
        }.get(mode, self.settings.pro_public_model)

    def _openrouter_model_for_mode(self, mode: str, task_type: str, complexity: str) -> str:
        if mode == "economy":
            return self.settings.cheap_code_agent
        if task_type == "frontend" and complexity == "low":
            return self.settings.frontend_fix_agent
        if mode == "ui" or task_type == "frontend":
            return self.settings.ui_agent
        if task_type in {"architecture", "review"} and mode == "ultra":
            return self.cost_policy.strongest_allowed(
                [
                    self.settings.project_reasoning_agent,
                    self.settings.reasoning_agent,
                    self.settings.backend_partner_agent,
                    self.settings.code_agent,
                    self.settings.cheap_code_agent,
                ]
            )
        if task_type == "file_edit":
            return self.settings.code_agent
        if task_type == "testing":
            return self.settings.reasoning_agent
        if mode == "ultra" and complexity == "critical":
            return self.cost_policy.strongest_allowed(
                [
                    self.settings.deep_reasoning_agent,
                    self.settings.project_reasoning_agent,
                    self.settings.reasoning_agent,
                    self.settings.code_agent,
                    self.settings.backend_partner_agent,
                    self.settings.cheap_code_agent,
                ]
            )
        return self.settings.code_agent

    def _agents_for_mode(self, mode: str, task_type: str, complexity: str) -> dict[str, str]:
        agents = {
            "router": self.settings.router_agent,
            "fast": self.settings.fast_agent,
            "reasoning": self.settings.reasoning_agent,
            "coding": self.settings.code_agent,
            "review": self.settings.backend_partner_agent,
            "ui": self.settings.ui_agent,
            "frontend_reasoning": self.settings.frontend_reasoning_agent,
            "frontend_fix": self.settings.frontend_fix_agent,
            "backend_partner": self.settings.backend_partner_agent,
            "project_reasoning": self.settings.project_reasoning_agent,
            "deep_reasoning": self.settings.deep_reasoning_agent,
            "gemini_code_helper": self.settings.gemini_code_helper_agent,
        }
        if mode == "economy":
            agents["coding"] = self.settings.cheap_code_agent
            agents["review"] = self.settings.cheap_code_agent
        if mode == "ui":
            agents["reasoning"] = self.settings.frontend_reasoning_agent
            agents["coding"] = (
                self.settings.frontend_fix_agent
                if complexity == "low"
                else self.settings.frontend_coder_agent
            )
            agents["review"] = self.settings.reasoning_agent
            agents["premium_review"] = self.settings.backend_partner_agent
        if task_type == "frontend":
            agents["reasoning"] = self.settings.frontend_reasoning_agent
            agents["coding"] = (
                self.settings.frontend_fix_agent
                if complexity == "low"
                else self.settings.frontend_coder_agent
            )
            agents["review"] = self.settings.reasoning_agent
        if task_type in {"architecture", "review"}:
            agents["reasoning"] = self.settings.project_reasoning_agent
            agents["coding"] = self.settings.backend_partner_agent
            agents["review"] = self.settings.reasoning_agent
        if mode == "ultra":
            if complexity == "critical":
                agents["reasoning"] = self._premium_agent_or_budget_safe(self.settings.deep_reasoning_agent)
                agents["ultra_fallback"] = self._premium_agent_or_budget_safe(
                    self.settings.deep_reasoning_agent
                )
                premium_review = self._premium_agent_or_budget_safe(
                    self.settings.project_reasoning_agent
                )
            else:
                if task_type == "frontend":
                    agents["reasoning"] = self.settings.frontend_reasoning_agent
                    agents["ultra_fallback"] = self._premium_agent_or_budget_safe(
                        self.settings.frontend_coder_agent
                    )
                    premium_review = self._premium_agent_or_budget_safe(self.settings.reasoning_agent)
                else:
                    agents["reasoning"] = self._premium_agent_or_budget_safe(
                        self.settings.project_reasoning_agent
                    )
                    agents["ultra_fallback"] = self._premium_agent_or_budget_safe(
                        self.settings.ultra_fallback
                    )
                    premium_review = self._premium_agent_or_budget_safe(self.settings.premium_fallback)
            agents["premium_review"] = premium_review
        return agents

    def _budget_safe_model(self, model: str, mode: str, task_type: str) -> str:
        if self.cost_policy.estimate(model).within_budget:
            return model

        candidates = [
            self.settings.code_agent,
            self.settings.backend_partner_agent,
            self.settings.frontend_coder_agent,
            self.settings.reasoning_agent,
            self.settings.cheap_code_agent,
            self.settings.fast_agent,
        ]
        if mode == "ui" or task_type == "frontend":
            candidates = [
                self.settings.frontend_coder_agent,
                self.settings.frontend_reasoning_agent,
                self.settings.reasoning_agent,
                self.settings.cheap_code_agent,
                self.settings.fast_agent,
            ]
        if mode == "economy":
            candidates = [self.settings.cheap_code_agent, self.settings.fast_agent]
        return self.cost_policy.strongest_allowed(candidates)

    def _premium_agent_or_budget_safe(self, model: str) -> str:
        if self.cost_policy.estimate(model).within_budget:
            return model
        return self.cost_policy.strongest_allowed(
            [
                self.settings.code_agent,
                self.settings.backend_partner_agent,
                self.settings.frontend_coder_agent,
                self.settings.project_reasoning_agent,
                self.settings.reasoning_agent,
                self.settings.cheap_code_agent,
            ]
        )

    def _pipeline_models(self, mode: str, agents: dict[str, str], selected_model: str) -> list[str]:
        if mode == "ultra":
            return [
                agents["reasoning"],
                agents["fast"],
                agents["gemini_code_helper"],
                agents["coding"],
                agents["ultra_fallback"],
                agents["premium_review"],
                agents["ultra_fallback"],
            ]
        if mode in {"pro", "ui"}:
            return [
                agents["reasoning"],
                agents["fast"],
                agents["gemini_code_helper"],
                agents["coding"],
                agents["review"],
                agents["coding"],
            ]
        return [selected_model]

    def _task_type(self, task_text: str) -> str:
        if _contains_any(task_text, FILE_EDIT_KEYWORDS):
            return "file_edit"
        if _contains_any(task_text, TEST_KEYWORDS):
            return "testing"
        if _contains_any(task_text, FRONTEND_KEYWORDS):
            return "frontend"
        if _contains_any(task_text, REVIEW_KEYWORDS):
            return "review"
        if _contains_any(task_text, DEBUG_KEYWORDS):
            return "debugging"
        if _contains_any(task_text, ARCHITECTURE_KEYWORDS):
            return "architecture"
        if _contains_any(task_text, LOW_COMPLEXITY_KEYWORDS):
            return "explanation"
        return "simple_code"

    def _complexity(self, task_text: str) -> str:
        if _contains_any(task_text, HIGH_COMPLEXITY_KEYWORDS):
            return "critical"
        if _contains_any(task_text, LOW_COMPLEXITY_KEYWORDS) and _contains_any(
            task_text,
            FRONTEND_KEYWORDS,
        ):
            return "low"
        if _contains_any(task_text, ARCHITECTURE_KEYWORDS | DEBUG_KEYWORDS | FILE_EDIT_KEYWORDS):
            return "high"
        if _contains_any(task_text, LOW_COMPLEXITY_KEYWORDS):
            return "low"
        return "medium"

    def _reason(
        self,
        mode: str,
        task_type: str,
        complexity: str,
        is_streaming: bool,
        has_tool_contract: bool,
    ) -> str:
        details = [f"mode={mode}", f"task_type={task_type}", f"complexity={complexity}"]
        if is_streaming:
            if self._needs_deep_reasoning(task_type, complexity):
                details.append("streaming with internal reasoning")
            else:
                details.append("streaming proxy")
        if has_tool_contract:
            details.append("tool contract proxy")
        return ", ".join(details)

    def _needs_deep_reasoning(self, task_type: str, complexity: str) -> bool:
        return complexity in {"high", "critical"} or task_type in {
            "architecture",
            "debugging",
            "file_edit",
            "frontend",
            "review",
            "testing",
        }


def _contains_any(text: str, keywords: set[str]) -> bool:
    return any(_contains_keyword(text, keyword) for keyword in keywords)


def _contains_keyword(text: str, keyword: str) -> bool:
    escaped = re.escape(keyword)
    if keyword.replace("-", "").replace("_", "").isalnum():
        return re.search(rf"(?<![\w-]){escaped}(?![\w-])", text) is not None
    return keyword in text
