from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from claude_gateway.config import Settings
from claude_gateway.main import create_app


class FakeOpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        self.calls.append((model, payload))
        return {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": f"model={model}"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }

    async def stream_messages(self, payload: dict[str, Any], model: str):
        self.calls.append((model, payload))
        yield b"event: message_start\n"
        yield b"data: {}\n\n"


def make_settings() -> Settings:
    return Settings(
        gateway_api_keys=("test-token",),
        openrouter_api_key="test-openrouter-token",
        enable_agent_orchestration=True,
    )


class GatewayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(settings=make_settings(), client_factory=FakeOpenRouterClient)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer test-token"}

    def test_models_requires_auth(self) -> None:
        response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 401)

        response = self.client.get("/v1/models", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        model_ids = {model["id"] for model in response.json()["data"]}
        self.assertIn("claude-code-pro", model_ids)

    def test_auto_routes_frontend_to_ui(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-auto",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Crie um dashboard React bonito"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "ui")
        self.assertEqual(data["selected_openrouter_model"], "moonshotai/kimi-k2.6")
        self.assertTrue(data["cost_estimate"]["effective_path"]["within_budget"])
        self.assertLessEqual(
            data["cost_estimate"]["effective_path"]["cost_ratio_vs_claude"],
            0.5,
        )

    def test_tool_calls_are_proxied_without_orchestration(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 256,
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "Leia um arquivo"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"][0]["text"], "model=deepseek/deepseek-v4-pro")
        self.assertEqual(len(self.app.state.openrouter.calls), 1)

    def test_non_streaming_pro_uses_agent_pipeline(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Corrija esse bug difícil"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "claude-code-pro")
        self.assertGreaterEqual(len(self.app.state.openrouter.calls), 5)

    def test_ultra_pipeline_uses_extra_budget_safe_candidate(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Corrija esse bug crítico de auth"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "claude-code-ultra")
        called_models = [model for model, _payload in self.app.state.openrouter.calls]
        self.assertGreaterEqual(len(called_models), 6)
        self.assertNotIn("anthropic/claude-sonnet-4.6", called_models)
        self.assertNotIn("anthropic/claude-opus-4.7", called_models)

    def test_budget_endpoint_reports_default_models_under_target(self) -> None:
        response = self.client.get("/v1/budget", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["max_cost_ratio_vs_claude"], 0.5)
        self.assertFalse(data["allow_premium_fallback"])
        for model in data["models"].values():
            self.assertTrue(model["within_budget"], model)
            self.assertLessEqual(model["cost_ratio_vs_claude"], 0.5)

    def test_over_budget_internal_model_is_replaced(self) -> None:
        settings = make_settings()
        settings.code_agent = "anthropic/claude-sonnet-4.6"
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        response = client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Implemente uma API"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotEqual(data["selected_openrouter_model"], "anthropic/claude-sonnet-4.6")
        self.assertTrue(data["cost_estimate"]["effective_path"]["within_budget"])

    def test_external_model_request_is_budget_routed_by_default(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "anthropic/claude-opus-4.7",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Implemente uma API"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotEqual(data["mode"], "direct")
        self.assertNotEqual(data["selected_openrouter_model"], "anthropic/claude-opus-4.7")
        self.assertTrue(data["cost_estimate"]["effective_path"]["within_budget"])

    def test_direct_external_model_can_be_enabled_for_admin(self) -> None:
        settings = make_settings()
        settings.allow_direct_external_models = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        response = client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "anthropic/claude-opus-4.7",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Implemente uma API"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "direct")
        self.assertEqual(data["selected_openrouter_model"], "anthropic/claude-opus-4.7")

    def test_customer_token_forces_allowed_model_and_reports_own_usage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.customer_accounts = (
                "customer-token|Maria|149.90|60000|claude-code-economy|true"
            )
            settings.quota_data_file = f"{tmpdir}/usage.json"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)
            customer_headers = {"Authorization": "Bearer customer-token"}

            response = client.post(
                "/v1/messages",
                headers=customer_headers,
                json={
                    "model": "claude-opus-4.7",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Explique este trecho"}],
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["content"][0]["text"], "model=deepseek/deepseek-v4-flash")

            usage = client.get("/v1/usage", headers=customer_headers)
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()["customer"]["allowed_model"], "claude-code-economy")
            self.assertGreater(usage.json()["today"]["requests"], 0)

    def test_customer_quota_blocks_before_upstream_call(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.customer_accounts = "tiny-token|Tiny|49.90|10|claude-code-pro|true"
            settings.quota_data_file = f"{tmpdir}/usage.json"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            response = client.post(
                "/v1/messages",
                headers={"Authorization": "Bearer tiny-token"},
                json={
                    "model": "claude-code-pro",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Crie uma funcao"}],
                },
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(app.state.openrouter.calls, [])

    def test_streaming_returns_sse(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Explique a função"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())
        self.assertIn(b"event: message_start", body)


if __name__ == "__main__":
    unittest.main()
