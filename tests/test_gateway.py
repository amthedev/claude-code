from __future__ import annotations

import asyncio
import unittest
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from claude_gateway.config import Settings
from claude_gateway.main import create_app
from claude_gateway.openrouter import OpenRouterClient


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


class FakeOpenAIHelper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

    async def generate_text(
        self,
        *,
        instructions: str,
        input_text: str,
        max_output_tokens: int | None = None,
    ) -> str:
        self.calls.append(
            {
                "instructions": instructions,
                "input_text": input_text,
                "max_output_tokens": max_output_tokens,
            }
        )
        return "Use stricter validation and explain edge cases."


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
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])

    def test_admin_login_is_checked_on_server(self) -> None:
        settings = make_settings()
        settings.admin_password = "admin-test-password"
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.post(
            "/v1/admin/login",
            headers=self.headers,
            json={"login": "reidelas", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 403)

        response = client.post(
            "/v1/admin/login",
            headers=self.headers,
            json={"login": "reidelas", "password": "admin-test-password"},
        )
        self.assertEqual(response.status_code, 200)

    def test_trusted_admin_ip_can_open_admin_routes_without_token(self) -> None:
        settings = make_settings()
        settings.gateway_api_keys = ("real-admin-token",)
        settings.admin_trusted_ips = ("177.200.246.8",)
        settings.trust_proxy_headers = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.get(
            "/v1/admin/accounts",
            headers={"X-Forwarded-For": "177.200.246.8"},
        )
        self.assertEqual(response.status_code, 200)

    def test_trusted_admin_ip_accepts_cloudflare_header(self) -> None:
        settings = make_settings()
        settings.gateway_api_keys = ("real-admin-token",)
        settings.admin_trusted_ips = ("177.200.246.8",)
        settings.trust_proxy_headers = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.get(
            "/v1/admin/accounts",
            headers={"CF-Connecting-IP": "177.200.246.8"},
        )
        self.assertEqual(response.status_code, 200)

    def test_admin_ip_check_reports_detected_proxy_ip(self) -> None:
        settings = make_settings()
        settings.admin_trusted_ips = ("177.200.246.8",)
        settings.trust_proxy_headers = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.get(
            "/v1/admin/ip-check",
            headers={"CF-Connecting-IP": "177.200.246.8"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["detected_ip"], "177.200.246.8")
        self.assertTrue(response.json()["trusted"])

    def test_support_queue_allows_one_active_ticket(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            gift_one = client.post(
                "/v1/admin/gift-cards",
                headers=self.headers,
                json={"code": "SUPPORT-ONE", "plan": "Pro", "price": 149.9, "model": "sonnet"},
            ).json()["giftCard"]
            gift_two = client.post(
                "/v1/admin/gift-cards",
                headers=self.headers,
                json={"code": "SUPPORT-TWO", "plan": "Pro", "price": 149.9, "model": "sonnet"},
            ).json()["giftCard"]
            account_one = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Um",
                    "login": "um@example.com",
                    "password": "secret-one",
                    "giftCard": gift_one["code"],
                },
            ).json()["account"]
            account_two = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Dois",
                    "login": "dois@example.com",
                    "password": "secret-two",
                    "giftCard": gift_two["code"],
                },
            ).json()["account"]

            customer_one_headers = {"Authorization": f"Bearer {account_one['apiToken']}"}
            customer_two_headers = {"Authorization": f"Bearer {account_two['apiToken']}"}
            ticket_one = client.post(
                "/v1/support/tickets",
                headers=customer_one_headers,
                json={"message": "Preciso de ajuda"},
            ).json()["ticket"]
            ticket_two = client.post(
                "/v1/support/tickets",
                headers=customer_two_headers,
                json={"message": "Estou na fila"},
            ).json()["ticket"]

            queue = client.get("/v1/admin/support/tickets", headers=self.headers).json()
            self.assertEqual([ticket["id"] for ticket in queue["waiting"]], [ticket_one["id"], ticket_two["id"]])

            response = client.post(f"/v1/admin/support/tickets/{ticket_one['id']}/claim", headers=self.headers)
            self.assertEqual(response.status_code, 200)

            response = client.post(f"/v1/admin/support/tickets/{ticket_two['id']}/claim", headers=self.headers)
            self.assertEqual(response.status_code, 409)

            response = client.post(
                f"/v1/admin/support/tickets/{ticket_one['id']}/messages",
                headers=self.headers,
                json={"message": "Vou te ajudar agora."},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["ticket"]["messages"][-1]["sender"], "support")

            response = client.post(f"/v1/admin/support/tickets/{ticket_one['id']}/close", headers=self.headers)
            self.assertEqual(response.status_code, 200)

            response = client.post(f"/v1/admin/support/tickets/{ticket_two['id']}/claim", headers=self.headers)
            self.assertEqual(response.status_code, 200)

    def test_cors_allows_local_admin_origin_when_configured(self) -> None:
        settings = make_settings()
        settings.cors_allowed_origins = ("http://127.0.0.1:8787",)
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.options(
            "/v1/admin/accounts",
            headers={
                "Origin": "http://127.0.0.1:8787",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://127.0.0.1:8787")

    def test_trusted_admin_ip_does_not_bypass_non_admin_routes(self) -> None:
        settings = make_settings()
        settings.gateway_api_keys = ("real-admin-token",)
        settings.admin_trusted_ips = ("177.200.246.8",)
        settings.trust_proxy_headers = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.get(
            "/v1/models",
            headers={"X-Forwarded-For": "177.200.246.8"},
        )
        self.assertEqual(response.status_code, 401)

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
        self.assertEqual(response.json()["content"][0]["text"], "model=qwen/qwen3-coder-flash")
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

    def test_openai_helper_can_review_agent_pipeline_for_admin(self) -> None:
        settings = make_settings()
        settings.openai_api_key = "test-openai-token"
        app = create_app(
            settings=settings,
            client_factory=FakeOpenRouterClient,
            openai_helper_factory=FakeOpenAIHelper,
        )
        client = TestClient(app)
        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Corrija esse bug difícil"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.openai_helper.calls), 1)
        final_payload = app.state.openrouter.calls[-1][1]
        self.assertIn("OPENAI_HELPER", str(final_payload))
        self.assertIn("Use stricter validation", str(final_payload))

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
            self.assertEqual(
                response.json()["content"][0]["text"],
                "model=qwen/qwen3-coder-30b-a3b-instruct",
            )

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

    def test_gift_card_signup_creates_customer_token(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.json"
            settings.quota_data_file = f"{tmpdir}/usage.json"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            gift = client.post(
                "/v1/admin/gift-cards",
                headers=self.headers,
                json={
                    "code": "CLAUDE-TEST-PRO",
                    "plan": "Plano Padrão",
                    "price": 149.9,
                    "model": "sonnet",
                    "manualLimit": 60000,
                },
            )
            self.assertEqual(gift.status_code, 200)
            self.assertEqual(gift.json()["giftCard"]["code"], "CLAUDE-TEST-PRO")

            signup = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Teste",
                    "login": "cliente@example.com",
                    "password": "senha-segura",
                    "giftCard": "CLAUDE-TEST-PRO",
                },
            )
            self.assertEqual(signup.status_code, 200)
            account = signup.json()["account"]
            self.assertNotIn("passwordHash", account)
            self.assertTrue(account["apiToken"].startswith("cus_"))
            self.assertEqual(account["giftCardCode"], "CLAUDE-TEST-PRO")

            reused = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Outro Cliente",
                    "login": "outro@example.com",
                    "password": "senha-segura",
                    "giftCard": "CLAUDE-TEST-PRO",
                },
            )
            self.assertEqual(reused.status_code, 400)

            response = client.post(
                "/v1/router/debug",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Implemente uma API"}],
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["requested_model"], "claude-code-ultra")

    def test_customer_conversations_are_saved_in_database(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            gift = client.post(
                "/v1/admin/gift-cards",
                headers=self.headers,
                json={"code": "CHAT-HISTORY", "plan": "Plano", "price": 149.9, "model": "sonnet"},
            ).json()["giftCard"]
            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Chat",
                    "login": "chat@example.com",
                    "password": "senha-segura",
                    "giftCard": gift["code"],
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            saved = client.post(
                "/v1/conversations",
                headers=customer_headers,
                json={
                    "messages": [
                        {"role": "user", "content": "Boa noite, preciso de um app"},
                        {"role": "assistant", "content": "Claro, vamos montar."},
                    ]
                },
            )
            self.assertEqual(saved.status_code, 200)
            conversation = saved.json()["conversation"]
            self.assertEqual(conversation["title"], "Boa noite, preciso de um app")

            listed = client.get("/v1/conversations", headers=customer_headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["data"][0]["id"], conversation["id"])

            loaded = client.get(f"/v1/conversations/{conversation['id']}", headers=customer_headers)
            self.assertEqual(loaded.status_code, 200)
            self.assertEqual(loaded.json()["conversation"]["messages"][1]["content"], "Claro, vamos montar.")

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

    def test_streaming_payload_uses_public_model_identity(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "explique uma funcao"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            _ = b"".join(response.iter_bytes())

        payload = self.app.state.openrouter.calls[-1][1]
        self.assertTrue(payload["stream"])
        self.assertIn("Claude Opus 4.7", payload["system"])

    def test_model_identity_question_returns_selected_public_model(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "qual modelo e voce?"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "claude-code-ultra")
        self.assertIn("Claude Opus 4.7", response.json()["content"][0]["text"])
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_streaming_model_identity_question_returns_selected_public_model(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "qual modelo está usando?"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"Claude Sonnet 4.6", body)
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_openrouter_payload_disables_reasoning_for_latency(self) -> None:
        client = OpenRouterClient(make_settings())

        payload = client._payload_for_model(
            {"messages": [], "reasoning": {"effort": "high", "exclude": False}},
            "qwen/qwen3-coder-30b-a3b-instruct",
        )

        self.assertEqual(payload["reasoning"], {"effort": "none", "exclude": True})
        self.assertFalse(payload["include_reasoning"])

    def test_openrouter_strips_reasoning_blocks_from_non_streaming_response(self) -> None:
        client = OpenRouterClient(make_settings())

        response = client._strip_reasoning_from_response(
            {
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "Oi!"},
                ],
                "reasoning": "hidden",
            }
        )

        self.assertEqual(response["content"], [{"type": "text", "text": "Oi!"}])
        self.assertNotIn("reasoning", response)

    def test_openrouter_filters_reasoning_events_from_stream(self) -> None:
        client = OpenRouterClient(make_settings())

        async def chunks():
            yield (
                b'event: message_start\n'
                b'data: {"type":"message_start"}\n\n'
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,'
                b'"content_block":{"type":"thinking","thinking":""}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"thinking_delta","thinking":"hidden"}}\n\n'
                b'event: content_block_stop\n'
                b'data: {"type":"content_block_stop","index":0}\n\n'
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":1,'
                b'"delta":{"type":"text_delta","text":"Oi!"}}\n\n'
            )

        async def collect() -> bytes:
            parts = []
            async for chunk in client._filter_reasoning_stream(chunks()):
                parts.append(chunk)
            return b"".join(parts)

        body = asyncio.run(collect())
        self.assertNotIn(b"thinking", body)
        self.assertIn(b"text_delta", body)


if __name__ == "__main__":
    unittest.main()
