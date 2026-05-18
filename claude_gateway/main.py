from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .accounts import AccountStore
from .budget import CLAUDE_BASELINE_MODEL, CostPolicy
from .auth import AuthContext, client_ip_for_debug, require_gateway_auth
from .config import Settings, get_settings
from .customers import CustomerReservation, CustomerUsageStore, clamp_customer_payload
from .openai_client import OpenAIHelperClient
from .openrouter import OpenRouterClient, OpenRouterError
from .orchestrator import MessageOrchestrator
from .routing import RoutePlanner, extract_prompt_text, model_profiles
from .security import InMemoryRateLimiter, SecurityHeadersMiddleware, rate_limit_key, verify_admin_login
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
        reservation = _reserve_customer_budget(app, auth, payload, decision)
        if payload.get("stream"):
            if not app.state.settings.openrouter_api_key:
                app.state.customer_usage.rollback(reservation)
                raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured.")
            app.state.usage.record_request(decision)
            return StreamingResponse(
                app.state.openrouter.stream_messages(payload, decision.selected_openrouter_model),
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
    if auth.customer:
        limited = clamp_customer_payload(limited, settings, auth.customer)
    return limited


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


app = create_app()


def run() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    reload_enabled = os.getenv("RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run("claude_gateway.main:app", host=host, port=port, reload=reload_enabled)


if __name__ == "__main__":
    run()
