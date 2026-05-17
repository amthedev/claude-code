from __future__ import annotations

from typing import Any, Callable

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .budget import CLAUDE_BASELINE_MODEL, CostPolicy
from .auth import require_gateway_auth
from .config import Settings, get_settings
from .openrouter import OpenRouterClient, OpenRouterError
from .orchestrator import MessageOrchestrator
from .routing import RoutePlanner, model_profiles
from .usage import UsageStore


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[Settings], OpenRouterClient] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    app = FastAPI(title="Claude Code OpenRouter Gateway", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.usage = UsageStore()
    app.state.planner = RoutePlanner(resolved_settings)
    factory = client_factory or OpenRouterClient
    app.state.openrouter = factory(resolved_settings)
    app.state.orchestrator = MessageOrchestrator(
        app.state.openrouter,
        app.state.planner,
        app.state.usage,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "openrouter_configured": bool(app.state.settings.openrouter_api_key),
            "orchestration_enabled": app.state.settings.enable_agent_orchestration,
            "cost_target": {
                "baseline_model": CLAUDE_BASELINE_MODEL,
                "max_cost_ratio_vs_claude": app.state.settings.max_cost_ratio_vs_claude,
                "minimum_savings_vs_claude": 1 - app.state.settings.max_cost_ratio_vs_claude,
            },
        }

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        require_gateway_auth(request, app.state.settings)
        cost_policy = CostPolicy(
            max_ratio_vs_claude=app.state.settings.max_cost_ratio_vs_claude,
        )
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
                for profile in model_profiles(app.state.settings)
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
            "models": {
                role: cost_policy.estimate(model).to_dict() for role, model in models.items()
            },
        }

    @app.post("/v1/messages")
    async def messages(request: Request, payload: dict[str, Any] = Body(...)) -> JSONResponse:
        require_gateway_auth(request, app.state.settings)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

        decision = app.state.planner.plan(payload)
        if payload.get("stream"):
            if not app.state.settings.openrouter_api_key:
                raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not configured.")
            app.state.usage.record_request(decision)
            return StreamingResponse(
                app.state.openrouter.stream_messages(payload, decision.selected_openrouter_model),
                media_type="text/event-stream",
            )

        try:
            response, _ = await app.state.orchestrator.complete(payload)
        except OpenRouterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        return JSONResponse(response)

    @app.get("/v1/usage")
    async def usage(request: Request) -> dict[str, Any]:
        require_gateway_auth(request, app.state.settings)
        return app.state.usage.snapshot()

    @app.post("/v1/router/debug")
    async def router_debug(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        require_gateway_auth(request, app.state.settings)
        return app.state.planner.plan(payload).to_dict()

    @app.post("/v1/agent/run")
    async def agent_run(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        require_gateway_auth(request, app.state.settings)
        if payload.get("stream"):
            payload = {**payload, "stream": False}

        try:
            response, decision = await app.state.orchestrator.complete(
                payload,
                force_orchestration=True,
            )
        except OpenRouterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        return JSONResponse({"decision": decision.to_dict(), "response": response})

    return app


app = create_app()


def run() -> None:
    uvicorn.run("claude_gateway.main:app", host="127.0.0.1", port=8787, reload=True)


if __name__ == "__main__":
    run()
