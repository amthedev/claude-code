from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import base64
import json
import logging
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from unicodedata import normalize
from urllib.parse import quote
from zoneinfo import ZoneInfo

import uvicorn
import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .accounts import AccountStore, AccountUsageReservation, public_trial_status
from .budget import CLAUDE_BASELINE_MODEL, CostPolicy
from .auth import AuthContext, client_ip_for_debug, extract_bearer_token, require_gateway_auth
from .anthropic import build_text_message, clean_model_text
from .benchmark import BENCHMARK_CASES, benchmark_failures, benchmark_payload
from .code_workspaces import CodeWorkspaceStore, github_repo_parts
from .config import Settings, get_settings
from .conversations import ConversationStore
from .customers import (
    CustomerReservation,
    CustomerUsageStore,
    actual_reserved_tokens_from_response,
    clamp_customer_payload,
    estimate_request_cost_usd,
    normalize_reasoning_mode,
)
from .openai_client import OpenAIHelperClient
from .openai_compat import (
    anthropic_to_chat_completion,
    anthropic_to_response,
    chat_to_anthropic,
    responses_to_anthropic,
)
from .model_client import AnthropicModelClient, VPSAnthropicClient, default_model_client
from .openrouter import OpenRouterClient, OpenRouterError
from .orchestrator import MessageOrchestrator
from .research import (
    WebSearchClient,
    WebSearchDecision,
    decide_web_search,
    normalize_web_search_policy,
    web_search_context,
    web_search_unavailable_context,
)
from .routing import RouteDecision, RoutePlanner, extract_prompt_text, model_profiles, payload_has_tool_contract
from .security import (
    InMemoryRateLimiter,
    OperationalLoggingMiddleware,
    SecurityHeadersMiddleware,
    rate_limit_key,
    verify_admin_login,
)
from .skills import render_skill_prompt, select_skills
from .support import SupportStore
from .usage import UsageStore
from .vps_scheduler import VPSScheduleStore, vps_scheduler_loop

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontier"
LATENCY_LOGGER = logging.getLogger("claude_gateway.latency")
CUSTOMER_FORCED_FAST_REQUESTS = 10
FAST_CONTEXT_MAX_CHARS = 36_000
FAST_CONTEXT_MAX_MESSAGES = 16
TOOL_CONTEXT_MAX_MESSAGES = 24
HEAVY_MODE_REQUIRED_TERMS = (
    "auth",
    "autenticacao",
    "autorizacao",
    "critical",
    "critico",
    "crítico",
    "corrupcao",
    "corrupção",
    "data loss",
    "database",
    "deploy",
    "migration",
    "migracao",
    "migração",
    "multiple files",
    "pagamento",
    "payment",
    "producao",
    "produção",
    "production",
    "race condition",
    "seguranca",
    "segurança",
    "security",
    "vazamento",
    "varios arquivos",
    "vários arquivos",
)

OPENAI_DESIGN_DIRECTOR_PROMPT = """You are a concise design director for a coding assistant.
Return only tactical guidance that improves frontend implementation quality before code is written.
Focus on premium SaaS polish, layout hierarchy, responsive behavior, component structure, visual restraint,
copy quality, spacing, states, accessibility, and verification. Do not write full source code. Do not mention
OpenAI, ChatGPT, internal routing, providers, or that another model is helping."""

OPENAI_DECISION_DIRECTOR_PROMPT = """You are a concise decision director for a coding assistant.
Choose the best practical defaults so the assistant can execute instead of asking the user to choose.
Return only actionable decisions, assumptions, and next steps. Prefer proceeding with reversible, conventional,
project-consistent choices. Ask the user only if continuing would require missing credentials, payment, destructive
irreversible action, legal/medical/financial judgment, or a personal preference that materially changes the result.
Do not write full source code. Do not mention OpenAI, ChatGPT, internal routing, providers, or hidden helpers."""

GEMINI_CODE_HELPER_PROMPT = """You are a concise internal coding helper for an Anthropic-compatible coding assistant.
Return implementation guidance that helps write better code: file structure, APIs, component boundaries, edge cases,
small pitfalls, and verification. Prefer concrete decisions over options. Keep it short. Do not mention Gemini,
internal routing, providers, or hidden helpers."""

SUPPORT_ASSISTANT_PROMPT = """Você é o primeiro atendimento de suporte do app Claude em português do Brasil.
Resolva dúvidas simples sobre login, planos, Pix, GitHub, ZIP/pastas, limite de tokens, chat, histórico, suporte e uso geral.
Se a mensagem pedir pessoa humana, "mano", atendente, dono/admin, reembolso/estorno, cobrança indevida, conta invadida,
pagamento aprovado sem liberar plano, dado sensível exposto, ameaça jurídica, ou algo que exija ação manual no banco,
comece a resposta exatamente com "ESCALATE:" e explique em uma frase por que precisa de humano.
Caso contrário, responda diretamente em até 5 frases, com tom calmo e prático. Não invente acesso a dados internos."""

