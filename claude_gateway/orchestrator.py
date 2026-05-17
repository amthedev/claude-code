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
from .openrouter import OpenRouterClient
from .routing import RouteDecision, RoutePlanner
from .usage import UsageStore


REASONING_PROMPT = """You are the reasoning agent in a coding gateway.
Create a concise technical plan. Focus on risk, files or components likely involved, and the minimal verification needed.
Do not claim you edited files."""

TEST_PROMPT = """You are the testing agent in a coding gateway.
List the focused tests, edge cases, and regression risks that should be checked for this request.
Keep it concise and actionable."""

CODING_PROMPT = """You are the coding agent in a coding gateway.
Produce the best implementation-oriented answer for the user. Be concrete, concise, and preserve tool/API compatibility."""

CHALLENGER_PROMPT = """You are the challenger coding agent in a cost-controlled coding gateway.
Produce an independent implementation-oriented answer. Prefer a different angle from the plan when it reveals a simpler, safer, or more robust solution.
Be concrete and preserve tool/API compatibility."""

REVIEW_PROMPT = """You are the review agent in a coding gateway.
Critique the proposed answer for bugs, missing edge cases, broken API contracts, cost issues, and compatibility risks.
Return only high-signal findings and fixes."""

FINAL_PROMPT = """You are the final orchestrator.
Use the internal plan, draft, review, and test notes to produce one polished final answer.
Do not mention hidden agent names unless directly useful. Do not invent completed local file edits."""


class MessageOrchestrator:
    def __init__(
        self,
        client: OpenRouterClient,
        planner: RoutePlanner,
        usage: UsageStore,
    ) -> None:
        self.client = client
        self.planner = planner
        self.usage = usage

    async def complete(
        self,
        payload: dict[str, Any],
        *,
        force_orchestration: bool = False,
    ) -> tuple[dict[str, Any], RouteDecision]:
        decision = self.planner.plan(payload, force_orchestration=force_orchestration)
        self.usage.record_request(decision)

        if not decision.use_orchestration:
            response = await self.client.complete_messages(payload, decision.selected_openrouter_model)
            response = public_response_copy(response, decision.public_model)
            self.usage.record_response(decision, response)
            return response, decision

        response = await self._run_agent_pipeline(payload, decision)
        self.usage.record_response(decision, response)
        return response, decision

    async def _run_agent_pipeline(
        self,
        payload: dict[str, Any],
        decision: RouteDecision,
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

        final_context = self._internal_context(
            plan=plan_text,
            tests=tests_text,
            draft=draft_text,
            challenger=challenger_text,
            review=review_text,
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
