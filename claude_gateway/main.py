from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from unicodedata import normalize
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .accounts import AccountStore
from .budget import CLAUDE_BASELINE_MODEL, CostPolicy
from .auth import AuthContext, client_ip_for_debug, require_gateway_auth
from .anthropic import build_text_message
from .config import Settings, get_settings
from .conversations import ConversationStore
from .customers import CustomerReservation, CustomerUsageStore, clamp_customer_payload
from .openai_client import OpenAIHelperClient
from .openrouter import OpenRouterClient, OpenRouterError
from .orchestrator import MessageOrchestrator
from .routing import RoutePlanner, extract_prompt_text, model_profiles, payload_has_tool_contract
from .security import InMemoryRateLimiter, SecurityHeadersMiddleware, rate_limit_key, verify_admin_login
from .support import SupportStore
from .usage import UsageStore

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontier"


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[Settings], OpenRouterClient] | None = None,
    openai_helper_factory: Callable[[Settings], OpenAIHelperClient] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    app = FastAPI(title="Claude Code", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = resolved_settings
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.usage = UsageStore()
    app.state.customer_usage = CustomerUsageStore(resolved_settings)
    app.state.account_store = AccountStore(resolved_settings)
    app.state.conversation_store = ConversationStore(resolved_settings)
    app.state.support_store = SupportStore(resolved_settings)
    app.state.planner = RoutePlanner(resolved_settings)
    factory = client_factory or OpenRouterClient
    app.state.openrouter = factory(resolved_settings)
    helper_factory = openai_helper_factory or OpenAIHelperClient
    app.state.openai_helper = helper_factory(resolved_settings) if resolved_settings.openai_api_key else None
    app.state.orchestrator = MessageOrchestrator(
        app.state.openrouter,
        app.state.planner,
        app.state.usage,
        app.state.openai_helper,
    )
    app.add_middleware(SecurityHeadersMiddleware)
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

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "openrouter_configured": bool(app.state.settings.openrouter_api_key),
            "openai_helper_configured": bool(app.state.settings.openai_api_key),
            "orchestration_enabled": app.state.settings.enable_agent_orchestration,
            "cost_target": {
                "baseline_model": CLAUDE_BASELINE_MODEL,
                "max_cost_ratio_vs_claude": app.state.settings.max_cost_ratio_vs_claude,
                "minimum_savings_vs_claude": 1 - app.state.settings.max_cost_ratio_vs_claude,
            },
        }

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        auth = require_gateway_auth(request, app.state.settings)
        cost_policy = CostPolicy(
            max_ratio_vs_claude=app.state.settings.max_cost_ratio_vs_claude,
        )
        profiles = model_profiles(app.state.settings)
        if auth.customer and auth.customer.allowed_model != "*":
            profiles = [profile for profile in profiles if profile.id == auth.customer.allowed_model]
        return {
            "data": [
                {
                    "id": profile.id,
                    "type": "model",
                    "display_name": profile.display_name,
                    "description": profile.description,
                    "cost_target": cost_policy.estimate(
                        app.state.planner._openrouter_model_for_mode(
                            profile.mode,
                            "simple_code",
                            "medium",
                        )
                    ).to_dict(),
                }
                for profile in profiles
            ]
        }

    @app.get("/v1/budget")
    async def budget(request: Request) -> dict[str, Any]:
        require_gateway_auth(request, app.state.settings)
        cost_policy = CostPolicy(
            max_ratio_vs_claude=app.state.settings.max_cost_ratio_vs_claude,
        )
        models = {
            "router_agent": app.state.settings.router_agent,
            "cheap_code_agent": app.state.settings.cheap_code_agent,
            "code_agent": app.state.settings.code_agent,
            "reasoning_agent": app.state.settings.reasoning_agent,
            "ui_agent": app.state.settings.ui_agent,
            "fast_agent": app.state.settings.fast_agent,
            "premium_fallback": app.state.settings.premium_fallback,
            "ultra_fallback": app.state.settings.ultra_fallback,
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
            "max_request_output_tokens": app.state.settings.max_request_output_tokens,
            "models": {
                role: cost_policy.estimate(model).to_dict() for role, model in models.items()
            },
        }

    @app.post("/v1/messages")
    async def messages(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = require_gateway_auth(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

        payload = _prepare_payload(payload, app.state.settings, auth)
        decision = app.state.planner.plan(payload)
        identity_answer = _selected_model_identity_answer(payload, decision.public_model, app.state.settings)
        payload = _with_gateway_reasoning(payload, decision)
        payload = _with_public_model_identity(payload, decision.public_model, app.state.settings)
        payload["__gateway_route_decision"] = decision
        if identity_answer:
            app.state.usage.record_request(decision)
            message = build_text_message(
                decision.public_model,
                identity_answer,
                usage={"input_tokens": 0, "output_tokens": len(identity_answer.split())},
            )
            if payload.get("stream"):
                return StreamingResponse(
                    _stream_text_message(message),
                    media_type="text/event-stream",
                )
            return JSONResponse(message)

        reservation = _reserve_customer_budget(app, auth, payload, decision)
        if payload.get("stream"):
            if decision.use_orchestration:
                try:
                    response, _ = await app.state.orchestrator.complete(
                        {**payload, "stream": False},
                        allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
                    )
                except OpenRouterError as exc:
                    app.state.customer_usage.rollback(reservation)
                    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
                except Exception:
                    app.state.customer_usage.rollback(reservation)
                    raise

                return StreamingResponse(
                    _stream_text_message(response),
                    media_type="text/event-stream",
                )

            if not app.state.settings.openrouter_api_key:
                app.state.customer_usage.rollback(reservation)
                raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured.")
            app.state.usage.record_request(decision)
            return StreamingResponse(
                _public_model_stream(
                    app.state.openrouter.stream_messages(payload, decision.selected_openrouter_model),
                    decision.public_model,
                ),
                media_type="text/event-stream",
            )

        try:
            response, _ = await app.state.orchestrator.complete(
                payload,
                allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
            )
        except OpenRouterError as exc:
            app.state.customer_usage.rollback(reservation)
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except Exception:
            app.state.customer_usage.rollback(reservation)
            raise

        return JSONResponse(response)

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

    @app.post("/v1/admin/login")
    async def admin_login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, str]:
        _rate_limit(request, app, "admin-auth", app.state.settings.auth_rate_limit)
        _require_admin(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        verify_admin_login(payload, app.state.settings)
        return {"status": "ok"}

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
        return {"ticket": app.state.support_store.current_for_customer(auth.token)}

    @app.post("/v1/support/tickets")
    async def open_support_ticket(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = _require_customer(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
        return JSONResponse({"ticket": app.state.support_store.open_ticket(auth.token, payload)})

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
        return JSONResponse({"ticket": app.state.support_store.customer_message(auth.token, ticket_id, payload)})

    @app.get("/v1/admin/support/tickets")
    async def list_support_tickets(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return app.state.support_store.list_admin_tickets()

    @app.post("/v1/admin/support/tickets/{ticket_id}/claim")
    async def claim_support_ticket(ticket_id: str, request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return JSONResponse({"ticket": app.state.support_store.claim_ticket(ticket_id)})

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
        return JSONResponse({"ticket": app.state.support_store.admin_message(ticket_id, payload)})

    @app.post("/v1/admin/support/tickets/{ticket_id}/close")
    async def close_support_ticket(ticket_id: str, request: Request) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        _require_admin(request, app.state.settings)
        return JSONResponse({"ticket": app.state.support_store.close_ticket(ticket_id)})

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
        return JSONResponse({"conversation": app.state.conversation_store.save_for_customer(auth.token, payload)})

    @app.get("/v1/usage")
    async def usage(request: Request) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = require_gateway_auth(request, app.state.settings)
        if auth.customer:
            return app.state.customer_usage.snapshot_for(auth.customer)
        return app.state.usage.snapshot()

    @app.post("/v1/router/debug")
    async def router_debug(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = require_gateway_auth(request, app.state.settings)
        payload = _prepare_payload(payload, app.state.settings, auth)
        return app.state.planner.plan(payload).to_dict()

    @app.post("/v1/agent/run")
    async def agent_run(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        _rate_limit(request, app, "api", app.state.settings.api_rate_limit)
        auth = require_gateway_auth(request, app.state.settings)
        payload = _prepare_payload(payload, app.state.settings, auth)
        if payload.get("stream"):
            payload = {**payload, "stream": False}

        decision = app.state.planner.plan(payload, force_orchestration=True)
        payload = _with_public_model_identity(payload, decision.public_model, app.state.settings)
        reservation = _reserve_customer_budget(app, auth, payload, decision)
        try:
            response, decision = await app.state.orchestrator.complete(
                payload,
                force_orchestration=True,
                allow_openai_helper=_allow_openai_helper(auth, app.state.settings),
            )
        except OpenRouterError as exc:
            app.state.customer_usage.rollback(reservation)
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except Exception:
            app.state.customer_usage.rollback(reservation)
            raise

        return JSONResponse({"decision": decision.to_dict(), "response": response})

    return app


def _mount_frontend(app: FastAPI) -> None:
    if not FRONTEND_DIR.exists():
        return

    app.mount("/frontier", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontier")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/frontier/app.html")

    @app.get("/app", include_in_schema=False)
    async def app_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "app.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_page() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "admin.html")


def _prepare_payload(
    payload: dict[str, Any],
    settings: Settings,
    auth: AuthContext,
) -> dict[str, Any]:
    prompt_text = extract_prompt_text(payload)
    if len(prompt_text) > settings.max_request_input_chars:
        raise HTTPException(
            status_code=413,
            detail="Request input is larger than MAX_REQUEST_INPUT_CHARS.",
        )

    limited = dict(payload)
    limited["max_tokens"] = _safe_max_tokens(limited, settings)
    if payload_has_tool_contract(limited):
        limited["max_tokens"] = max(
            limited["max_tokens"],
            min(settings.tool_request_output_tokens, settings.max_request_output_tokens),
        )
    if auth.customer:
        limited = clamp_customer_payload(limited, settings, auth.customer)
    return limited


def _with_public_model_identity(
    payload: dict[str, Any],
    public_model: str,
    settings: Settings,
) -> dict[str, Any]:
    label = _public_model_label(public_model, settings)
    today = datetime.now(ZoneInfo("America/Recife")).strftime("%Y-%m-%d")
    prompt = (
        f"Public compatibility profile: the user selected {label} for this chat. "
        f"Current date for user-facing and factual work: {today}, timezone America/Recife. "
        f"Match Anthropic Claude Code response behavior as closely as possible: be helpful, "
        f"direct, careful with code, concise by default, and explicit about files, commands, "
        f"verification, and uncertainty. Preserve Anthropic Messages API and tool-use compatibility. "
        f"For frontend/UI tasks, build production-quality interfaces: polished hierarchy, responsive "
        f"layout, reusable components, tasteful motion, accurate copy, and visual choices that do not "
        f"look generic or AI-generated. For factual/current people, brands, products, dates, or places, "
        f"verify with available tools before writing confident details. "
        f"If the user asks what model you are or what model is being used, answer with {label}. "
        f"Do not mention internal routing providers or gateway implementation details such as "
        f"DeepSeek, Kimi, StepFun, Tencent, Qwen, OpenRouter, OpenAI helper, or hidden agents "
        f"unless the user explicitly asks for technical routing details."
    )
    return _append_system_prompt(payload, prompt)


def _with_gateway_reasoning(payload: dict[str, Any], decision: Any) -> dict[str, Any]:
    outgoing = dict(payload)
    if decision.complexity == "critical" or decision.mode == "ultra":
        outgoing["__gateway_reasoning"] = "medium"
    elif decision.complexity == "high" or decision.task_type in {
        "architecture",
        "debugging",
        "frontend",
        "review",
    }:
        outgoing["__gateway_reasoning"] = "low"
    else:
        outgoing["__gateway_reasoning"] = "none"
    return outgoing


def _selected_model_identity_answer(
    payload: dict[str, Any],
    public_model: str,
    settings: Settings,
) -> str | None:
    prompt = _normalize_text(extract_prompt_text(payload))
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
    return f"Eu sou o {label}, o modelo selecionado neste chat."


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


async def _public_model_stream(chunks: Any, public_model: str):
    buffer = ""
    async for chunk in chunks:
        buffer += chunk.decode("utf-8", "replace")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            yield (_rewrite_stream_event_model(event, public_model) + "\n\n").encode("utf-8")

    if buffer:
        yield _rewrite_stream_event_model(buffer, public_model).encode("utf-8")


def _rewrite_stream_event_model(event: str, public_model: str) -> str:
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
            if isinstance(payload.get("message"), dict):
                payload["message"]["model"] = public_model
                payload["message"].pop("provider", None)
            if "model" in payload:
                payload["model"] = public_model
            payload.pop("provider", None)

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
    labels = {
        settings.economy_public_model: "Claude Haiku 4.5",
        settings.pro_public_model: "Claude Sonnet 4.6",
        settings.ultra_public_model: "Claude Opus 4.7",
        settings.ui_public_model: "Claude Code UI",
        settings.auto_public_model: "Claude Code Auto",
    }
    return labels.get(public_model, public_model)


def _safe_max_tokens(payload: dict[str, Any], settings: Settings) -> int:
    try:
        requested = int(payload.get("max_tokens") or settings.max_request_output_tokens)
    except (TypeError, ValueError):
        requested = settings.max_request_output_tokens
    return max(1, min(requested, settings.max_request_output_tokens))


def _reserve_customer_budget(
    app: FastAPI,
    auth: AuthContext,
    payload: dict[str, Any],
    decision: Any,
) -> CustomerReservation | None:
    if not auth.customer:
        return None
    return app.state.customer_usage.reserve(auth.customer, payload, decision)


def _allow_openai_helper(auth: AuthContext, settings: Settings) -> bool:
    if not settings.openai_api_key:
        return False
    if auth.customer and not settings.openai_helper_for_customers:
        return False
    return True


def _rate_limit(request: Request, app: FastAPI, namespace: str, limit: int) -> None:
    app.state.rate_limiter.check(
        rate_limit_key(request, namespace),
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
