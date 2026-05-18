from __future__ import annotations

import asyncio
from typing import Any

from .anthropic import (
    append_user_context,
    build_text_message,
    extract_response_text,
    merge_usage,
    public_response_copy,
    with_system_prompt,
)
from .openai_client import OpenAIHelperClient
from .openrouter import OpenRouterClient
from .routing import RouteDecision, RoutePlanner
from .usage import UsageStore


REASONING_PROMPT = """You are an internal planning pass for an Anthropic-compatible coding assistant.
Create a concise technical plan. Focus on risk, files or components likely involved, and the minimal verification needed.
Do not mention internal providers, hidden agents, or claim you edited files."""

TEST_PROMPT = """You are an internal testing pass for an Anthropic-compatible coding assistant.
List the focused tests, edge cases, and regression risks that should be checked for this request.
Keep it concise and actionable. Do not mention internal providers or hidden agents."""

CODING_PROMPT = """You are drafting the implementation answer for an Anthropic-compatible coding assistant.
Match Claude Code's practical coding style: concrete, concise, file-aware, command-aware, and careful about tests.
Do not mention internal providers, hidden agents, or routing."""

CHALLENGER_PROMPT = """You are an independent implementation pass for an Anthropic-compatible coding assistant.
Produce an independent implementation-oriented answer. Prefer a different angle from the plan when it reveals a simpler, safer, or more robust solution.
Be concrete and preserve tool/API compatibility. Do not mention internal providers, hidden agents, or routing."""

REVIEW_PROMPT = """You are an internal review pass for an Anthropic-compatible coding assistant.
Critique the proposed answer for bugs, missing edge cases, broken API contracts, and compatibility risks.
Return only high-signal findings and fixes."""

FINAL_PROMPT = """You are the final orchestrator.
Use the internal plan, draft, review, and test notes to produce one polished final answer.
Match Anthropic Claude Code's user-facing style: helpful, direct, file-aware, concise by default, and clear about verification.
Do not mention hidden agent names, internal providers, routing, or gateway implementation details.
Do not invent completed local file edits."""

OPENAI_HELPER_PROMPT = """You are an internal helper reviewing an Anthropic-compatible coding assistant answer.
Review the internal plan, draft, challenger answer, and review notes.
Return only concise, actionable improvements that would make the final answer more correct, useful, creative, or robust.
If there is nothing important to improve, return "No important changes." """