CLAUDE_CODE_SYSTEM_REMINDER_RE = re.compile(r"(?is)<system-reminder>.*?</system-reminder>")
CLAUDE_CODE_SESSION_RE = re.compile(r"(?is)<session>.*?</session>")


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[Settings], AnthropicModelClient] | None = None,
    openrouter_fallback_factory: Callable[[Settings], AnthropicModelClient] | None = None,
    openai_helper_factory: Callable[[Settings], OpenAIHelperClient] | None = None,
    web_search_factory: Callable[[Settings], WebSearchClient] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    openapi_url = "/openapi.json" if resolved_settings.expose_openapi else None
    app = FastAPI(
        title="Claude Code Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=openapi_url,
    )
    app.state.settings = resolved_settings
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.usage = UsageStore()
    app.state.customer_usage = CustomerUsageStore(resolved_settings)
    app.state.account_store = AccountStore(resolved_settings)
    app.state.conversation_store = ConversationStore(resolved_settings)
    app.state.code_workspaces = CodeWorkspaceStore(resolved_settings)
    app.state.support_store = SupportStore(resolved_settings)
    app.state.vps_schedules = VPSScheduleStore(resolved_settings)
    app.state.planner = RoutePlanner(resolved_settings)
    if client_factory is None:
        app.state.model_client = default_model_client(
            resolved_settings,
            primary_factory=VPSAnthropicClient,
            fallback_factory=openrouter_fallback_factory or OpenRouterClient,
        )
    elif openrouter_fallback_factory is not None:
        app.state.model_client = default_model_client(
            resolved_settings,
            primary_factory=client_factory,
            fallback_factory=openrouter_fallback_factory,
        )
    else:
        app.state.model_client = client_factory(resolved_settings)
    app.state.openrouter = app.state.model_client
    helper_factory = openai_helper_factory or OpenAIHelperClient
    app.state.openai_helper = helper_factory(resolved_settings) if resolved_settings.openai_api_key else None
    search_factory = web_search_factory or WebSearchClient
    app.state.web_search = (
        search_factory(resolved_settings)
        if resolved_settings.enable_web_search
        and (resolved_settings.openai_api_key or resolved_settings.openrouter_api_key)
        else None
    )
    app.state.orchestrator = MessageOrchestrator(
        app.state.model_client,
        app.state.planner,
        app.state.usage,
        app.state.openai_helper,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(OperationalLoggingMiddleware)
    if resolved_settings.trusted_hosts and resolved_settings.trusted_hosts != ("*",):
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(resolved_settings.trusted_hosts))
    if resolved_settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "Anthropic-Auth-Token"],
        )
    _mount_frontend(app)

    @app.on_event("startup")
    async def _start_vps_scheduler() -> None:
        if not resolved_settings.runpod_api_key or not resolved_settings.runpod_pod_id:
            app.state.vps_scheduler_task = None
            return
        app.state.vps_scheduler_task = asyncio.create_task(vps_scheduler_loop(app.state.vps_schedules))

    @app.on_event("shutdown")
    async def _stop_vps_scheduler() -> None:
        task = getattr(app.state, "vps_scheduler_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        close_model_client = getattr(app.state.model_client, "aclose", None)
        if close_model_client:
            await close_model_client()
        close_openrouter = getattr(app.state.openrouter, "aclose", None)
        if close_openrouter and app.state.openrouter is not app.state.model_client:
            await close_openrouter()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        if not app.state.settings.expose_detailed_health:
            return {"status": "ok"}
        return _detailed_health(app)

    @app.get("/v1/admin/health")
    async def admin_health(request: Request) -> dict[str, Any]:
        _require_admin(request, app.state.settings)
        return _detailed_health(app)

    @app.post("/v1/admin/benchmark")
    async def admin_benchmark(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        auth = _require_admin(request, app.state.settings)
        if payload is not None and not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return _admin_benchmark(app, auth)

    def _detailed_health(app: FastAPI) -> dict[str, Any]:
        settings = app.state.settings
        return {
            "status": "ok",
            "public_model": _public_model_label(settings.auto_public_model, settings),
            "model_backend_configured": bool(settings.vps_model_base_url and settings.vps_model_id),
            "fast_backend_configured": bool(settings.vps_fast_model_base_url or settings.vps_model_base_url),
            "strong_backend_configured": bool(
                settings.vps_strong_model_base_url and settings.vps_strong_model_id
            ),
            "external_fallback_enabled": settings.openrouter_emergency_fallback,
            "external_fallback_configured": bool(
                settings.openrouter_emergency_fallback and settings.openrouter_api_key
            ),
            "external_fallback_uses": getattr(app.state.model_client, "fallback_uses", 0),
            "openai_helper_configured": bool(settings.openai_api_key),
            "web_search": _web_search_status(settings),
            "orchestration_enabled": settings.enable_agent_orchestration,
            "production_readiness": _production_readiness(app),
            "public_trial": public_trial_status(settings),
            "vps_scheduler": app.state.vps_schedules.status(),
            "cost_target": {
                "baseline_model": CLAUDE_BASELINE_MODEL,
                "max_cost_ratio_vs_claude": settings.max_cost_ratio_vs_claude,
                "minimum_savings_vs_claude": 1 - settings.max_cost_ratio_vs_claude,
            },
        }

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        auth: AuthContext | None = None
        try:
            auth = require_gateway_auth(request, app.state.settings)
        except HTTPException as exc:
            if exc.status_code not in {401, 403}:
                raise

        profiles = model_profiles(app.state.settings)
        if auth and auth.customer and auth.customer.allowed_model != "*":
            filtered = [profile for profile in profiles if profile.id == auth.customer.allowed_model]
            profiles = filtered or profiles
        return {
            "object": "list",
            "data": [
                {
                    "id": profile.id,
                    "object": "model",
                    "type": "model",
                    "display_name": profile.display_name,
                    "description": profile.description,
                }
                for profile in profiles
            ]
        }

    @app.get("/v1/plans")
    async def list_public_plans() -> dict[str, Any]:
        return {
            "data": app.state.account_store.list_plans(),
            "public_trial": public_trial_status(app.state.settings),
        }

    @app.post("/v1/responses")
    async def create_openai_response(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_model_access(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

        anthropic_payload = responses_to_anthropic(payload)
        if "gateway_web_search" in payload:
            anthropic_payload["gateway_web_search"] = payload.get("gateway_web_search")
        stream = bool(payload.get("stream"))
        if stream:
            anthropic_payload["stream"] = True
            chunks, public_model = await _stream_gateway_message_chunks(request, app, anthropic_payload)
            return _sse_response(_anthropic_stream_to_response_sse(chunks, _public_model_label(public_model, app.state.settings)))
        anthropic_payload["stream"] = False
        response, public_model = await _complete_gateway_message(request, app, anthropic_payload)
        openai_response = anthropic_to_response(response, payload, public_model)
        return JSONResponse(openai_response)

    @app.post("/v1/chat/completions")
    async def create_chat_completion(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_model_access(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

        anthropic_payload = chat_to_anthropic(payload)
        if "gateway_web_search" in payload:
            anthropic_payload["gateway_web_search"] = payload.get("gateway_web_search")
        stream = bool(payload.get("stream"))
        if stream:
            anthropic_payload["stream"] = True
            chunks, public_model = await _stream_gateway_message_chunks(request, app, anthropic_payload)
            return _sse_response(_anthropic_stream_to_chat_sse(chunks, _public_model_label(public_model, app.state.settings)))
        anthropic_payload["stream"] = False
        response, public_model = await _complete_gateway_message(request, app, anthropic_payload)
        completion = anthropic_to_chat_completion(response, payload, public_model)
        return JSONResponse(completion)

    @app.get("/v1/budget")
    async def budget(request: Request) -> dict[str, Any]:
        _require_admin(request, app.state.settings)
        cost_policy = CostPolicy(
            max_ratio_vs_claude=app.state.settings.max_cost_ratio_vs_claude,
        )
        internal_models = {
            "router_agent": app.state.settings.router_agent,
            "cheap_code_agent": app.state.settings.cheap_code_agent,
            "code_agent": app.state.settings.code_agent,
            "reasoning_agent": app.state.settings.reasoning_agent,
            "ui_agent": app.state.settings.ui_agent,
            "fast_agent": app.state.settings.fast_agent,
            "premium_fallback": app.state.settings.premium_fallback,
            "ultra_fallback": app.state.settings.ultra_fallback,
            "frontend_coder_agent": app.state.settings.frontend_coder_agent,
            "frontend_fix_agent": app.state.settings.frontend_fix_agent,
            "frontend_reasoning_agent": app.state.settings.frontend_reasoning_agent,
            "backend_partner_agent": app.state.settings.backend_partner_agent,
            "project_reasoning_agent": app.state.settings.project_reasoning_agent,
            "deep_reasoning_agent": app.state.settings.deep_reasoning_agent,
            "gemini_code_helper_agent": app.state.settings.gemini_code_helper_agent,
        }
        return {
            "baseline_model": CLAUDE_BASELINE_MODEL,
            "max_cost_ratio_vs_claude": app.state.settings.max_cost_ratio_vs_claude,
            "minimum_savings_vs_claude": 1 - app.state.settings.max_cost_ratio_vs_claude,
            "allow_premium_fallback": app.state.settings.allow_premium_fallback,
            "allow_direct_external_models": app.state.settings.allow_direct_external_models,
            "openai_helper": {
                "configured": bool(app.state.settings.openai_api_key),
                "model": app.state.settings.openai_helper_model,
                "for_customers": app.state.settings.openai_helper_for_customers,
            },
            "web_search": _web_search_status(app.state.settings),
            "max_request_output_tokens": app.state.settings.max_request_output_tokens,
            "model_roles": {
                role: _public_cost_estimate(cost_policy.estimate(model).to_dict())
                for role, model in internal_models.items()
            },
        }

    @app.post("/v1/messages")
    async def messages(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_model_access(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

        if _is_claude_code_request(request, payload):
            payload = {**payload, "__gateway_client": "claude-code"}
        payload = _prepare_payload(payload, app.state.settings, auth, app.state.account_store)
        payload = await _with_customer_latency_policy(payload, app, auth)
        payload = await _with_customer_power_tier(payload, app, auth)
        decision = app.state.planner.plan(payload)
        control_answer = _prompt_control_answer(payload, app.state.settings)
        if control_answer:
            message = build_text_message(
                _public_model_label(decision.public_model, app.state.settings),
                control_answer,
                usage={"input_tokens": 0, "output_tokens": len(control_answer.split())},
            )
            if payload.get("stream"):
                return _sse_response(_stream_text_message(message))
            return JSONResponse(message)

        quick_answer = _quick_local_answer(payload)
        if quick_answer:
            app.state.usage.record_request(decision)
            message = build_text_message(
                _public_model_label(decision.public_model, app.state.settings),
                quick_answer,
                usage={"input_tokens": 1, "output_tokens": len(quick_answer.split())},
            )
            if payload.get("stream"):
                return _sse_response(_stream_text_message(message))
            return JSONResponse(message)

        identity_answer = _selected_model_identity_answer(payload, decision.public_model, app.state.settings)
        reservation = None
        if not identity_answer:
            payload = _with_simple_response_budget(payload, decision, app.state.settings)
            reservation = await _reserve_customer_budget(app, auth, payload, decision)
            payload = await _with_web_research(app, auth, payload)
        payload = _with_gateway_reasoning(payload, decision)
        payload = _with_public_model_identity(payload, decision.public_model, app.state.settings)
        payload = _with_automatic_skills(payload, decision)
        payload["__gateway_route_decision"] = decision
        if identity_answer:
            app.state.usage.record_request(decision)
            message = build_text_message(
                _public_model_label(decision.public_model, app.state.settings),
                identity_answer,
                usage={"input_tokens": 0, "output_tokens": len(identity_answer.split())},
            )
            if payload.get("stream"):
                return _sse_response(_stream_text_message(message))
            return JSONResponse(message)

        payload = await _with_openai_execution_guidance(app, auth, payload, decision)
        payload["__gateway_route_decision"] = decision
        payload = await _with_gemini_code_guidance(app, payload, decision)
        payload["__gateway_route_decision"] = decision
        if payload.get("stream"):
            if decision.use_orchestration:
                try:
                    response, _ = await app.state.orchestrator.complete(
                        {**payload, "stream": False},
                        allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
                    )
                except OpenRouterError as exc:
                    await _rollback_customer_budget(app, reservation)
                    _raise_public_upstream_error(exc)
                except Exception:
                    await _rollback_customer_budget(app, reservation)
                    raise

                await _settle_customer_budget(app, reservation, payload, decision, response)
                response = _with_public_response_model(response, decision.public_model, app.state.settings)
                return _sse_response(_stream_text_message(response))

            app.state.usage.record_request(decision)
            return _sse_response(
                _public_model_stream_with_budget_settlement(
                    app.state.model_client.stream_messages(payload, decision.selected_openrouter_model),
                    _public_model_label(decision.public_model, app.state.settings),
                    app=app,
                    reservation=reservation,
                    payload=payload,
                    decision=decision,
                )
            )

        try:
            response, _ = await app.state.orchestrator.complete(
                payload,
                allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
            )
        except OpenRouterError as exc:
            await _rollback_customer_budget(app, reservation)
            _raise_public_upstream_error(exc)
        except Exception:
            await _rollback_customer_budget(app, reservation)
            raise

        await _settle_customer_budget(app, reservation, payload, decision, response)
        response = _with_public_response_model(response, decision.public_model, app.state.settings)
        return JSONResponse(response)

    @app.post("/v1/messages/count_tokens")
    async def count_message_tokens(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_model_access(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        text = extract_prompt_text(payload)
        tool_chars = len(json.dumps(payload.get("tools") or [], ensure_ascii=False))
        token_count = max(1, (len(text) + tool_chars + 3) // 4)
        return JSONResponse({"input_tokens": token_count})

    @app.post("/v1/auth/signup")
    async def signup(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        _rate_limit_public_auth(app, payload)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"account": app.state.account_store.signup(payload)})

    @app.post("/v1/auth/login")
    async def login(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        _rate_limit_public_auth(app, payload)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"account": app.state.account_store.login(payload)})

    @app.get("/v1/auth/me")
    async def current_customer(request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return JSONResponse({"account": app.state.account_store.account_for_token(auth.token)})

    @app.post("/v1/billing/purchases")
    async def create_purchase(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        purchase = app.state.account_store.create_purchase(auth.token, payload)
        if purchase["paymentMethod"] == "card_subscription":
            subscription = await _create_mercado_pago_subscription(request, app, purchase)
            purchase = app.state.account_store.update_purchase_checkout(
                purchase["id"],
                preference_id=subscription["id"],
                checkout_url=subscription["init_point"],
                sandbox_checkout_url=subscription.get("sandbox_init_point") or "",
                payment_method="card_subscription",
            )
            return JSONResponse({"purchase": purchase, "payment": _subscription_payment_payload(subscription)})

        pix = await _create_mercado_pago_pix_payment(request, app, purchase)
        purchase = app.state.account_store.update_purchase_payment(
            purchase["id"],
            payment_id=str(pix.get("id") or ""),
            payment_method="pix",
            status=str(pix.get("status") or "pending"),
        )
        return JSONResponse({"purchase": purchase, "payment": _pix_payment_payload(pix)})

    @app.get("/v1/billing/purchases")
    async def list_customer_purchases(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return {"data": app.state.account_store.list_purchases_for_token(auth.token)}

    @app.post("/v1/billing/mercadopago/confirm")
    async def confirm_mercado_pago_payment(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        payload = payload or {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        payment_id = str(
            payload.get("paymentId")
            or payload.get("payment_id")
            or payload.get("collection_id")
            or payload.get("collectionId")
            or ""
        ).strip()
        preapproval_id = str(payload.get("preapprovalId") or payload.get("preapproval_id") or "").strip()
        if preapproval_id:
            preapproval = await _fetch_mercado_pago_preapproval(app, preapproval_id)
            purchase_id = str(preapproval.get("external_reference") or "")
            if not purchase_id:
                raise HTTPException(status_code=400, detail="Subscription is missing external reference.")
            result = app.state.account_store.approve_purchase_from_payment_for_token(
                auth.token,
                purchase_id,
                payment_id=str(preapproval.get("id") or preapproval_id),
                status=_preapproval_purchase_status(preapproval),
            )
            return JSONResponse(result)

        if not payment_id:
            raise HTTPException(status_code=400, detail="Payment id is required.")
        payment = await _fetch_mercado_pago_payment(app, payment_id)
        purchase_id = str(payment.get("external_reference") or "")
        if not purchase_id:
            raise HTTPException(status_code=400, detail="Payment is missing external reference.")
        result = app.state.account_store.approve_purchase_from_payment_for_token(
            auth.token,
            purchase_id,
            payment_id=str(payment.get("id") or payment_id),
            status=str(payment.get("status") or ""),
        )
        return JSONResponse(result)

    @app.post("/v1/billing/mercadopago/webhook")
    async def mercado_pago_webhook(request: Request) -> dict[str, str]:
        if not app.state.settings.mercado_pago_access_token:
            raise HTTPException(status_code=503, detail="Mercado Pago is not configured.")
        await _verify_mercado_pago_webhook_signature(request, app.state.settings)
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        payment_id = _mercado_pago_payment_id(request, payload)
        if not payment_id:
            preapproval_id = _mercado_pago_preapproval_id(request, payload)
            if not preapproval_id:
                return {"status": "ignored"}
            preapproval = await _fetch_mercado_pago_preapproval(app, preapproval_id)
            purchase_id = str(preapproval.get("external_reference") or "")
            if not purchase_id:
                return {"status": "ignored"}
            app.state.account_store.approve_purchase_from_payment(
                purchase_id,
                payment_id=str(preapproval.get("id") or preapproval_id),
                status=_preapproval_purchase_status(preapproval),
            )
            return {"status": "ok"}

        payment = await _fetch_mercado_pago_payment(app, payment_id)
        purchase_id = str(payment.get("external_reference") or "")
        if not purchase_id:
            return {"status": "ignored"}
        app.state.account_store.approve_purchase_from_payment(
            purchase_id,
            payment_id=str(payment.get("id") or payment_id),
            status=str(payment.get("status") or ""),
        )
        return {"status": "ok"}

    @app.get("/v1/admin/setup-status")
    async def admin_setup_status() -> dict[str, bool]:
        return {"configured": app.state.account_store.admin_configured()}

    @app.post("/v1/admin/setup")
    async def admin_setup(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        _rate_limit(request, app, "admin-auth", app.state.settings.auth_rate_limit)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"admin": app.state.account_store.setup_admin(payload)})

    @app.post("/v1/admin/login")
    async def admin_login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _rate_limit(request, app, "admin-auth", app.state.settings.auth_rate_limit)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        if app.state.account_store.admin_configured():
            return {"status": "ok", "admin": app.state.account_store.login_admin(payload)}

        _require_admin(request, app.state.settings)
        verify_admin_login(payload, app.state.settings)
        return {"status": "ok", "admin": {"token": extract_bearer_token(request) or ""}}

    @app.get("/v1/admin/ip-check")
    async def admin_ip_check(request: Request) -> dict[str, object]:
        return client_ip_for_debug(request, app.state.settings)

    @app.get("/v1/admin/gift-cards")
    async def list_gift_cards(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return {"data": app.state.account_store.list_gift_cards()}

    @app.post("/v1/admin/gift-cards")
    async def create_gift_card(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"giftCard": app.state.account_store.create_gift_card(payload)})

    @app.patch("/v1/admin/gift-cards/{card_id}")
    async def update_gift_card(
        card_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"giftCard": app.state.account_store.update_gift_card(card_id, payload)})

    @app.delete("/v1/admin/gift-cards/{card_id}")
    async def delete_gift_card(card_id: str, request: Request) -> dict[str, str]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return app.state.account_store.delete_gift_card(card_id)

    @app.get("/v1/admin/accounts")
    async def list_accounts(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return {"data": app.state.account_store.list_accounts()}

    @app.post("/v1/admin/api-tokens")
    async def create_api_token(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        accounts = app.state.account_store.create_api_tokens(payload)
        return JSONResponse({"account": accounts[0], "accounts": accounts})

    @app.post("/v1/admin/accounts/purge")
    async def purge_accounts(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if payload is not None and not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        include_gift_cards = bool((payload or {}).get("includeGiftCards"))
        return JSONResponse(app.state.account_store.purge_accounts(include_gift_cards=include_gift_cards))

    @app.post("/v1/admin/accounts/bulk-recharge")
    async def bulk_recharge_accounts(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if payload is not None and not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        amount = int(float((payload or {}).get("addTokens") or 50_000_000))
        return JSONResponse(app.state.account_store.bulk_add_daily_tokens(amount))

    @app.get("/v1/admin/vps/schedules")
    async def list_vps_schedules(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return {
            "data": app.state.vps_schedules.list_schedules(),
            "status": app.state.vps_schedules.status(),
        }

    @app.post("/v1/admin/vps/schedules")
    async def create_vps_schedule(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"schedule": app.state.vps_schedules.create_schedule(payload)})

    @app.patch("/v1/admin/vps/schedules/{schedule_id}")
    async def update_vps_schedule(
        schedule_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"schedule": app.state.vps_schedules.update_schedule(schedule_id, payload)})

    @app.delete("/v1/admin/vps/schedules/{schedule_id}")
    async def delete_vps_schedule(schedule_id: str, request: Request) -> dict[str, str]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return app.state.vps_schedules.delete_schedule(schedule_id)

    @app.post("/v1/admin/vps/schedules/tick")
    async def tick_vps_schedule(request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return JSONResponse({"status": await app.state.vps_schedules.tick()})

    @app.post("/v1/admin/vps/actions")
    async def run_vps_action(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        action = str(payload.get("action") or "")
        if action.strip().lower() == "start":
            return JSONResponse({"status": await app.state.vps_schedules.start_for_hours(int(payload.get("hours") or 12))})
        return JSONResponse({"status": await app.state.vps_schedules.manual_action(action)})

    @app.get("/v1/admin/purchases")
    async def list_purchases(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return {"data": app.state.account_store.list_purchases()}

    @app.post("/v1/admin/purchases/{purchase_id}/approve")
    async def approve_purchase(purchase_id: str, request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return JSONResponse({"purchase": app.state.account_store.approve_purchase(purchase_id)})

    @app.post("/v1/admin/purchases/{purchase_id}/cancel")
    async def cancel_purchase(purchase_id: str, request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return JSONResponse({"purchase": app.state.account_store.cancel_purchase(purchase_id)})

    @app.patch("/v1/admin/accounts/{account_id}")
    async def update_account(
        account_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"account": app.state.account_store.update_account(account_id, payload)})

    @app.delete("/v1/admin/accounts/{account_id}")
    async def delete_account(account_id: str, request: Request) -> dict[str, str]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return app.state.account_store.delete_account(account_id)

    @app.get("/v1/support/tickets/current")
    async def current_support_ticket(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        ticket = await asyncio.to_thread(app.state.support_store.current_for_customer, auth.token)
        return {"ticket": ticket}

    @app.post("/v1/support/tickets")
    async def open_support_ticket(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        ticket = await asyncio.to_thread(app.state.support_store.open_ticket, auth.token, payload)
        ticket = await _auto_support_reply(app, ticket, str(payload.get("message") or ""))
        return JSONResponse({"ticket": ticket})

    @app.post("/v1/support/tickets/{ticket_id}/messages")
    async def customer_support_message(
        ticket_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        ticket = await asyncio.to_thread(app.state.support_store.customer_message, auth.token, ticket_id, payload)
        if ticket.get("status") == "ai":
            ticket = await _auto_support_reply(app, ticket, str(payload.get("message") or ""))
        return JSONResponse({"ticket": ticket})

    @app.get("/v1/admin/support/tickets")
    async def list_support_tickets(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return await asyncio.to_thread(app.state.support_store.list_admin_tickets)

    @app.post("/v1/admin/support/tickets/{ticket_id}/claim")
    async def claim_support_ticket(ticket_id: str, request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        ticket = await asyncio.to_thread(app.state.support_store.claim_ticket, ticket_id)
        return JSONResponse({"ticket": ticket})

    @app.post("/v1/admin/support/tickets/{ticket_id}/messages")
    async def admin_support_message(
        ticket_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        ticket = await asyncio.to_thread(app.state.support_store.admin_message, ticket_id, payload)
        return JSONResponse({"ticket": ticket})

    @app.post("/v1/admin/support/tickets/{ticket_id}/close")
    async def close_support_ticket(ticket_id: str, request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        ticket = await asyncio.to_thread(app.state.support_store.close_ticket, ticket_id)
        return JSONResponse({"ticket": ticket})

    @app.get("/v1/conversations")
    async def list_conversations(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return {"data": app.state.conversation_store.list_for_customer(auth.token)}

    @app.get("/v1/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return {"conversation": app.state.conversation_store.get_for_customer(auth.token, conversation_id)}

    @app.post("/v1/conversations")
    async def save_conversation(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        payload = dict(payload)
        if not str(payload.get("title") or "").strip():
            generated_title = await _generate_conversation_title(app, payload)
            if generated_title:
                payload["title"] = generated_title
        return JSONResponse({"conversation": app.state.conversation_store.save_for_customer(auth.token, payload)})

    @app.get("/v1/code/workspaces")
    async def list_code_workspaces(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return {"data": app.state.code_workspaces.list_for_customer(auth.token)}

    @app.post("/v1/code/github/repositories")
    async def list_github_repositories(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        github_token = str(payload.get("githubToken") or payload.get("github_token") or "").strip()
        profile = str(payload.get("profile") or payload.get("owner") or "").strip()
        if not github_token:
            raise HTTPException(status_code=400, detail="Informe sua chave GitHub para listar repositórios.")
        repos = await _list_github_repositories(github_token, profile)
        return JSONResponse({"data": repos})

    @app.post("/v1/code/workspaces/upload")
    async def upload_code_workspace(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        if isinstance(payload.get("files"), list):
            workspace = app.state.code_workspaces.create_from_base64_files(auth.token, payload)
        else:
            workspace = app.state.code_workspaces.create_from_base64_zip(auth.token, payload)
        return JSONResponse({"workspace": workspace})

    @app.post("/v1/code/workspaces/github")
    async def import_github_workspace(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        repo_url = str(payload.get("repoUrl") or payload.get("repo_url") or "").strip()
        ref = str(payload.get("ref") or "").strip()
        github_token = str(payload.get("githubToken") or payload.get("github_token") or "").strip()
        if not github_token:
            raise HTTPException(
                status_code=400,
                detail="Para importar GitHub, entre com sua conta GitHub ou informe uma chave de acesso.",
            )
        zip_bytes, resolved_ref = await _download_github_zip(repo_url, ref, github_token)
        workspace = app.state.code_workspaces.create_from_zip(
            auth.token,
            name=str(payload.get("name") or ""),
            zip_bytes=zip_bytes,
            source="github",
            repo_url=repo_url,
            ref=resolved_ref,
        )
        return JSONResponse({"workspace": workspace})

    @app.get("/v1/code/workspaces/{workspace_id}/files")
    async def list_code_workspace_files(workspace_id: str, request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return app.state.code_workspaces.list_files(auth.token, workspace_id)

    @app.get("/v1/code/workspaces/{workspace_id}/files/content")
    async def read_code_workspace_file(
        workspace_id: str,
        request: Request,
        path: str,
    ) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        return app.state.code_workspaces.read_file(auth.token, workspace_id, path)

    @app.patch("/v1/code/workspaces/{workspace_id}/files/content")
    async def write_code_workspace_file(
        workspace_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"file": app.state.code_workspaces.write_file(auth.token, workspace_id, payload)})

    @app.post("/v1/code/workspaces/{workspace_id}/terminal")
    async def run_code_workspace_command(
        workspace_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"result": app.state.code_workspaces.run_command(auth.token, workspace_id, payload)})

    @app.post("/v1/code/workspaces/{workspace_id}/github/publish")
    async def publish_code_workspace_to_github(
        workspace_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        github_token = str(payload.get("githubToken") or payload.get("github_token") or "").strip()
        branch = str(payload.get("ref") or payload.get("branch") or "").strip()
        message = str(payload.get("message") or "").strip() or "Atualiza projeto pelo Hub"
        if not github_token:
            raise HTTPException(status_code=400, detail="Informe sua chave GitHub para publicar alterações.")
        workspace, files = app.state.code_workspaces.files_for_publish(auth.token, workspace_id)
        if workspace.get("source") != "github" or not workspace.get("repoUrl"):
            raise HTTPException(status_code=400, detail="Só workspaces importados do GitHub podem ser publicados.")
        result = await _publish_workspace_to_github(
            workspace["repoUrl"],
            branch or workspace.get("ref") or "main",
            github_token,
            message,
            files,
        )
        return JSONResponse({"publish": result})

    @app.get("/v1/code/workspaces/{workspace_id}/download")
    async def download_code_workspace(workspace_id: str, request: Request) -> StreamingResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        filename, data = app.state.code_workspaces.zip_bytes_for(auth.token, workspace_id)
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/v1/usage")
    async def usage(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = require_gateway_auth(request, app.state.settings)
        if auth.customer:
            return await _customer_usage_snapshot(app, auth)
        return _public_usage_snapshot(app.state.usage.snapshot())

    @app.post("/v1/router/debug")
    async def router_debug(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = require_gateway_auth(request, app.state.settings)
        payload = _prepare_payload(payload, app.state.settings, auth, app.state.account_store)
        payload = await _with_customer_latency_policy(payload, app, auth)
        payload = await _with_customer_power_tier(payload, app, auth)
        decision = app.state.planner.plan(payload)
        web_search = _web_search_debug(payload, app.state.settings, auth)
        return {
            **_public_route_decision(decision, app.state.settings),
            "web_search_policy": web_search["policy"],
            "web_search_reason": web_search["reason"],
            "web_search_enabled": web_search["enabled"],
            "web_search_should_search": web_search["should_search"],
        }

    @app.post("/v1/agent/run")
    async def agent_run(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_model_access(request, app.state.settings)
        payload = _prepare_payload(payload, app.state.settings, auth, app.state.account_store)
        payload = await _with_customer_latency_policy(payload, app, auth)
        payload = await _with_customer_power_tier(payload, app, auth)
        if payload.get("stream"):
            payload = {**payload, "stream": False}

        decision = app.state.planner.plan(payload, force_orchestration=True)
        payload = _with_simple_response_budget(payload, decision, app.state.settings)
        reservation = await _reserve_customer_budget(app, auth, payload, decision)
        payload = await _with_web_research(app, auth, payload)
        payload = _with_public_model_identity(payload, decision.public_model, app.state.settings)
        payload = await _with_openai_execution_guidance(app, auth, payload, decision)
        try:
            response, decision = await app.state.orchestrator.complete(
                payload,
                force_orchestration=True,
                allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
            )
        except OpenRouterError as exc:
            await _rollback_customer_budget(app, reservation)
            _raise_public_upstream_error(exc)
        except Exception:
            await _rollback_customer_budget(app, reservation)
            raise

        await _settle_customer_budget(app, reservation, payload, decision, response)
        return JSONResponse({"decision": _public_route_decision(decision, app.state.settings), "response": response})

    return app


def _mount_frontend(app: FastAPI) -> None:
    if not FRONTEND_DIR.exists():
        return

    app.mount("/frontier", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontier")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/frontier/app.html")

    @app.head("/", include_in_schema=False)
    async def root_head() -> JSONResponse:
        return JSONResponse({}, headers={"Cache-Control": "no-cache"})

    @app.get("/app", include_in_schema=False)
    async def app_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "app.html")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "admin.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "claude-mark.svg", media_type="image/svg+xml")


def _public_base_url(request: Request, settings: Settings) -> str:
    configured = settings.mercado_pago_public_url.strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _mercado_pago_notification_url(base_url: str) -> str:
    if not base_url.lower().startswith("https://"):
        return ""
    return f"{base_url}/v1/billing/mercadopago/webhook"


def _mercado_pago_app_url(base_url: str) -> str:
    if not base_url.lower().startswith("https://"):
        return ""
    return f"{base_url}/app"


def _mercado_pago_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        text = response.text.strip()
        return text[:500] or f"HTTP {response.status_code}"

    parts: list[str] = []
    for key in ("message", "error", "status", "status_detail"):
        value = data.get(key)
        if value:
            parts.append(str(value))

    cause = data.get("cause")
    if isinstance(cause, list):
        for item in cause[:3]:
            if isinstance(item, dict):
                detail = item.get("description") or item.get("message") or item.get("code")
                if detail:
                    parts.append(str(detail))
            elif item:
                parts.append(str(item))
    elif cause:
        parts.append(str(cause))

    return " · ".join(dict.fromkeys(parts))[:500] or f"HTTP {response.status_code}"


async def _verify_mercado_pago_webhook_signature(request: Request, settings: Settings) -> None:
    secret = settings.mercado_pago_webhook_secret.strip()
    if not secret:
        return

    signature_header = request.headers.get("x-signature", "")
    request_id = request.headers.get("x-request-id", "")
    signature_parts = _parse_signature_header(signature_header)
    ts = signature_parts.get("ts", "")
    received_hash = signature_parts.get("v1", "")
    if not ts or not received_hash:
        raise HTTPException(status_code=403, detail="Invalid Mercado Pago webhook signature.")

    data_id = request.query_params.get("data.id") or request.query_params.get("id") or ""
    manifest_parts = []
    if data_id:
        manifest_parts.append(f"id:{data_id};")
    if request_id:
        manifest_parts.append(f"request-id:{request_id};")
    manifest_parts.append(f"ts:{ts};")
    manifest = "".join(manifest_parts)
    expected_hash = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=403, detail="Invalid Mercado Pago webhook signature.")

    tolerance_seconds = settings.mercado_pago_webhook_tolerance_seconds
    if tolerance_seconds > 0:
        try:
            sent_at_ms = int(ts)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid Mercado Pago webhook signature timestamp.") from exc
        now_ms = int(time.time() * 1000)
        if abs(now_ms - sent_at_ms) > tolerance_seconds * 1000:
            raise HTTPException(status_code=403, detail="Expired Mercado Pago webhook signature.")


def _parse_signature_header(value: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for raw_part in value.split(","):
        key, separator, raw_value = raw_part.partition("=")
        if separator:
            parts[key.strip()] = raw_value.strip()
    return parts


async def _download_github_zip(repo_url: str, ref: str, github_token: str) -> tuple[bytes, str]:
    owner, repo = github_repo_parts(repo_url)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "claude-code-workspace",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        resolved_ref = ref
        if not resolved_ref:
            metadata = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            if metadata.status_code >= 400:
                raise HTTPException(status_code=502, detail="Não consegui acessar este repositório no GitHub.")
            resolved_ref = metadata.json().get("default_branch") or "main"

        archive = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/zipball/{resolved_ref}",
            headers=headers,
        )
        if archive.status_code >= 400:
            raise HTTPException(status_code=502, detail="Não consegui baixar o ZIP do GitHub.")
        return archive.content, resolved_ref


async def _list_github_repositories(github_token: str, profile: str = "") -> list[dict[str, Any]]:
    headers = _github_headers(github_token)
    repos: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        for page in range(1, 6):
            response = await client.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            if response.status_code >= 400:
                raise HTTPException(status_code=502, detail="Não consegui listar seus repositórios no GitHub.")
            items = response.json()
            if not isinstance(items, list) or not items:
                break
            repos.extend(_public_github_repo(item) for item in items if isinstance(item, dict))
            if len(items) < 100:
                break

    cleaned_profile = profile.strip().lower()
    if cleaned_profile:
        repos = [
            repo
            for repo in repos
            if repo["owner"].lower() == cleaned_profile or cleaned_profile in repo["fullName"].lower()
        ]
    return repos[:300]


async def _publish_workspace_to_github(
    repo_url: str,
    branch: str,
    github_token: str,
    message: str,
    files: list[tuple[str, bytes]],
) -> dict[str, Any]:
    owner, repo = github_repo_parts(repo_url)
    headers = _github_headers(github_token)
    updated = 0
    created = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for path, content in files:
            if len(content) > 950_000:
                skipped += 1
                continue
            encoded_path = quote(path, safe="/")
            existing_sha = ""
            current = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}",
                headers=headers,
                params={"ref": branch},
            )
            if current.status_code == 200:
                current_data = current.json()
                if isinstance(current_data, dict):
                    existing_sha = str(current_data.get("sha") or "")
            elif current.status_code not in {404}:
                errors.append({"path": path, "error": f"HTTP {current.status_code}"})
                continue

            body: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content).decode("ascii"),
                "branch": branch,
            }
            if existing_sha:
                body["sha"] = existing_sha
            response = await client.put(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}",
                headers=headers,
                json=body,
            )
            if response.status_code in {200, 201}:
                if existing_sha:
                    updated += 1
                else:
                    created += 1
                continue
            detail = _github_error_detail(response)
            errors.append({"path": path, "error": detail})
            if len(errors) >= 8:
                break
    if errors:
        raise HTTPException(status_code=502, detail={"message": "Alguns arquivos não foram publicados.", "errors": errors})
    return {
        "repoUrl": repo_url,
        "branch": branch,
        "updated": updated,
        "created": created,
        "skipped": skipped,
        "fileCount": len(files),
    }


def _github_headers(github_token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "User-Agent": "claude-code-workspace",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _public_github_repo(repo: dict[str, Any]) -> dict[str, Any]:
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    full_name = str(repo.get("full_name") or "")
    return {
        "id": repo.get("id"),
        "name": str(repo.get("name") or ""),
        "fullName": full_name,
        "owner": str(owner.get("login") or full_name.split("/", 1)[0]),
        "private": bool(repo.get("private")),
        "defaultBranch": str(repo.get("default_branch") or "main"),
        "htmlUrl": str(repo.get("html_url") or ""),
        "description": str(repo.get("description") or ""),
        "updatedAt": str(repo.get("updated_at") or ""),
    }


def _github_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(data, dict):
        message = data.get("message")
        if message:
            return str(message)[:300]
    return f"HTTP {response.status_code}"


async def _create_mercado_pago_preference(
    request: Request,
    app: FastAPI,
    purchase: dict[str, Any],
) -> dict[str, Any]:
    token = app.state.settings.mercado_pago_access_token
    if not token:
        raise HTTPException(status_code=503, detail="Configure MERCADO_PAGO_ACCESS_TOKEN to sell plans.")

    base_url = _public_base_url(request, app.state.settings)
    payload = _mercado_pago_preference_payload(base_url, purchase)

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"Mercado Pago recusou o checkout: {_mercado_pago_error_detail(response)}",
        )

    data = response.json()
    if not data.get("id") or not data.get("init_point"):
        raise HTTPException(status_code=502, detail="Mercado Pago did not return a checkout URL.")
    return data


async def _create_mercado_pago_pix_payment(
    request: Request,
    app: FastAPI,
    purchase: dict[str, Any],
) -> dict[str, Any]:
    token = app.state.settings.mercado_pago_access_token
    if not token:
        raise HTTPException(status_code=503, detail="Configure MERCADO_PAGO_ACCESS_TOKEN to sell plans.")

    base_url = _public_base_url(request, app.state.settings)
    payload = {
        "transaction_amount": float(purchase["price"]),
        "description": f"Claude {purchase['plan']}",
        "payment_method_id": "pix",
        "external_reference": purchase["id"],
        "payer": _mercado_pago_payment_payer(purchase),
        "metadata": {
            "account_id": purchase["accountId"],
            "plan_id": purchase["planId"],
            "purchase_id": purchase["id"],
        },
    }
    notification_url = _mercado_pago_notification_url(base_url)
    if notification_url:
        payload["notification_url"] = notification_url
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.mercadopago.com/v1/payments",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Idempotency-Key": purchase["id"],
            },
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"Mercado Pago recusou o Pix: {_mercado_pago_error_detail(response)}",
        )
    return response.json()


async def _create_mercado_pago_subscription(
    request: Request,
    app: FastAPI,
    purchase: dict[str, Any],
) -> dict[str, Any]:
    token = app.state.settings.mercado_pago_access_token
    if not token:
        raise HTTPException(status_code=503, detail="Configure MERCADO_PAGO_ACCESS_TOKEN to sell plans.")

    base_url = _public_base_url(request, app.state.settings)
    payload = {
        "reason": f"Assinatura Claude {purchase['plan']}",
        "external_reference": purchase["id"],
        "payer_email": purchase["login"],
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": float(purchase["price"]),
            "currency_id": "BRL",
        },
        "status": "pending",
    }
    app_url = _mercado_pago_app_url(base_url)
    if app_url:
        payload["back_url"] = f"{app_url}?payment=success"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.mercadopago.com/preapproval",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail=f"Mercado Pago recusou a assinatura: {_mercado_pago_error_detail(response)}",
        )
    data = response.json()
    if not data.get("id") or not data.get("init_point"):
        raise HTTPException(status_code=502, detail="Mercado Pago did not return a subscription URL.")
    return data


def _pix_payment_payload(payment: dict[str, Any]) -> dict[str, Any]:
    transaction = (payment.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {
        "type": "pix",
        "id": str(payment.get("id") or ""),
        "status": str(payment.get("status") or ""),
        "qrCode": str(transaction.get("qr_code") or ""),
        "qrCodeBase64": str(transaction.get("qr_code_base64") or ""),
        "ticketUrl": str(transaction.get("ticket_url") or ""),
    }


def _subscription_payment_payload(subscription: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "card_subscription",
        "id": str(subscription.get("id") or ""),
        "checkoutUrl": str(subscription.get("init_point") or ""),
        "sandboxCheckoutUrl": str(subscription.get("sandbox_init_point") or ""),
        "status": str(subscription.get("status") or ""),
    }


async def _auto_support_reply(app: FastAPI, ticket: dict[str, Any], message: str) -> dict[str, Any]:
    text = message.strip()
    if not text:
        return ticket
    if _support_needs_human(text):
        return app.state.support_store.escalate_to_human(
            ticket["id"],
            "Vou encaminhar sua conversa para o atendimento humano. Assim que o Mano puder assumir, ele continua por aqui.",
        )

    reply = await _generate_support_reply(app, ticket, text)
    if reply.startswith("ESCALATE:"):
        reason = reply.removeprefix("ESCALATE:").strip()
        message_to_customer = "Vou encaminhar sua conversa para o atendimento humano."
        if reason:
            message_to_customer = f"{message_to_customer} Motivo: {reason[:220]}"
        return app.state.support_store.escalate_to_human(ticket["id"], message_to_customer)
    return app.state.support_store.ai_message(ticket["id"], reply)


async def _generate_support_reply(app: FastAPI, ticket: dict[str, Any], message: str) -> str:
    fallback = _fallback_support_reply(message)
    if not app.state.openai_helper:
        return fallback

    history = "\n".join(
        f"{item.get('author')}: {item.get('body')}"
        for item in (ticket.get("messages") or [])[-8:]
        if isinstance(item, dict)
    )
    try:
        reply = await app.state.openai_helper.generate_text(
            instructions=SUPPORT_ASSISTANT_PROMPT,
            input_text=(
                f"Cliente: {ticket.get('customerName')} <{ticket.get('customerLogin')}>\n"
                f"Status atual: {ticket.get('status')}\n"
                f"Histórico recente:\n{history}\n\n"
                f"Nova mensagem do cliente:\n{message}"
            )[:6000],
            max_output_tokens=260,
        )
    except Exception:
        return fallback
    cleaned = " ".join(reply.strip().split())
    return cleaned[:1200] or fallback


def _support_needs_human(message: str) -> bool:
    text = _normalize_text(message)
    human_phrases = (
        "falar com humano",
        "falar com atendente",
        "falar com o mano",
        "chama o mano",
        "quero humano",
        "quero atendente",
        "pessoa real",
        "suporte humano",
        "admin",
        "dono",
    )
    manual_risk_terms = (
        "reembolso",
        "estorno",
        "cobranca indevida",
        "cobranca duplicada",
        "pagamento aprovado",
        "pagamento nao liberou",
        "plano nao liberou",
        "conta invadida",
        "hackearam",
        "vazou",
        "processo",
        "juridico",
        "ameaca",
    )
    return any(phrase in text for phrase in human_phrases + manual_risk_terms)


def _fallback_support_reply(message: str) -> str:
    text = _normalize_text(message)
    if "github" in text or "repo" in text or "repositorio" in text:
        return (
            "Para conectar GitHub, entre no Hub, informe seu perfil ou organização, a branch padrão e sua chave GitHub. "
            "Depois o sistema lista seus repositórios para você escolher sem colar URL manual. "
            "Se o repo não aparecer, confira se a chave tem permissão de leitura no repositório."
        )
    if "zip" in text or "pasta" in text or "arquivo" in text:
        return (
            "Você pode subir um ZIP ou selecionar uma pasta. O projeto fica extraído em um workspace, "
            "as edições salvam nesse workspace e o botão de download gera um ZIP atualizado."
        )
    if "pix" in text or "pagamento" in text or "plano" in text:
        return (
            "Para planos pagos, o Pix abre no checkout e a tela atualiza quando o pagamento é confirmado. "
            "Se o pagamento já foi aprovado e o plano não liberou, eu encaminho para atendimento humano."
        )
    if "senha" in text or "login" in text or "entrar" in text:
        return (
            "Confira se está usando o mesmo e-mail do cadastro e tente entrar novamente. "
            "Se a conta estiver pausada ou você perdeu acesso ao e-mail, peça atendimento humano por aqui."
        )
    return (
        "Posso te ajudar por aqui. Me diga o que aconteceu, qual tela você estava usando e, se aparecer erro, "
        "mande o texto exato da mensagem. Se for algo que precise de ação manual, eu encaminho para o Mano."
    )


def _mercado_pago_preference_payload(base_url: str, purchase: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "items": [
            {
                "id": purchase["planId"],
                "title": f"Claude {purchase['plan']}",
                "description": f"Plano {purchase['plan']} do Claude",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": float(purchase["price"]),
            }
        ],
        "payer": _mercado_pago_payer(purchase),
        "external_reference": purchase["id"],
        "binary_mode": False,
        "payment_methods": {
            "installments": 1,
            "default_installments": 1,
        },
        "statement_descriptor": "CLAUDE",
        "metadata": {
            "account_id": purchase["accountId"],
            "plan_id": purchase["planId"],
            "purchase_id": purchase["id"],
        },
    }
    notification_url = _mercado_pago_notification_url(base_url)
    if notification_url:
        payload["notification_url"] = notification_url
    app_url = _mercado_pago_app_url(base_url)
    if app_url:
        payload["back_urls"] = {
            "success": f"{app_url}?payment=success",
            "failure": f"{app_url}?payment=failure",
            "pending": f"{app_url}?payment=pending",
        }
        payload["auto_return"] = "approved"
    return payload


def _mercado_pago_payer(purchase: dict[str, Any]) -> dict[str, Any]:
    first_name, surname = _split_payer_name(purchase.get("name"))
    payer = {
        "name": first_name,
        "surname": surname,
        "email": purchase["login"],
    }
    document = _payer_document(purchase.get("payerDocument"))
    if document:
        payer["identification"] = document
    return payer


def _mercado_pago_payment_payer(purchase: dict[str, Any]) -> dict[str, Any]:
    first_name, surname = _split_payer_name(purchase.get("name"))
    payer = {
        "first_name": first_name,
        "last_name": surname,
        "email": purchase["login"],
    }
    document = _payer_document(purchase.get("payerDocument"))
    if document:
        payer["identification"] = document
    return payer


def _split_payer_name(name: Any) -> tuple[str, str]:
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "Cliente", "Claude"
    if len(parts) == 1:
        return parts[0][:40], "Claude"
    return parts[0][:40], " ".join(parts[1:])[:80]


def _payer_document(value: Any) -> dict[str, str] | None:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if len(digits) == 11:
        return {"type": "CPF", "number": digits}
    if len(digits) == 14:
        return {"type": "CNPJ", "number": digits}
    return None


def _mercado_pago_payment_id(request: Request, payload: dict[str, Any]) -> str:
    topic = str(request.query_params.get("topic") or payload.get("type") or payload.get("topic") or "").lower()
    resource = str(payload.get("resource") or "")
    if "preapproval" in topic or "preapproval" in resource:
        return ""
    query_id = request.query_params.get("data.id") or request.query_params.get("id")
    if query_id:
        return query_id
    data = payload.get("data") if isinstance(payload, dict) else {}
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    if isinstance(payload, dict) and payload.get("resource"):
        return str(payload["resource"]).rstrip("/").rsplit("/", 1)[-1]
    return ""


def _mercado_pago_preapproval_id(request: Request, payload: dict[str, Any]) -> str:
    topic = str(request.query_params.get("topic") or payload.get("type") or payload.get("topic") or "").lower()
    resource = str(payload.get("resource") or "")
    if "preapproval" in topic:
        query_id = request.query_params.get("data.id") or request.query_params.get("id")
        if query_id:
            return query_id
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    if "preapproval" in resource:
        return resource.rstrip("/").rsplit("/", 1)[-1]
    return ""


async def _fetch_mercado_pago_payment(app: FastAPI, payment_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {app.state.settings.mercado_pago_access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Could not verify Mercado Pago payment.")
    return response.json()


async def _fetch_mercado_pago_preapproval(app: FastAPI, preapproval_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.mercadopago.com/preapproval/{preapproval_id}",
            headers={"Authorization": f"Bearer {app.state.settings.mercado_pago_access_token}"},
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Could not verify Mercado Pago subscription.")
    return response.json()


def _preapproval_purchase_status(preapproval: dict[str, Any]) -> str:
    status = str(preapproval.get("status") or "").lower()
    if status in {"authorized", "active"}:
        return "approved"
    if status in {"cancelled", "canceled", "paused"}:
        return "canceled"
    return status or "pending"


async def _complete_gateway_message(
    request: Request,
    app: FastAPI,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    trace_start = time.perf_counter()
    trace: dict[str, Any] = {"path": request.url.path, "stream": False}
    auth = _require_model_access(request, app.state.settings)
    if _is_claude_code_request(request, payload):
        payload = {**payload, "__gateway_client": "claude-code"}
    payload = _prepare_payload(payload, app.state.settings, auth, app.state.account_store)
    trace["prepare_ms"] = _elapsed_ms(trace_start)
    payload = await _with_customer_latency_policy(payload, app, auth)
    payload = await _with_customer_power_tier(payload, app, auth)
    trace["policy_ms"] = _elapsed_ms(trace_start)
    decision = app.state.planner.plan(payload)
    trace["context_trimmed"] = bool(payload.get("__gateway_context_trimmed"))
    control_answer = _prompt_control_answer(payload, app.state.settings)
    if control_answer:
        return (
            build_text_message(
                _public_model_label(decision.public_model, app.state.settings),
                control_answer,
                usage={"input_tokens": 0, "output_tokens": len(control_answer.split())},
            ),
            decision.public_model,
        )

    quick_answer = _quick_local_answer(payload)
    if quick_answer:
        app.state.usage.record_request(decision)
        return (
            build_text_message(
                _public_model_label(decision.public_model, app.state.settings),
                quick_answer,
                usage={"input_tokens": 1, "output_tokens": len(quick_answer.split())},
            ),
            decision.public_model,
        )

    identity_answer = _selected_model_identity_answer(payload, decision.public_model, app.state.settings)
    reservation = None
    if not identity_answer:
        payload = _with_simple_response_budget(payload, decision, app.state.settings)
        reservation = await _reserve_customer_budget(app, auth, payload, decision)
        payload = await _with_web_research(app, auth, payload)
    payload = _with_gateway_reasoning(payload, decision)
    payload = _with_public_model_identity(payload, decision.public_model, app.state.settings)
    payload["__gateway_route_decision"] = decision
    if identity_answer:
        app.state.usage.record_request(decision)
        return (
            build_text_message(
                _public_model_label(decision.public_model, app.state.settings),
                identity_answer,
                usage={"input_tokens": 0, "output_tokens": len(identity_answer.split())},
            ),
            decision.public_model,
        )

    payload = await _with_openai_execution_guidance(app, auth, payload, decision)
    payload["__gateway_route_decision"] = decision
    payload = await _with_gemini_code_guidance(app, payload, decision)
    payload["__gateway_route_decision"] = decision
    try:
        response, _ = await app.state.orchestrator.complete(
            payload,
            allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
        )
    except OpenRouterError as exc:
        await _rollback_customer_budget(app, reservation)
        _raise_public_upstream_error(exc)
    except Exception:
        await _rollback_customer_budget(app, reservation)
        raise
    await _settle_customer_budget(app, reservation, payload, decision, response)
    trace["total_ms"] = _elapsed_ms(trace_start)
    _log_latency_trace(trace)
    response = _with_public_response_model(response, decision.public_model, app.state.settings)
    return response, decision.public_model


async def _stream_gateway_message_chunks(
    request: Request,
    app: FastAPI,
    payload: dict[str, Any],
) -> tuple[Any, str]:
    trace_start = time.perf_counter()
    trace: dict[str, Any] = {"path": request.url.path, "stream": True}
    auth = _require_model_access(request, app.state.settings)
    if _is_claude_code_request(request, payload):
        payload = {**payload, "__gateway_client": "claude-code"}
    payload = _prepare_payload(payload, app.state.settings, auth, app.state.account_store)
    trace["prepare_ms"] = _elapsed_ms(trace_start)
    payload = await _with_customer_latency_policy(payload, app, auth)
    payload = await _with_customer_power_tier(payload, app, auth)
    trace["policy_ms"] = _elapsed_ms(trace_start)
    decision = app.state.planner.plan(payload)
    trace["context_trimmed"] = bool(payload.get("__gateway_context_trimmed"))

    control_answer = _prompt_control_answer(payload, app.state.settings)
    quick_answer = control_answer or _quick_local_answer(payload)
    identity_answer = None if quick_answer else _selected_model_identity_answer(
        payload,
        decision.public_model,
        app.state.settings,
    )
    if quick_answer or identity_answer:
        app.state.usage.record_request(decision)
        text = quick_answer or identity_answer or ""
        return (
            _stream_text_message(
                build_text_message(
                    _public_model_label(decision.public_model, app.state.settings),
                    text,
                    usage={"input_tokens": 0 if identity_answer or control_answer else 1, "output_tokens": len(text.split())},
                )
            ),
            decision.public_model,
        )

    payload = _with_simple_response_budget(payload, decision, app.state.settings)
    reservation = await _reserve_customer_budget(app, auth, payload, decision)
    payload = await _with_web_research(app, auth, payload)
    payload = _with_gateway_reasoning(payload, decision)
    payload = _with_public_model_identity(payload, decision.public_model, app.state.settings)
    payload = _with_automatic_skills(payload, decision)
    payload["__gateway_route_decision"] = decision
    payload = await _with_openai_execution_guidance(app, auth, payload, decision)
    payload["__gateway_route_decision"] = decision
    payload = await _with_gemini_code_guidance(app, payload, decision)
    payload["__gateway_route_decision"] = decision

    if decision.use_orchestration:
        try:
            response, _ = await app.state.orchestrator.complete(
                {**payload, "stream": False},
                allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
            )
        except OpenRouterError as exc:
            await _rollback_customer_budget(app, reservation)
            _raise_public_upstream_error(exc)
        except Exception:
            await _rollback_customer_budget(app, reservation)
            raise
        await _settle_customer_budget(app, reservation, payload, decision, response)
        response = _with_public_response_model(response, decision.public_model, app.state.settings)
        return _latency_traced_stream(_stream_text_message(response), trace, trace_start), decision.public_model

    app.state.usage.record_request(decision)
    return (
        _latency_traced_stream(
            _public_model_stream_with_budget_settlement(
                app.state.model_client.stream_messages(payload, decision.selected_openrouter_model),
                _public_model_label(decision.public_model, app.state.settings),
                app=app,
                reservation=reservation,
                payload=payload,
                decision=decision,
            ),
            trace,
            trace_start,
        ),
        decision.public_model,
    )


async def _anthropic_stream_to_chat_sse(chunks: Any, model: str):
    chunk_id = f"chatcmpl-{int(time.time() * 1000)}"
    created = int(time.time())
    final_reason = "stop"
    yielded_final = False
    yield _chat_sse_chunk(chunk_id, created, model, {"role": "assistant"}, None)

    buffer = ""
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", "replace")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            async for outgoing in _chat_sse_from_anthropic_event(event, chunk_id, created, model):
                if outgoing[0]:
                    final_reason = outgoing[0]
                    continue
                yield outgoing[1]
            if _anthropic_event_type(event) == "message_stop":
                yielded_final = True
                yield _chat_sse_chunk(chunk_id, created, model, {}, final_reason)
                yield b"data: [DONE]\n\n"

    if buffer:
        async for outgoing in _chat_sse_from_anthropic_event(buffer, chunk_id, created, model):
            if outgoing[0]:
                final_reason = outgoing[0]
            else:
                yield outgoing[1]
    if not yielded_final:
        yield _chat_sse_chunk(chunk_id, created, model, {}, final_reason)
        yield b"data: [DONE]\n\n"


async def _anthropic_stream_to_response_sse(chunks: Any, model: str):
    response_id = f"resp_{int(time.time() * 1000)}"
    item_id = f"msg_{int(time.time() * 1000)}"
    created = int(time.time())
    text_started = False
    final_response = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": model,
        "output": [],
    }
    yield _response_sse_event(
        "response.created",
        {"type": "response.created", "response": {**final_response, "status": "in_progress"}},
    )

    buffer = ""
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", "replace")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            payload = _anthropic_event_payload(event)
            if not payload:
                continue
            event_type = str(payload.get("type") or "")
            if event_type == "content_block_delta":
                delta = payload.get("delta")
                if not isinstance(delta, dict) or not isinstance(delta.get("text"), str) or not delta["text"]:
                    continue
                if not text_started:
                    text_started = True
                    yield _response_sse_event(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
                        },
                    )
                    yield _response_sse_event(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "item_id": item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    )
                yield _response_sse_event(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": delta["text"],
                    },
                )
            elif event_type == "message_stop":
                yield _response_sse_event("response.completed", {"type": "response.completed", "response": final_response})
                yield b"data: [DONE]\n\n"
                return

    yield _response_sse_event("response.completed", {"type": "response.completed", "response": final_response})
    yield b"data: [DONE]\n\n"


def _response_sse_event(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


async def _chat_sse_from_anthropic_event(event: str, chunk_id: str, created: int, model: str):
    payload = _anthropic_event_payload(event)
    if not payload:
        return
    event_type = str(payload.get("type") or "")
    if event_type == "content_block_start":
        block = payload.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            index = int(payload.get("index") or 0)
            delta = {
                "tool_calls": [
                    {
                        "index": index,
                        "id": str(block.get("id") or f"tool_{index}"),
                        "type": "function",
                        "function": {"name": str(block.get("name") or ""), "arguments": ""},
                    }
                ]
            }
            yield None, _chat_sse_chunk(chunk_id, created, model, delta, None)
        return
    if event_type == "content_block_delta":
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return
        if isinstance(delta.get("text"), str) and delta["text"]:
            yield None, _chat_sse_chunk(chunk_id, created, model, {"content": delta["text"]}, None)
            return
        if isinstance(delta.get("partial_json"), str):
            index = int(payload.get("index") or 0)
            yield None, _chat_sse_chunk(
                chunk_id,
                created,
                model,
                {"tool_calls": [{"index": index, "function": {"arguments": delta["partial_json"]}}]},
                None,
            )
        return
    if event_type == "message_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict):
            reason = str(delta.get("stop_reason") or "")
            if reason == "tool_use":
                yield "tool_calls", b""
            elif reason == "max_tokens":
                yield "length", b""
            elif reason:
                yield "stop", b""


def _anthropic_event_type(event: str) -> str:
    payload = _anthropic_event_payload(event)
    return str(payload.get("type") or "") if payload else ""


def _anthropic_event_payload(event: str) -> dict[str, Any] | None:
    data_lines = [
        line.removeprefix("data:").strip()
        for line in str(event or "").splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    if not raw or raw == "[DONE]":
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _chat_sse_chunk(
    chunk_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def _latency_traced_stream(chunks: Any, trace: dict[str, Any], start: float):
    first_chunk_logged = False
    try:
        async for chunk in chunks:
            if not first_chunk_logged:
                trace["first_chunk_ms"] = _elapsed_ms(start)
                first_chunk_logged = True
            yield chunk
    finally:
        trace["total_ms"] = _elapsed_ms(start)
        _log_latency_trace(trace)


def _log_latency_trace(trace: dict[str, Any]) -> None:
    if not LATENCY_LOGGER.isEnabledFor(logging.INFO):
        return
    safe_trace = {
        key: trace.get(key)
        for key in ("path", "stream", "context_trimmed", "prepare_ms", "policy_ms", "first_chunk_ms", "total_ms")
        if key in trace
    }
    LATENCY_LOGGER.info("gateway_latency %s", json.dumps(safe_trace, sort_keys=True))


def _prepare_payload(
    payload: dict[str, Any],
    settings: Settings,
    auth: AuthContext,
    account_store: AccountStore | None = None,
) -> dict[str, Any]:
    limited, controls = _apply_prompt_control_commands(dict(payload), settings)
    if auth.customer and account_store and (controls.get("model") is not None or controls.get("reasoning") is not None):
        account_store.update_preferences_for_token(
            auth.token,
            model=controls.get("model"),
            reasoning=controls.get("reasoning"),
        )

    if auth.customer and controls.get("model"):
        limited["model"] = controls["model"]
        limited["__gateway_model_locked"] = True
    elif auth.customer and auth.customer.preferred_model:
        limited["model"] = auth.customer.preferred_model
        limited["__gateway_model_locked"] = True
    elif controls.get("model"):
        limited["model"] = controls["model"]
        limited["__gateway_model_locked"] = True

    limited = _limit_payload_context(limited, settings)
    prompt_text = extract_prompt_text(limited)
    if len(prompt_text) > settings.max_request_input_chars:
        raise HTTPException(
            status_code=413,
            detail="Latest request is larger than MAX_REQUEST_INPUT_CHARS.",
        )

    reasoning_value = limited.pop("gateway_reasoning_mode", None)
    if reasoning_value is None:
        reasoning_value = limited.pop("reasoning_mode", None)
    else:
        limited.pop("reasoning_mode", None)
    if controls.get("reasoning"):
        reasoning_value = controls["reasoning"]
    elif reasoning_value is None and auth.customer and auth.customer.preferred_reasoning:
        reasoning_value = auth.customer.preferred_reasoning
    if reasoning_value is None:
        reasoning_value = "fast"
        limited["__gateway_reasoning_auto_default"] = True
    limited["__gateway_reasoning_mode"] = normalize_reasoning_mode(reasoning_value)
    policy_value = limited.pop("gateway_web_search", None)
    if policy_value is None:
        policy_value = limited.pop("web_search", "auto")
    else:
        limited.pop("web_search", None)
    search_policy = normalize_web_search_policy(policy_value)
    limited["__gateway_web_search_policy"] = search_policy
    limited["max_tokens"] = _safe_max_tokens(limited, settings)
    if auth.customer:
        limited = clamp_customer_payload(limited, settings, auth.customer)
        limited["max_tokens"] = _safe_max_tokens(limited, settings)
    return limited


def _limit_payload_context(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return payload


def _message_cannot_start_context(message: Any) -> bool:
    if not isinstance(message, dict):
        return True
    if str(message.get("role") or "").lower() != "user":
        return True
    return _message_has_tool_result(message)


def _message_has_tool_result(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def _truncate_message_to_context(message: dict[str, Any], limit: int) -> dict[str, Any] | None:
    content = message.get("content")
    budget = max(1000, limit - 2000)
    if isinstance(content, str):
        return {**message, "content": content[-budget:]}
    if not isinstance(content, list):
        return None

    trimmed_blocks: list[Any] = []
    remaining = budget
    for block in reversed(content):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            trimmed_blocks.insert(0, block)
            continue
        text = block["text"]
        if remaining <= 0:
            continue
        trimmed_text = text[-remaining:]
        remaining -= len(trimmed_text)
        trimmed_blocks.insert(0, {**block, "text": trimmed_text})

    if not trimmed_blocks:
        return None
    return {**message, "content": trimmed_blocks}


def _apply_prompt_control_commands(
    payload: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    controls: dict[str, str | None] = {}
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, controls

    cleaned_messages: list[Any] = []
    last_user_index = max(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict) and str(message.get("role") or "").lower() == "user"
        ),
        default=-1,
    )
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
            cleaned_messages.append(message)
            continue

        cleaned, message_controls, has_content = _clean_control_message(message, settings)
        controls.update(message_controls)
        if index == last_user_index and message_controls and not has_content:
            payload["__gateway_prompt_control_only"] = True
        if has_content:
            cleaned_messages.append(cleaned)

    if controls:
        payload["messages"] = cleaned_messages
        payload["__gateway_prompt_controls"] = controls
    return payload, controls


def _prompt_control_answer(payload: dict[str, Any], settings: Settings) -> str | None:
    if not payload.get("__gateway_prompt_control_only"):
        return None
    controls = payload.get("__gateway_prompt_controls")
    if not isinstance(controls, dict):
        return None

    parts: list[str] = []
    if controls.get("model"):
        parts.append(f"modelo {_public_model_label(str(payload.get('model') or settings.auto_public_model), settings)}")
    if controls.get("reasoning"):
        parts.append(f"raciocínio {_reasoning_label(str(payload.get('__gateway_reasoning_mode') or 'auto'))}")
    if not parts:
        return None
    return f"Configuração aplicada: {', '.join(parts)}. Pode mandar a próxima mensagem."


def _clean_control_message(
    message: dict[str, Any],
    settings: Settings,
) -> tuple[dict[str, Any], dict[str, str | None], bool]:
    content = message.get("content")
    controls: dict[str, str | None] = {}
    if isinstance(content, str):
        cleaned, text_controls = _strip_control_lines(content, settings)
        controls.update(text_controls)
        return {**message, "content": cleaned}, controls, bool(cleaned.strip())

    if not isinstance(content, list):
        return message, controls, bool(content)

    cleaned_blocks: list[Any] = []
    has_content = False
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            cleaned_blocks.append(block)
            has_content = True
            continue
        cleaned, text_controls = _strip_control_lines(block["text"], settings)
        controls.update(text_controls)
        if cleaned.strip():
            cleaned_blocks.append({**block, "text": cleaned})
            has_content = True
    return {**message, "content": cleaned_blocks}, controls, has_content


def _strip_control_lines(text: str, settings: Settings) -> tuple[str, dict[str, str | None]]:
    controls: dict[str, str | None] = {}
    kept: list[str] = []
    for line in str(text or "").splitlines():
        parsed = _parse_control_line(line, settings)
        if parsed:
            controls.update(parsed)
            continue
        kept.append(line)
    return "\n".join(kept).strip(), controls


def _parse_control_line(line: str, settings: Settings) -> dict[str, str] | None:
    raw = str(line or "").strip()
    if not raw:
        return None

    slash = raw.startswith("/")
    candidate = raw[1:].strip() if slash else raw
    normalized = _normalize_text(candidate.replace(":", " ").replace("=", " "))
    tokens = normalized.split()
    if not tokens:
        return None

    if slash and len(tokens) <= 3:
        reasoning = _command_reasoning(" ".join(tokens))
        if reasoning:
            return {"reasoning": reasoning}
        model = _command_model(" ".join(tokens), settings)
        if model:
            return {"model": model}

    model_value = _command_value(tokens, {"modelo", "model", "perfil"}, allow_leading_verb=True)
    reasoning_value = _command_value(
        tokens,
        {"raciocinio", "reasoning", "pensamento", "analise", "modo"},
        allow_leading_verb=True,
    )
    controls: dict[str, str] = {}
    if model_value:
        model = _command_model(model_value, settings)
        if model:
            controls["model"] = model
    if reasoning_value:
        reasoning = _command_reasoning(reasoning_value)
        if reasoning:
            controls["reasoning"] = reasoning
    return controls or None


def _command_value(tokens: list[str], keywords: set[str], *, allow_leading_verb: bool = False) -> str:
    verbs = {"trocar", "mudar", "alterar", "usar", "use", "setar", "definir", "colocar"}
    starters = ("para", "pra", "pro", "por", "como", "em")
    for index, token in enumerate(tokens):
        if token not in keywords:
            continue
        if index > 0 and not (allow_leading_verb and any(item in verbs for item in tokens[:index])):
            continue
        value = tokens[index + 1 :]
        while value and value[0] in starters:
            value = value[1:]
        return " ".join(value)
    return ""


def _command_reasoning(value: str) -> str:
    normalized = _normalize_text(value)
    if normalized in {
        "auto",
        "automatico",
        "rapido",
        "fast",
        "fraco",
        "normal",
        "padrao",
        "medio",
        "forte",
        "strong",
        "extra",
        "extra forte",
        "xstrong",
    }:
        if normalized == "padrao":
            return "normal"
        return normalize_reasoning_mode(normalized)
    return ""


def _reasoning_label(value: str) -> str:
    return {
        "auto": "Automático",
        "fast": "Rápido",
        "normal": "Normal",
        "medium": "Médio",
        "strong": "Forte",
        "xstrong": "Extra forte",
    }.get(normalize_reasoning_mode(value), "Normal")


def _command_model(value: str, settings: Settings) -> str:
    raw = str(value or "").strip()
    normalized = _normalize_text(raw)
    if not normalized:
        return ""
    if "/" in raw:
        return raw

    public_models = {
        settings.economy_public_model.lower(): settings.economy_public_model,
        settings.pro_public_model.lower(): settings.pro_public_model,
        settings.ultra_public_model.lower(): settings.ultra_public_model,
        settings.ui_public_model.lower(): settings.ui_public_model,
        settings.auto_public_model.lower(): settings.auto_public_model,
    }
    if raw.lower() in public_models:
        return public_models[raw.lower()]
    if "auto" in normalized or "automatico" in normalized:
        return settings.auto_public_model
    if "4 7" in normalized or "4.7" in raw or "opus 4 7" in normalized:
        return settings.ultra_public_model
    if "4 5" in normalized or "4.5" in raw:
        return settings.pro_public_model
    if "ui" in normalized or "interface" in normalized:
        return settings.ui_public_model
    if "haiku" in normalized or "economy" in normalized or "economico" in normalized:
        return settings.economy_public_model
    if "sonnet" in normalized or normalized == "pro" or "padrao" in normalized:
        return settings.pro_public_model
    if "opus" in normalized or "ultra" in normalized or "avancado" in normalized:
        return settings.ultra_public_model
    return ""


async def _with_customer_power_tier(
    payload: dict[str, Any],
    app: FastAPI,
    auth: AuthContext,
) -> dict[str, Any]:
    if not auth.customer or auth.customer.allowed_model != "*":
        return payload
    if payload.get("__gateway_latency_fast_locked") or normalize_reasoning_mode(payload.get("__gateway_reasoning_mode")) == "fast":
        return {**payload, "model": app.state.settings.economy_public_model}

    settings = app.state.settings
    requested = str(payload.get("model") or "").strip()
    requested_lower = requested.lower()
    model_locked = bool(payload.get("__gateway_model_locked"))
    if "/" in requested_lower:
        return payload
    requested_auto = requested_lower in {"", settings.auto_public_model.lower(), "auto", "claude-code-auto"}
    if model_locked and not requested_auto and requested_lower in {
        settings.economy_public_model.lower(),
        settings.pro_public_model.lower(),
        settings.ultra_public_model.lower(),
        settings.ui_public_model.lower(),
    }:
        return payload
    if _is_explicit_low_power_model(requested_lower, settings):
        return payload

    snapshot = await _customer_usage_snapshot(app, auth)
    remaining = snapshot["today"].get("remaining_tokens")
    limit = auth.customer.daily_token_limit
    ratio = 1.0
    if isinstance(remaining, int) and limit > 0:
        ratio = max(0.0, min(1.0, remaining / limit))
    pacing_ratio = _customer_time_pacing_ratio(snapshot)

    if ratio <= 0.05 or pacing_ratio <= 0.65:
        target_model = settings.economy_public_model
    elif ratio <= 0.20 or pacing_ratio <= 0.90:
        target_model = settings.pro_public_model
    elif requested_lower == settings.ui_public_model.lower():
        target_model = settings.ui_public_model
    else:
        target_model = settings.ultra_public_model

    outgoing = dict(payload)
    outgoing["model"] = target_model
    outgoing["__gateway_customer_power_tier"] = {
        "remaining_token_ratio": round(ratio, 4),
        "time_pacing_ratio": round(pacing_ratio, 4),
        "selected_public_model": target_model,
    }
    return outgoing


async def _with_customer_latency_policy(
    payload: dict[str, Any],
    app: FastAPI,
    auth: AuthContext,
) -> dict[str, Any]:
    if not auth.customer:
        return payload

    snapshot = await _customer_usage_snapshot(app, auth)
    today = snapshot.get("today") if isinstance(snapshot, dict) else {}
    try:
        requests_today = int(today.get("requests") or 0) if isinstance(today, dict) else 0
    except (TypeError, ValueError):
        requests_today = 0

    requested_reasoning = normalize_reasoning_mode(payload.get("__gateway_reasoning_mode"))
    heavy_requested = requested_reasoning in {"medium", "strong", "xstrong"} or _requested_heavy_model(payload)
    heavy_allowed = (
        requests_today >= CUSTOMER_FORCED_FAST_REQUESTS
        and heavy_requested
        and _payload_requires_heavy_mode(payload)
    )

    outgoing = dict(payload)
    outgoing["__gateway_customer_requests_today"] = requests_today
    outgoing["__gateway_heavy_allowed"] = heavy_allowed
    if str(payload.get("model") or "").strip().lower() == app.state.settings.ultra_public_model.lower():
        outgoing["__gateway_heavy_allowed"] = True
        return outgoing
    if heavy_allowed:
        return outgoing

    outgoing["model"] = app.state.settings.economy_public_model
    outgoing["__gateway_hidden_reasoning_mode"] = outgoing.get("__gateway_reasoning_mode")
    outgoing["__gateway_reasoning_mode"] = "fast"
    outgoing["__gateway_latency_fast_locked"] = True
    outgoing["__gateway_latency_policy"] = (
        "first_10_customer_messages"
        if requests_today < CUSTOMER_FORCED_FAST_REQUESTS
        else "fast_default_heavy_gate"
    )
    return outgoing


def _requested_heavy_model(payload: dict[str, Any]) -> bool:
    requested = str(payload.get("model") or "").strip().lower()
    return any(marker in requested for marker in ("opus", "ultra", "4.7", "strong", "xstrong"))


def _payload_requires_heavy_mode(payload: dict[str, Any]) -> bool:
    text = _normalize_text(extract_prompt_text(payload))
    if not text:
        return False
    return any(term in text for term in HEAVY_MODE_REQUIRED_TERMS)


def _customer_time_pacing_ratio(snapshot: dict[str, Any]) -> float:
    customer = snapshot.get("customer") if isinstance(snapshot, dict) else {}
    today = snapshot.get("today") if isinstance(snapshot, dict) else {}
    if not isinstance(customer, dict) or not isinstance(today, dict):
        return 1.0
    if not customer.get("api_only"):
        return 1.0

    limit = int(customer.get("daily_token_limit") or 0)
    remaining = today.get("remaining_tokens")
    if limit <= 0 or not isinstance(remaining, int):
        return 1.0

    created_at = _parse_iso_datetime(str(customer.get("created_at") or ""))
    expires_at = _parse_iso_datetime(str(customer.get("expires_at") or ""))
    if not created_at or not expires_at or expires_at <= created_at:
        return 1.0

    now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
    total_seconds = max(1.0, (expires_at - created_at).total_seconds())
    remaining_seconds = max(0.0, (expires_at - now).total_seconds())
    expected_remaining_ratio = max(0.05, min(1.0, remaining_seconds / total_seconds))
    actual_remaining_ratio = max(0.0, min(1.0, remaining / limit))
    return actual_remaining_ratio / expected_remaining_ratio


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_explicit_low_power_model(model: str, settings: Settings) -> bool:
    if not model:
        return False
    economy_names = {
        settings.economy_public_model.lower(),
        "claude-code-economy",
        "haiku",
    }
    return model in economy_names or "economy" in model or "haiku" in model


def _with_public_model_identity(
    payload: dict[str, Any],
    public_model: str,
    settings: Settings,
) -> dict[str, Any]:
    label = _public_model_label(public_model, settings)
    today = datetime.now(ZoneInfo("America/Recife")).strftime("%Y-%m-%d")
    prompt = (
        f"Public model: {label}. "
        f"Current date for user-facing and factual work: {today}, timezone America/Recife. "
        f"Read the full conversation carefully and answer the user's latest message directly. "
        f"Do not respond with a generic readiness message when the user has already asked a concrete "
        f"question or given a concrete task. If the user asks about local files, folders, directories, "
        f"or workspace access and no file/shell tools are present in the request, say clearly that this "
        f"chat surface did not provide file tools, then give the exact next best path instead of pretending "
        f"to inspect the machine. "
        f"Keep Anthropic-compatible API behavior while being helpful, "
        f"direct, careful with code, concise by default, and explicit about files, commands, "
        f"verification, and uncertainty. Preserve Anthropic Messages API and tool-use compatibility. "
        f"Respond in the same language as the user's latest message; if the user writes Portuguese, "
        f"answer in Brazilian Portuguese and do not switch to English unless explicitly requested. "
        f"When the request includes tools for reading files, editing files, running commands, or inspecting "
        f"a workspace, use those tools to do the work instead of only describing what should be done. "
        f"Do not claim that code was changed unless the provided tool results confirm it. "
        f"Use polished Markdown for user-facing explanations: short bold section titles, useful bullets, "
        f"tables when they make comparison easier, blockquote callouts for important highlights, and a "
        f"warmer practical voice with concrete next steps. For plans, comparisons, timelines, diagnostics, "
        f"or project status, prefer a more visual answer with compact tables, clearly labeled sections, "
        f"progress-style wording, and varied emphasis instead of one long paragraph. "
        f"Do not over-format tiny answers, and never put Markdown markers around every word. "
        f"Act with strong execution autonomy: when the user has given a reasonable goal, choose sensible "
        f"project-consistent defaults and proceed instead of asking them to pick between options. State "
        f"brief assumptions only when helpful. Ask a clarifying question only when blocked by missing "
        f"credentials, irreversible destructive actions, safety/legal/financial risk, or a preference that "
        f"materially changes the result. "
        f"For frontend/UI tasks, build production-quality interfaces: polished hierarchy, responsive "
        f"layout, reusable components, tasteful motion, accurate copy, and visual choices that do not "
        f"look generic or AI-generated. For factual/current people, brands, products, dates, or places, "
        f"verify with available tools before writing confident details. Whenever the answer depends on "
        f"information that can change over time, including latest news, current prices, schedules, laws, "
        f"software versions, company data, sports, weather, or public figures, search or otherwise verify "
        f"fresh information before answering. If no browsing/search tool is available in the current "
        f"environment, say that you cannot verify live data instead of guessing. "
        f"If the user asks what model is being used, answer only with {label}. "
        f"Do not mention internal routing providers or gateway implementation details such as "
        f"DeepSeek, Kimi, StepFun, Tencent, OpenRouter, OpenAI helper, or hidden agents "
        f"unless the user explicitly asks for technical routing details."
    )
    return _append_system_prompt(payload, prompt)


def _with_automatic_skills(payload: dict[str, Any], decision: RouteDecision) -> dict[str, Any]:
    prompt_text = extract_prompt_text({key: value for key, value in payload.items() if key != "system"})
    skills = select_skills(prompt_text, decision.task_type)
    return _append_system_prompt(payload, render_skill_prompt(skills))


async def _with_openai_execution_guidance(
    app: FastAPI,
    auth: AuthContext,
    payload: dict[str, Any],
    decision: RouteDecision,
) -> dict[str, Any]:
    settings = app.state.settings
    if not _allow_openai_helper(auth, settings) or not app.state.openai_helper:
        return payload
    if payload_has_tool_contract(payload) or not _needs_internal_guidance(decision):
        return payload

    wants_design = settings.enable_openai_design_director and (
        decision.task_type == "frontend" or decision.mode in {"ultra", "ui"}
    )
    wants_decision = settings.enable_openai_decision_director and (
        decision.mode in {"pro", "ultra", "ui"} and decision.task_type != "explanation"
    )
    if not wants_design and not wants_decision:
        return payload

    instructions = OPENAI_DECISION_DIRECTOR_PROMPT
    if wants_design:
        instructions = f"{OPENAI_DECISION_DIRECTOR_PROMPT}\n\n{OPENAI_DESIGN_DIRECTOR_PROMPT}"

    try:
        guidance = await app.state.openai_helper.generate_text(
            instructions=instructions,
            input_text=_execution_director_input(payload, decision),
            max_output_tokens=min(settings.openai_helper_max_output_tokens, 900),
        )
    except Exception:
        return payload

    guidance = guidance.strip()
    if not guidance:
        return payload

    prompt = (
        "Internal execution guidance to apply silently before answering. "
        "Use it to choose defaults, proceed with the best path, and reduce unnecessary questions. "
        "Do not mention this guidance to the user.\n"
        f"{guidance[:4000]}"
    )
    return _append_system_prompt(payload, prompt)


def _execution_director_input(payload: dict[str, Any], decision: RouteDecision) -> str:
    preview = extract_prompt_text(payload)[:12000]
    return (
        f"Public model: {decision.public_model}\n"
        f"Mode: {decision.mode}\n"
        f"Task type: {decision.task_type}\n"
        f"Complexity: {decision.complexity}\n\n"
        f"User/project context:\n{preview}"
    )


async def _with_gemini_code_guidance(
    app: FastAPI,
    payload: dict[str, Any],
    decision: RouteDecision,
) -> dict[str, Any]:
    settings = app.state.settings
    if not settings.enable_gemini_code_helper or not settings.openrouter_api_key:
        return payload
    if payload_has_tool_contract(payload) or not _needs_internal_guidance(decision):
        return payload
    if decision.use_orchestration:
        return payload
    if decision.mode == "economy" or decision.task_type == "explanation":
        return payload
    if decision.task_type not in {
        "architecture",
        "debugging",
        "file_edit",
        "frontend",
        "review",
        "simple_code",
        "testing",
    }:
        return payload

    helper_payload = {
        "model": settings.gemini_code_helper_agent,
        "max_tokens": 600,
        "stream": False,
        "system": GEMINI_CODE_HELPER_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": _execution_director_input(payload, decision),
            }
        ],
    }
    try:
        response = await app.state.openrouter.complete_messages(
            helper_payload,
            settings.gemini_code_helper_agent,
        )
    except Exception:
        return payload

    guidance = _extract_text_blocks(response).strip()
    if not guidance:
        return payload
    return _append_system_prompt(
        payload,
        (
            "Internal Gemini coding guidance to apply silently before answering. "
            "Use it to improve code structure, edge cases, and verification. "
            "Do not mention this guidance to the user.\n"
            f"{guidance[:3000]}"
        ),
    )


def _needs_internal_guidance(decision: RouteDecision) -> bool:
    return decision.complexity in {"high", "critical"} or decision.task_type in {
        "architecture",
        "debugging",
        "file_edit",
        "frontend",
        "review",
        "testing",
    }


def _extract_text_blocks(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    content = response.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "\n".join(chunks)


async def _with_web_research(
    app: FastAPI,
    auth: AuthContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    decision = decide_web_search(payload, app.state.settings, auth)
    outgoing = dict(payload)
    outgoing["__gateway_web_search"] = decision.to_dict()
    if not decision.should_search:
        return outgoing

    if not decision.enabled or not app.state.web_search:
        return _append_system_prompt(outgoing, web_search_unavailable_context(decision))

    prompt_text = extract_prompt_text(payload)
    try:
        result = await asyncio.wait_for(
            app.state.web_search.search(prompt_text, required=True),
            timeout=max(0.05, float(app.state.settings.web_search_timeout_seconds or 8.0)),
        )
    except Exception:
        return _append_system_prompt(outgoing, web_search_unavailable_context(decision))

    outgoing["__gateway_web_search_result"] = result.to_dict()
    if not result.summary and not result.sources:
        unavailable = WebSearchDecision(
            policy=decision.policy,
            enabled=decision.enabled,
            should_search=decision.should_search,
            reason="empty_result",
        )
        return _append_system_prompt(outgoing, web_search_unavailable_context(unavailable))
    return _append_system_prompt(outgoing, web_search_context(result))


def _web_search_debug(payload: dict[str, Any], settings: Settings, auth: AuthContext) -> dict[str, Any]:
    return decide_web_search(payload, settings, auth).to_dict()


def _web_search_status(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": settings.enable_web_search,
        "configured": bool(settings.openai_api_key or settings.openrouter_api_key),
        "provider": "openai" if settings.openai_api_key else "openrouter" if settings.openrouter_api_key else "",
        "model": settings.web_search_model,
        "openrouter_model": settings.web_search_openrouter_model or settings.fast_agent,
        "context_size": settings.web_search_context_size,
        "for_customers": settings.web_search_for_customers,
        "max_output_tokens": settings.web_search_max_output_tokens,
        "allowed_domains": list(settings.web_search_allowed_domains),
        "blocked_domains": list(settings.web_search_blocked_domains),
    }


def _production_readiness(app: FastAPI) -> dict[str, Any]:
    settings = app.state.settings
    return {
        "model_backend": bool(settings.vps_model_base_url and settings.vps_model_id),
        "strong_backend": bool(
            settings.vps_strong_model_base_url and settings.vps_strong_model_id
        ),
        "external_gateway": bool(settings.openrouter_api_key),
        "external_fallback": bool(
            settings.openrouter_emergency_fallback and settings.openrouter_api_key
        ),
        "web_search": bool(
            settings.enable_web_search and (settings.openai_api_key or settings.openrouter_api_key)
        ),
        "openai_helper": bool(settings.openai_api_key),
        "mercado_pago": bool(settings.mercado_pago_access_token),
        "mercado_pago_webhook_secret": bool(settings.mercado_pago_webhook_secret),
        "admin_password": bool(
            settings.admin_password or settings.admin_password_hash or app.state.account_store.admin_configured()
        ),
        "cors_restricted": settings.cors_allowed_origins != ("*",),
        "trusted_hosts_restricted": settings.trusted_hosts != ("*",),
        "openapi_private": not settings.expose_openapi,
        "persistent_storage": settings.account_data_file == settings.quota_data_file,
    }


def _admin_benchmark(app: FastAPI, auth: AuthContext) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rows.extend(_benchmark_system_rows(app))
    route_rows = _benchmark_route_rows(app, auth)
    rows.extend(route_rows)

    required_failures = [row for row in rows if row["status"] == "FAIL"]
    warnings = [row for row in rows if row["status"] == "WARN"]
    route_latencies = [row["latency_ms"] for row in route_rows if isinstance(row.get("latency_ms"), (int, float))]
    summary = {
        "status": "fail" if required_failures else "ok",
        "total": len(rows),
        "passed": len([row for row in rows if row["status"] == "OK"]),
        "failed": len(required_failures),
        "warnings": len(warnings),
        "route_median_ms": round(statistics.median(route_latencies), 1) if route_latencies else 0,
        "mode": "safe_router_only",
        "spends_credits": False,
    }
    return {
        "status": summary["status"],
        "generated_at": datetime.now(ZoneInfo("America/Recife")).isoformat(),
        "summary": summary,
        "results": rows,
        "advice": _benchmark_advice(rows),
    }


def _benchmark_system_rows(app: FastAPI) -> list[dict[str, Any]]:
    settings = app.state.settings
    readiness = _production_readiness(app)
    protected_margin = min(max(settings.customer_profit_margin, 0.50), 0.95)
    checks = [
        (
            "model_backend",
            "Backend de IA configurado",
            readiness["model_backend"],
            "required",
            "Configure o backend de IA antes de vender acesso.",
        ),
        (
            "external_fallback",
            "Fallback externo desativado",
            not readiness["external_fallback"],
            "warning",
            "Fallback externo deve permanecer desativado para evitar consumo inesperado.",
        ),
        (
            "web_search",
            "Pesquisa web configurada",
            readiness["web_search"],
            "warning",
            "Configure uma chave de busca web para buscar fontes atuais.",
        ),
        (
            "mercado_pago",
            "Mercado Pago configurado",
            readiness["mercado_pago"],
            "required",
            "Necessario para vender upgrades pagos.",
        ),
        (
            "mercado_pago_webhook_secret",
            "Webhook Mercado Pago assinado",
            readiness["mercado_pago_webhook_secret"],
            "required",
            "Configure MERCADO_PAGO_WEBHOOK_SECRET para validar x-signature.",
        ),
        (
            "admin_password",
            "Senha admin",
            readiness["admin_password"],
            "required",
            "Protege o painel administrativo.",
        ),
        (
            "cors_restricted",
            "CORS restrito",
            readiness["cors_restricted"],
            "required",
            "Evita uso do navegador por origens inesperadas.",
        ),
        (
            "trusted_hosts",
            "Trusted hosts restritos",
            readiness["trusted_hosts_restricted"],
            "required",
            "Use dominio explicito em producao.",
        ),
        (
            "persistent_storage",
            "SQLite persistente",
            readiness["persistent_storage"],
            "required",
            "Contas, compras e cotas devem ficar no mesmo banco persistente.",
        ),
        (
            "cost_guard",
            "Custo abaixo do alvo",
            settings.max_cost_ratio_vs_claude <= 0.50,
            "required",
            f"MAX_COST_RATIO_VS_CLAUDE={settings.max_cost_ratio_vs_claude:.2f}.",
        ),
        (
            "profit_margin",
            "Margem minima protegida",
            protected_margin >= 0.50,
            "required",
            f"Margem efetiva protegida: {protected_margin:.0%}.",
        ),
    ]
    rows: list[dict[str, Any]] = []
    for check_id, label, ok, severity, detail in checks:
        status = "OK" if ok else ("WARN" if severity == "warning" else "FAIL")
        rows.append(
            {
                "category": "setup",
                "id": check_id,
                "label": label,
                "status": status,
                "severity": severity,
                "detail": detail,
                "notes": "" if ok else detail,
            }
        )
    return rows


def _benchmark_route_rows(app: FastAPI, auth: AuthContext) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in BENCHMARK_CASES:
        started = time.perf_counter()
        payload = _prepare_payload(benchmark_payload(case), app.state.settings, auth, app.state.account_store)
        decision_obj = app.state.planner.plan(payload)
        decision = _public_route_decision(decision_obj, app.state.settings)
        web_search = _web_search_debug(payload, app.state.settings, auth)
        elapsed_ms = (time.perf_counter() - started) * 1000
        data = {
            **decision,
            "web_search_policy": web_search["policy"],
            "web_search_reason": web_search["reason"],
            "web_search_enabled": web_search["enabled"],
            "web_search_should_search": web_search["should_search"],
        }
        failures = benchmark_failures(case, data)
        effective_path = (data.get("cost_estimate") or {}).get("effective_path") or {}
        rows.append(
            {
                "category": "route",
                "id": case.id,
                "label": case.label,
                "status": "FAIL" if failures else "OK",
                "severity": "required",
                "latency_ms": round(elapsed_ms, 1),
                "mode": data.get("mode"),
                "task_type": data.get("task_type"),
                "complexity": data.get("complexity"),
                "selected_model": data.get("model_label"),
                "public_model": data.get("public_model"),
                "orchestration": data.get("use_orchestration"),
                "web_search": data.get("web_search_should_search"),
                "web_search_reason": data.get("web_search_reason"),
                "cost_ratio": effective_path.get("cost_ratio_vs_claude"),
                "within_budget": effective_path.get("within_budget"),
                "notes": "; ".join(failures),
            }
        )
    return rows


def _benchmark_advice(rows: list[dict[str, Any]]) -> list[str]:
    advice: list[str] = []
    if any(row["status"] == "FAIL" and row["category"] == "setup" for row in rows):
        advice.append("Corrija os itens FAIL de setup antes de vender acesso em producao.")
    if any(row["status"] == "FAIL" and row["category"] == "route" for row in rows):
        advice.append("Revise claude_gateway/routing.py: algum caso de roteamento saiu do esperado.")

    architecture = next((row for row in rows if row["id"] == "architecture_ultra"), None)
    if architecture and float(architecture.get("cost_ratio") or 0) > 0.45:
        advice.append("Architecture Ultra esta perto do limite de custo; considere revisor mais barato.")

    if not any(row["status"] == "FAIL" for row in rows):
        advice.append("Roteamento seguro: nao ha troca obrigatoria de modelo agora.")
    if any(row["status"] == "WARN" for row in rows):
        advice.append("Itens WARN nao bloqueiam o app, mas melhoram qualidade ou operacao quando configurados.")
    return advice


async def _generate_conversation_title(app: FastAPI, payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    user_messages = [
        _conversation_message_text(message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    first_user = next((message for message in user_messages if message), "")
    if not first_user or _literal_conversation_title(first_user):
        return ""
    if not app.state.openai_helper:
        return ""

    excerpt = "\n".join(
        f"{message.get('role', 'user')}: {_conversation_message_text(message)[:900]}"
        for message in messages[:8]
        if isinstance(message, dict)
    )
    try:
        title = await app.state.openai_helper.generate_text(
            instructions=(
                "Create a concise conversation title in Brazilian Portuguese. "
                "Infer the user's real intent instead of copying the first sentence. "
                "Use 2 to 7 words, title case, no quotes, no punctuation at the end. "
                "For greetings or tiny small talk, return an empty string."
            ),
            input_text=excerpt[:5000],
            max_output_tokens=40,
        )
    except Exception:
        return ""
    return _clean_generated_title(title)


def _conversation_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return " ".join(" ".join(parts).split())
    return " ".join(str(content or "").split())


def _literal_conversation_title(text: str) -> bool:
    cleaned = _normalize_text(text.replace("?", "").replace("!", ""))
    if len(cleaned.split()) <= 3:
        return True
    return cleaned in {
        "oi",
        "ola",
        "oi tudo bem",
        "ola tudo bem",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
    }


def _quick_local_answer(payload: dict[str, Any]) -> str | None:
    prompt = _normalize_text(_visible_last_user_message_text(payload).replace("?", "").replace("!", ""))
    if (
        not payload_has_tool_contract(payload)
        and ("diretorio" in prompt or "pasta" in prompt or "arquivo" in prompt)
        and (
            "acessar" in prompt
            or "ler" in prompt
            or "enxergar" in prompt
            or "consegue" in prompt
        )
    ):
        return (
            "Nesta conversa eu não recebi ferramentas de arquivo ou terminal, então não consigo acessar "
            "diretórios nem ler arquivos do seu computador por aqui. Para mexer no projeto de verdade, "
            "abra pelo Claude Code/Codex no diretório do projeto ou envie uma sessão com ferramentas de arquivo habilitadas."
        )
    if prompt in {
        "eae",
        "iae",
        "iai",
        "oi",
        "ola",
        "opa",
        "e ai",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "oi tudo bem",
        "ola tudo bem",
    }:
        return "Oi! Estou aqui. O que vamos resolver?"
    if prompt in {
        "quem e o presidente do brasil",
        "presidente do brasil",
        "qual e o presidente do brasil",
        "qual o presidente do brasil",
    }:
        return "O presidente do Brasil é Luiz Inácio Lula da Silva (Lula)."
    return None


def _with_simple_response_budget(
    payload: dict[str, Any],
    decision: RouteDecision,
    settings: Settings,
) -> dict[str, Any]:
    if payload_has_tool_contract(payload) or decision.use_orchestration:
        return payload
    if decision.complexity != "low" and decision.task_type != "explanation":
        return payload

    cap = max(64, int(settings.simple_request_max_output_tokens or 768))
    current = int(payload.get("max_tokens") or cap)
    if current <= cap:
        return payload
    return {**payload, "max_tokens": cap}


def _clean_generated_title(value: str) -> str:
    title = " ".join(value.replace("\n", " ").strip(" \t\"'`“”‘’.:;!-").split())
    if not title or title.lower() in {"empty", "vazio", "sem titulo", "sem título"}:
        return ""
    return title[:54].rstrip()


def _with_gateway_reasoning(payload: dict[str, Any], decision: Any) -> dict[str, Any]:
    outgoing = dict(payload)
    outgoing["__gateway_reasoning"] = _reasoning_effort_for_request(payload, decision)
    outgoing.pop("reasoning", None)
    outgoing.pop("include_reasoning", None)
    outgoing.pop("thinking", None)
    return outgoing


def _reasoning_effort_for_request(payload: dict[str, Any], decision: Any) -> str:
    if payload_has_tool_contract(payload) or _is_claude_code_payload(payload):
        return "none"

    if str(getattr(decision, "mode", "") or "").lower() == "ultra":
        return "high"

    mode = normalize_reasoning_mode(
        payload.get("__gateway_hidden_reasoning_mode")
        or payload.get("__gateway_reasoning_mode")
    )
    auto_default = bool(payload.get("__gateway_reasoning_auto_default"))
    if mode == "fast" and not auto_default:
        return "none"
    if mode == "medium":
        return "medium"
    if mode in {"strong", "xstrong"}:
        return "high"
    if mode == "normal":
        return "low"

    complexity = str(getattr(decision, "complexity", "") or "")
    task_type = str(getattr(decision, "task_type", "") or "")
    if complexity == "critical":
        return "high"
    if complexity == "high" or task_type in {"architecture", "debugging", "review", "testing"}:
        return "medium"
    return "none"


def _is_claude_code_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("__gateway_client") or "").strip().lower() == "claude-code"


CLAUDE_CODE_TOOL_NAMES = {
    "applypatch",
    "bash",
    "edit",
    "exitplanmode",
    "glob",
    "grep",
    "ls",
    "multiedit",
    "notebookedit",
    "read",
    "task",
    "todowrite",
    "webfetch",
    "write",
    "writefile",
    "readfile",
    "listfiles",
    "runtests",
}


def _is_claude_code_request(request: Request, payload: dict[str, Any] | None = None) -> bool:
    beta_header = request.headers.get("anthropic-beta", "")
    user_agent = request.headers.get("user-agent", "")
    if "claude-code" in beta_header.lower() or "claude-code" in user_agent.lower():
        return True
    return _looks_like_claude_code_tool_payload(payload)


def _looks_like_claude_code_tool_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = _normalize_tool_name(str(tool.get("name") or ""))
        if name in CLAUDE_CODE_TOOL_NAMES:
            return True
    return False


def _normalize_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _selected_model_identity_answer(
    payload: dict[str, Any],
    public_model: str,
    settings: Settings,
) -> str | None:
    prompt = _normalize_text(_visible_last_user_message_text(payload))
    identity_phrases = (
        "qual modelo e voce",
        "que modelo e voce",
        "qual modelo voce e",
        "voce e qual modelo",
        "qual modelo vc e",
        "qual modelo esta usando",
        "que modelo esta usando",
        "qual e o modelo",
        "qual seu modelo",
        "quem e voce",
    )
    if not any(phrase in prompt for phrase in identity_phrases):
        return None

    label = _public_model_label(public_model, settings)
    return label


def _last_user_message_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            return " ".join(parts)
        return str(content or "")
    return ""


def _visible_last_user_message_text(payload: dict[str, Any]) -> str:
    text = _last_user_message_text(payload)
    text = CLAUDE_CODE_SYSTEM_REMINDER_RE.sub("\n", text)
    text = CLAUDE_CODE_SESSION_RE.sub("\n", text)
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _normalize_text(value: str) -> str:
    ascii_text = normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.lower().split())


async def _stream_text_message(message: dict[str, Any]):
    text = ""
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                break

    start = {**message, "content": []}
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': start})}\n\n".encode()
    yield (
        "event: content_block_start\n"
        "data: "
        f"{json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}"
        "\n\n"
    ).encode()
    if text:
        yield (
            "event: content_block_delta\n"
            "data: "
            f"{json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}})}"
            "\n\n"
        ).encode()
    yield b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    yield (
        "event: message_delta\n"
        "data: "
        f"{json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': message.get('usage') or {}})}"
        "\n\n"
    ).encode()
    yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    yield b"event: data\ndata: [DONE]\n\n"


async def _iter_bytes(chunks: list[bytes]):
    for chunk in chunks:
        yield chunk


def _sse_response(chunks: Any) -> StreamingResponse:
    return StreamingResponse(
        chunks,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _public_model_stream(chunks: Any, public_model: str, on_usage: Callable[[dict[str, int]], None] | None = None):
    buffer = ""
    text_normalizer = _StreamTextNormalizer()
    visibility_filter = _StreamVisibilityFilter()
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", "replace")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            usage = _stream_event_usage(event)
            if usage and on_usage:
                on_usage(usage)
            rewritten = _rewrite_stream_event_model(event, public_model, text_normalizer, visibility_filter)
            if rewritten is not None:
                yield (rewritten + "\n\n").encode("utf-8")

    if buffer:
        usage = _stream_event_usage(buffer)
        if usage and on_usage:
            on_usage(usage)
        rewritten = _rewrite_stream_event_model(buffer, public_model, text_normalizer, visibility_filter)
        if rewritten is not None:
            yield rewritten.encode("utf-8")


async def _public_model_stream_with_budget_settlement(
    chunks: Any,
    public_model: str,
    *,
    app: FastAPI,
    reservation: CustomerReservation | AccountUsageReservation | None,
    payload: dict[str, Any],
    decision: Any,
):
    usage: dict[str, int] = {}

    def remember(next_usage: dict[str, int]) -> None:
        usage.update(next_usage)

    try:
        async for chunk in _public_model_stream(chunks, public_model, on_usage=remember):
            yield chunk
    except Exception:
        await _rollback_customer_budget(app, reservation)
        raise
    if usage:
        await _settle_customer_budget(app, reservation, payload, decision, {"usage": usage})


def _stream_event_usage(event: str) -> dict[str, int] | None:
    data_lines = [
        line.removeprefix("data:").strip()
        for line in str(event or "").splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _StreamTextNormalizer:
    def __init__(self) -> None:
        self.raw_text = ""
        self.cleaned_text = ""

    def delta_for(self, incoming: str) -> str:
        text = str(incoming or "")
        if not text:
            return ""
        if _safe_incremental_stream_delta(self.cleaned_text, text):
            self.raw_text += text
            self.cleaned_text += text
            return text
        self.raw_text = _merge_stream_text(self.raw_text, text)
        next_cleaned = clean_model_text(self.raw_text, strip=False)
        if _stream_text_equal(next_cleaned, self.cleaned_text) or _stream_text_endswith(self.cleaned_text, next_cleaned):
            return ""
        if _stream_text_startswith(next_cleaned, self.cleaned_text):
            delta = next_cleaned[len(self.cleaned_text):]
            self.cleaned_text = next_cleaned
            return delta

        overlap = _stream_overlap_length(self.cleaned_text, next_cleaned)
        delta = next_cleaned[overlap:] if overlap else next_cleaned
        self.cleaned_text = next_cleaned
        return delta


def _safe_incremental_stream_delta(current: str, incoming: str) -> bool:
    if not current:
        return False
    if not incoming:
        return False
    if "<think" in incoming.lower() or "</think" in incoming.lower():
        return False
    if _stream_text_startswith(incoming, current):
        return False
    stripped = incoming.lstrip()
    if stripped and stripped != incoming:
        if _stream_text_endswith(current, stripped):
            return False
        if _stream_overlap_length(current, stripped) >= 3:
            return False
    if incoming[:1].isspace() or current[-1:].isspace():
        return True
    if incoming[:1] in ".,;:!?)]}":
        return True
    if current[-1:] in "([{/\n":
        return True
    return False


class _StreamVisibilityFilter:
    def __init__(self) -> None:
        self.suppressed_indices: set[int] = set()
        self.index_map: dict[int, int] = {}
        self.next_index = 0

    def rewrite_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        event_type = str(payload.get("type") or "")
        index = payload.get("index")
        content_block = payload.get("content_block")

        if (
            event_type == "content_block_start"
            and isinstance(index, int)
            and isinstance(content_block, dict)
            and content_block.get("type") in {"thinking", "redacted_thinking"}
        ):
            self.suppressed_indices.add(index)
            return None

        if isinstance(index, int) and index in self.suppressed_indices:
            if event_type == "content_block_stop":
                self.suppressed_indices.discard(index)
            return None

        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("type") in {
            "thinking_delta",
            "signature_delta",
            "reasoning_delta",
        }:
            return None

        if isinstance(index, int):
            payload = dict(payload)
            payload["index"] = self._visible_index(index)
        return payload

    def _visible_index(self, original: int) -> int:
        if original not in self.index_map:
            self.index_map[original] = self.next_index
            self.next_index += 1
        return self.index_map[original]


def _merge_stream_text(current: str, incoming: str) -> str:
    if not current:
        return incoming
    if _stream_text_equal(incoming, current) or _stream_text_endswith(current, incoming):
        return current
    if _stream_text_startswith(incoming, current):
        return incoming

    overlap = _stream_overlap_length(current, incoming)
    if overlap >= 3 or (overlap > 0 and len(incoming) > overlap and incoming[overlap].isspace()):
        return current + incoming[overlap:]

    stripped = incoming.lstrip()
    if stripped and stripped != incoming:
        if _stream_text_endswith(current, stripped):
            return current
        stripped_overlap = _stream_overlap_length(current, stripped)
        if stripped_overlap >= 3:
            return current + stripped[stripped_overlap:]

    return current + incoming


def _stream_overlap_length(left: str, right: str) -> int:
    max_size = min(len(left), len(right))
    for size in range(max_size, 0, -1):
        if _stream_text_equal(left[-size:], right[:size]):
            return size
    return 0


def _stream_text_equal(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _stream_text_startswith(text: str, prefix: str) -> bool:
    return text[: len(prefix)].casefold() == prefix.casefold()


def _stream_text_endswith(text: str, suffix: str) -> bool:
    return text[-len(suffix) :].casefold() == suffix.casefold() if suffix else True


def _normalize_stream_payload_text(payload: dict[str, Any], normalizer: _StreamTextNormalizer) -> None:
    delta = payload.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        delta["text"] = normalizer.delta_for(delta["text"])
    elif isinstance(delta, str) and payload.get("type") == "response.output_text.delta":
        payload["delta"] = normalizer.delta_for(delta)

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        choice_delta = choice.get("delta")
        if not isinstance(choice_delta, dict):
            continue
        content = choice_delta.get("content")
        if isinstance(content, str):
            choice_delta["content"] = normalizer.delta_for(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    part["text"] = normalizer.delta_for(part["text"])


def _rewrite_stream_event_model(
    event: str,
    public_model: str,
    text_normalizer: _StreamTextNormalizer | None = None,
    visibility_filter: _StreamVisibilityFilter | None = None,
) -> str | None:
    lines = event.splitlines()
    rewritten: list[str] = []
    for line in lines:
        if not line.startswith("data:"):
            rewritten.append(line)
            continue

        prefix = "data:"
        value = line.removeprefix(prefix).strip()
        if not value or value == "[DONE]":
            rewritten.append(line)
            continue

        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        if isinstance(payload, dict):
            if visibility_filter:
                payload = visibility_filter.rewrite_payload(payload)
                if payload is None:
                    return None
            if isinstance(payload.get("message"), dict):
                payload["message"]["model"] = public_model
                payload["message"].pop("provider", None)
            if "model" in payload:
                payload["model"] = public_model
            payload.pop("provider", None)
            if text_normalizer:
                _normalize_stream_payload_text(payload, text_normalizer)

        rewritten.append(f"data: {json.dumps(payload)}")
    return "\n".join(rewritten)


def _append_system_prompt(payload: dict[str, Any], prompt: str) -> dict[str, Any]:
    outgoing = dict(payload)
    existing = outgoing.get("system")

    if not existing:
        outgoing["system"] = prompt
    elif isinstance(existing, str):
        outgoing["system"] = f"{existing}\n\n{prompt}"
    elif isinstance(existing, list):
        outgoing["system"] = [*existing, {"type": "text", "text": prompt}]
    else:
        outgoing["system"] = prompt

    return outgoing


def _public_model_label(public_model: str, settings: Settings) -> str:
    public = str(public_model or "").strip()
    lowered = public.lower()
    legacy_label = settings.legacy_public_model_label
    advanced_label = settings.public_model_label
    labels = {
        settings.economy_public_model.lower(): legacy_label,
        settings.pro_public_model.lower(): legacy_label,
        settings.ultra_public_model.lower(): advanced_label,
        settings.ui_public_model.lower(): legacy_label,
        settings.auto_public_model.lower(): legacy_label,
        "claude-code-economy": legacy_label,
        "claude-code-pro": legacy_label,
        "claude-code-ultra": advanced_label,
        "claude-code-ui": legacy_label,
        "claude-code-auto": legacy_label,
        "qwen-14b": legacy_label,
    }
    if lowered in labels:
        return labels[lowered]
    if "qwen" in lowered:
        return legacy_label
    if "ultra" in lowered or "opus" in lowered or "4.7" in lowered:
        return advanced_label
    return public if public.startswith("claude-code-") else legacy_label


def _with_public_response_model(response: dict[str, Any], public_model: str, settings: Settings) -> dict[str, Any]:
    outgoing = dict(response)
    outgoing["model"] = _public_model_label(public_model, settings)
    return outgoing


def _safe_max_tokens(payload: dict[str, Any], settings: Settings) -> int:
    try:
        if payload.get("max_tokens") is None:
            requested = min(4096, settings.max_request_output_tokens)
        else:
            requested = int(payload.get("max_tokens") or settings.max_request_output_tokens)
    except (TypeError, ValueError):
        requested = min(4096, settings.max_request_output_tokens)
    cap = settings.max_request_output_tokens
    if payload_has_tool_contract(payload) or _is_claude_code_payload(payload):
        cap = min(cap, max(256, int(settings.tool_request_output_tokens or 4096)))
    return max(1, min(requested, cap))


async def _reserve_customer_budget(
    app: FastAPI,
    auth: AuthContext,
    payload: dict[str, Any],
    decision: Any,
) -> CustomerReservation | AccountUsageReservation | None:
    if not auth.customer:
        return None
    account_reservation = await asyncio.to_thread(
        app.state.account_store.reserve_usage_for_token,
        auth.token,
        payload,
        decision,
    )
    if account_reservation:
        return account_reservation
    return await asyncio.to_thread(app.state.customer_usage.reserve, auth.customer, payload, decision)


async def _rollback_customer_budget(
    app: FastAPI,
    reservation: CustomerReservation | AccountUsageReservation | None,
) -> None:
    if isinstance(reservation, AccountUsageReservation):
        await asyncio.to_thread(app.state.account_store.rollback_usage, reservation)
        return
    await asyncio.to_thread(app.state.customer_usage.rollback, reservation)


async def _settle_customer_budget(
    app: FastAPI,
    reservation: CustomerReservation | AccountUsageReservation | None,
    payload: dict[str, Any],
    decision: Any,
    response: dict[str, Any],
) -> None:
    if reservation is None:
        return
    actual_tokens = actual_reserved_tokens_from_response(response, payload, app.state.settings, decision)
    if actual_tokens is None:
        return
    if isinstance(reservation, AccountUsageReservation):
        await asyncio.to_thread(app.state.account_store.settle_usage, reservation, actual_tokens=actual_tokens)
        return
    actual_cost = estimate_request_cost_usd(
        payload,
        decision,
        app.state.settings,
        estimated_tokens=actual_tokens,
    )
    await asyncio.to_thread(
        app.state.customer_usage.settle,
        reservation,
        actual_tokens=actual_tokens,
        actual_cost_usd=actual_cost,
    )


async def _customer_usage_snapshot(app: FastAPI, auth: AuthContext) -> dict[str, Any]:
    account_snapshot = await asyncio.to_thread(app.state.account_store.usage_snapshot_for_token, auth.token)
    if account_snapshot:
        return account_snapshot
    return await asyncio.to_thread(app.state.customer_usage.snapshot_for, auth.customer)


def _public_usage_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    total = snapshot.get("total") if isinstance(snapshot, dict) else {}
    modes: dict[str, int] = {}
    if isinstance(total, dict) and isinstance(total.get("modes"), dict):
        modes = {str(key): int(value or 0) for key, value in total["modes"].items()}
    return {
        "total": {
            "requests": int(total.get("requests") or 0) if isinstance(total, dict) else 0,
            "input_tokens": int(total.get("input_tokens") or 0) if isinstance(total, dict) else 0,
            "output_tokens": int(total.get("output_tokens") or 0) if isinstance(total, dict) else 0,
            "modes": modes,
        }
    }


def _public_route_decision(decision: RouteDecision, settings: Settings) -> dict[str, Any]:
    effective_path = (decision.cost_estimate.get("effective_path") or {}) if decision.cost_estimate else {}
    pipeline = (decision.cost_estimate.get("pipeline") or {}) if decision.cost_estimate else {}
    return {
        "requested_model": decision.requested_model,
        "public_model": decision.public_model,
        "model_label": _public_model_label(decision.public_model, settings),
        "mode": decision.mode,
        "task_type": decision.task_type,
        "complexity": decision.complexity,
        "use_orchestration": decision.use_orchestration,
        "reason": decision.reason,
        "cost_estimate": {
            "effective_path": _public_cost_estimate(effective_path),
            "pipeline": _public_cost_estimate(pipeline),
        },
    }


def _public_cost_estimate(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "cost_ratio_vs_claude": value.get("cost_ratio_vs_claude"),
        "within_budget": value.get("within_budget"),
    }


def _raise_public_upstream_error(exc: OpenRouterError) -> None:
    status_code = exc.status_code if 400 <= int(exc.status_code or 502) < 600 else 502
    raise HTTPException(
        status_code=status_code,
        detail="Model backend request failed. Try again shortly.",
    ) from exc


def _allow_openai_helper(auth: AuthContext, settings: Settings) -> bool:
    if not settings.openai_api_key:
        return False
    if auth.customer and not settings.openai_helper_for_customers:
        return False
    return True


def _rate_limit(request: Request, app: FastAPI, namespace: str, limit: int) -> None:
    app.state.rate_limiter.check(
        rate_limit_key(request, namespace, app.state.settings),
        limit=limit,
        window_seconds=app.state.settings.rate_limit_window_seconds,
    )


def _rate_limit_public_auth(app: FastAPI, payload: dict[str, Any]) -> None:
    login = ""
    if isinstance(payload, dict):
        login = str(payload.get("login") or "").strip().lower()[:254]
    key = f"auth:login:{login or 'unknown'}"
    app.state.rate_limiter.check(
        key,
        limit=app.state.settings.auth_rate_limit,
        window_seconds=app.state.settings.rate_limit_window_seconds,
    )


def _require_admin(request: Request, settings: Settings) -> AuthContext:
    auth = require_gateway_auth(request, settings)
    if auth.kind != "admin":
        raise HTTPException(status_code=403, detail="Admin token required.")
    return auth


def _require_model_access(request: Request, settings: Settings) -> AuthContext:
    auth = require_gateway_auth(request, settings)
    if auth.kind == "admin" and not settings.allow_admin_model_access:
        raise HTTPException(status_code=403, detail="Customer API token required.")
    return auth


def _require_customer(request: Request, settings: Settings) -> AuthContext:
    auth = require_gateway_auth(request, settings)
    if not auth.is_customer:
        raise HTTPException(status_code=403, detail="Customer token required.")
    return auth


app = create_app()


def run() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    reload_enabled = os.getenv("RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run("claude_gateway.main:app", host=host, port=port, reload=reload_enabled)


if __name__ == "__main__":
    run()