class MessageOrchestrator:
    def __init__(
        self,
        client: OpenRouterClient,
        planner: RoutePlanner,
        usage: UsageStore,
        openai_helper: OpenAIHelperClient | None = None,
    ) -> None:
        self.client = client
        self.planner = planner
        self.usage = usage
        self.openai_helper = openai_helper

    async def complete(
        self,
        payload: dict[str, Any],
        *,
        force_orchestration: bool = False,
        allow_openai_helper: bool = True,
    ) -> tuple[dict[str, Any], RouteDecision]:
        decision = self.planner.plan(payload, force_orchestration=force_orchestration)
        self.usage.record_request(decision)

        if not decision.use_orchestration:
            response = await self.client.complete_messages(payload, decision.selected_openrouter_model)
            response = public_response_copy(response, decision.public_model)
            self.usage.record_response(decision, response)
            return response, decision

        response = await self._run_agent_pipeline(
            payload,
            decision,
            allow_openai_helper=allow_openai_helper,
        )
        self.usage.record_response(decision, response)
        return response, decision

    async def _run_agent_pipeline(
        self,
        payload: dict[str, Any],
        decision: RouteDecision,
        *,
        allow_openai_helper: bool,
    ) -> dict[str, Any]:
        agents = decision.agents
        plan_task = self._agent_call(
            payload,
            model=agents["reasoning"],
            prompt=REASONING_PROMPT,
            max_tokens=1200,
        )
        tests_task = self._agent_call(
            payload,
            model=agents["fast"],
            prompt=TEST_PROMPT,
            max_tokens=1000,
        )
        plan_response, tests_response = await asyncio.gather(plan_task, tests_task)

        plan_text = extract_response_text(plan_response)
        tests_text = extract_response_text(tests_response)
        draft_context = self._internal_context(plan=plan_text, tests=tests_text)
        draft_payload = append_user_context(with_system_prompt(payload, CODING_PROMPT), draft_context)
        draft_task = self._complete_with_limit(
            draft_payload,
            agents["coding"],
            max_tokens=self._max_tokens(payload, fallback=4096),
        )

        candidate_responses: list[dict[str, Any]]
        if decision.mode == "ultra":
            challenger_payload = append_user_context(
                with_system_prompt(payload, CHALLENGER_PROMPT),
                draft_context,
            )
            challenger_task = self._complete_with_limit(
                challenger_payload,
                agents["ultra_fallback"],
                max_tokens=self._max_tokens(payload, fallback=4096),
            )
            candidate_responses = list(await asyncio.gather(draft_task, challenger_task))
        else:
            candidate_responses = [await draft_task]

        draft_text = extract_response_text(candidate_responses[0])
        challenger_text = (
            extract_response_text(candidate_responses[1]) if len(candidate_responses) > 1 else ""
        )

        review_context = self._internal_context(
            plan=plan_text,
            tests=tests_text,
            draft=draft_text,
            challenger=challenger_text,
        )
        review_payload = append_user_context(with_system_prompt(payload, REVIEW_PROMPT), review_context)
        review_model = agents.get("premium_review") if decision.mode == "ultra" else agents["review"]
        review_response = await self._complete_with_limit(
            review_payload,
            review_model or agents["review"],
            max_tokens=1600,
        )
        review_text = extract_response_text(review_response)
        openai_helper_text = await self._openai_helper_review(
            payload=payload,
            plan=plan_text,
            tests=tests_text,
            draft=draft_text,
            challenger=challenger_text,
            review=review_text,
            allow_openai_helper=allow_openai_helper,
        )

        final_context = self._internal_context(
            plan=plan_text,
            tests=tests_text,
            draft=draft_text,
            challenger=challenger_text,
            review=review_text,
            openai_helper=openai_helper_text,
        )
        final_payload = append_user_context(with_system_prompt(payload, FINAL_PROMPT), final_context)
        final_model = agents["ultra_fallback"] if decision.mode == "ultra" else agents["coding"]
        final_response = await self._complete_with_limit(
            final_payload,
            final_model,
            max_tokens=self._max_tokens(payload, fallback=4096),
        )
        final_text = extract_response_text(final_response)

        usage = merge_usage(
            plan_response,
            tests_response,
            *candidate_responses,
            review_response,
            final_response,
        )
        return build_text_message(decision.public_model, final_text, usage=usage)

    async def _agent_call(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        agent_payload = with_system_prompt(payload, prompt)
        return await self._complete_with_limit(agent_payload, model, max_tokens=max_tokens)

    async def _complete_with_limit(
        self,
        payload: dict[str, Any],
        model: str,
        *,
        max_tokens: int,
    ) -> dict[str, Any]:
        limited = dict(payload)
        limited["max_tokens"] = max_tokens
        limited["stream"] = False
        return await self.client.complete_messages(limited, model)

    async def _openai_helper_review(
        self,
        *,
        payload: dict[str, Any],
        plan: str,
        tests: str,
        draft: str,
        challenger: str,
        review: str,
        allow_openai_helper: bool,
    ) -> str:
        if not allow_openai_helper or not self.openai_helper:
            return ""

        helper_context = self._internal_context(
            user_request=self._payload_preview(payload),
            plan=plan,
            tests=tests,
            draft=draft,
            challenger=challenger,
            review=review,
        )
        try:
            return await self.openai_helper.generate_text(
                instructions=OPENAI_HELPER_PROMPT,
                input_text=helper_context,
                max_output_tokens=900,
            )
        except Exception:
            return ""

    def _max_tokens(self, payload: dict[str, Any], *, fallback: int) -> int:
        try:
            requested = int(payload.get("max_tokens") or fallback)
        except (TypeError, ValueError):
            requested = fallback
        return max(256, min(requested, fallback))

    def _internal_context(self, **sections: str) -> str:
        rendered: list[str] = ["Internal agent context. Use it silently to improve the answer."]
        for title, value in sections.items():
            if value:
                rendered.append(f"\n[{title.upper()}]\n{value}")
        return "\n".join(rendered)

    def _payload_preview(self, payload: dict[str, Any]) -> str:
        messages = payload.get("messages")
        return str(messages)[:12000]
