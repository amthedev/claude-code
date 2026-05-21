from __future__ import annotations

import asyncio
import json
import unittest
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from claude_gateway.anthropic import clean_model_text
from claude_gateway.config import Settings
from claude_gateway.customers import _today, parse_customer_accounts
from claude_gateway.main import _public_model_stream, create_app
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
        yield f'data: {{"message": {{"model": "{model}", "provider": "fake"}}}}\n\n'.encode()


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


class FakeHttpResponse:
    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self) -> dict[str, Any]:
        return self._data


class FakeMercadoPagoClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeMercadoPagoClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return FakeHttpResponse(
            {
                "id": "pref_test",
                "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref_test",
                "sandbox_init_point": "https://sandbox.mercadopago.com.br/checkout/v1/redirect?pref_id=pref_test",
            }
        )

    async def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        return FakeHttpResponse(
            {
                "id": 12345,
                "status": "approved",
                "external_reference": "purchase_reference",
            }
        )


async def collect_stream_text(chunks: Any) -> str:
    text = ""
    async for chunk in _public_model_stream(chunks, "claude-code-pro"):
        event = chunk.decode("utf-8")
        data_lines = [
            line.removeprefix("data:").strip()
            for line in event.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if not data or data == "[DONE]":
            continue
        payload = json.loads(data)
        delta = payload.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            text += delta["text"]
        for choice in payload.get("choices") or []:
            choice_delta = choice.get("delta") or {}
            content = choice_delta.get("content")
            if isinstance(content, str):
                text += content
    return text


async def stream_events(payloads: list[dict[str, Any]]):
    for payload in payloads:
        yield f"event: content_block_delta\ndata: {json.dumps(payload)}\n\n".encode()


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
        self.assertNotIn("localhost", response.headers["content-security-policy"])
        self.assertNotIn("127.0.0.1", response.headers["content-security-policy"])

    def test_public_health_is_minimal_and_admin_health_has_details(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        admin_response = self.client.get("/v1/admin/health", headers=self.headers)
        self.assertEqual(admin_response.status_code, 200)
        self.assertTrue(admin_response.json()["openrouter_configured"])
        self.assertIn("cost_target", admin_response.json())

    def test_public_stream_normalizes_overlapping_anthropic_text_deltas(self) -> None:
        payloads = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Se"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Seu"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "u texto"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " texto"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " já cont"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " contém"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ém um plano"}},
        ]

        text = asyncio.run(collect_stream_text(stream_events(payloads)))

        self.assertEqual(text, "Seu texto já contém um plano")

    def test_public_stream_normalizes_cumulative_openai_text_deltas(self) -> None:
        payloads = [
            {"choices": [{"delta": {"content": "How"}}]},
            {"choices": [{"delta": {"content": "How to"}}]},
            {"choices": [{"delta": {"content": "How to become"}}]},
            {"choices": [{"delta": {"content": " fluent"}}]},
            {"choices": [{"delta": {"content": " in English"}}]},
        ]

        text = asyncio.run(collect_stream_text(stream_events(payloads)))

        self.assertEqual(text, "How to become fluent in English")

    def test_clean_model_text_repairs_fragmented_duplicate_words(self) -> None:
        broken = (
            "AAquiqui está está um um ** **plplanoano real realistaista e e pratic praticoo "
            "para para fic ficarar flu fluenteente em em ingl inglêsês em em 33 meses meses"
        )

        self.assertEqual(
            clean_model_text(broken),
            "Aqui está um **plano realista e pratico para ficar fluente em inglês em 33 meses",
        )

    def test_clean_model_text_repairs_glued_words_without_dropping_spaces(self) -> None:
        broken = (
            "Treina ouvido com input comprensível (70–90% dentendimento)\n"
            "Escreva 3–5 frasesobre seu dia\n"
            "🔑 Priorize Iso (em ordem de impacto)\n"
            "→ Aprendas *1.0–2.0 palavras mais usadas**\n"
            "→ Foquem frases prontas, não listas de palavras\n"
            "⚠️ O quevita resultado:\n"
            "Decorar listas gigantes de palavrasem contexto\n"
            "Estudar 3hoje nada nos próximos 3 dias\n"
            "1 mês\tComprensão básica, frases curtas\n"
            "3 meses\tConversasimples\n"
            "Quer queu monte um plano?"
        )

        cleaned = clean_model_text(broken)

        self.assertIn("70–90% de entendimento", cleaned)
        self.assertIn("3–5 frases sobre seu dia", cleaned)
        self.assertIn("Priorize Isso", cleaned)
        self.assertIn("Aprenda as **1.000–2.000 palavras", cleaned)
        self.assertIn("Foque em frases prontas", cleaned)
        self.assertIn("O que evita resultado", cleaned)
        self.assertIn("palavras sem contexto", cleaned)
        self.assertIn("3h hoje e nada", cleaned)
        self.assertIn("Compreensão básica", cleaned)
        self.assertIn("Conversa simples", cleaned)
        self.assertIn("Quer que eu monte", cleaned)

    def test_clean_model_text_recovers_restarted_answer_and_new_typos(self) -> None:
        broken = (
            "Paraprender inglês rápido, o segredo é consistência diária.\n"
            "Tempo\tAtividade\tExemplo prático\t----\n"
            "15 min\tVocabulário útil\tI’d like a coffe\t15 min\tEscutativa\tBBC Learning English\t"
            "*10–20 min**\tFala\tUse Speak or conversa com IA\t*5–10 min**\tRevisão\tpalavrasoltas\n"
            "Conteúdo seu interesse e Broklyn Nine-Nine\n"
            "Use inglês na rotina, pensem frasesimples antes de dormir\n"
            "⚠️ O quevParaprender inglês rápido, o segredo é consistência diária.\n"
            "Tempo\tAtividade\tExemplo prático\t----\n"
            "15 min\tVocabulário útil\tI’d like a coffe\t15 min\tEscutativa\tBBC Learning English\t"
            "*10–20 min**\tFala\tUse Speak or conversa com IA\t*5–10 min**\tRevisão\tpalavrasoltas\n"
            "Conteúdo seu interesse e Broklyn Nine-Nine\n"
            "Use inglês na rotina, pensem frasesimples antes de dormir\n"
            "⚠️ O que evita progresso real\n"
            "Estudar gramática teórica por semanasem usar\n"
            "Evitar falar por medo derrar\n"
            "Poso montar um plano com metasemanais."
        )

        cleaned = clean_model_text(broken)

        self.assertEqual(cleaned.count("Para aprender inglês rápido"), 1)
        self.assertIn("coffee", cleaned)
        self.assertIn("Escuta ativa", cleaned)
        self.assertIn("10–20 min", cleaned)
        self.assertIn("Speak ou converse com IA", cleaned)
        self.assertIn("palavras soltas", cleaned)
        self.assertIn("Conteúdo do seu interesse", cleaned)
        self.assertIn("Brooklyn Nine-Nine", cleaned)
        self.assertIn("pense em frases simples", cleaned)
        self.assertIn("semanas sem usar", cleaned)
        self.assertIn("medo de errar", cleaned)
        self.assertIn("Posso montar um plano com metas semanais", cleaned)

    def test_openapi_is_not_public_by_default(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 404)

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

    def test_admin_password_can_be_configured_in_backend_database(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.admin_password = ""
            settings.admin_password_hash = ""
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            status = client.get("/v1/admin/setup-status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["configured"])

            setup = client.post(
                "/v1/admin/setup",
                json={"login": "admin", "password": "senha-admin-forte"},
            )
            self.assertEqual(setup.status_code, 200)
            token = setup.json()["admin"]["token"]
            self.assertTrue(token.startswith("sk-admin-"))

            response = client.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)

            login = client.post(
                "/v1/admin/login",
                json={"login": "admin", "password": "senha-admin-forte"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertTrue(login.json()["admin"]["token"].startswith("sk-admin-"))

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
            settings.mercado_pago_access_token = "mp-test-token"
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

    def test_signup_without_gift_card_creates_free_economy_account(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            settings.mercado_pago_access_token = "mp-test-token"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            response = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Gratis",
                    "login": "gratis@example.com",
                    "password": "secret-free",
                },
            )

            self.assertEqual(response.status_code, 200)
            account = response.json()["account"]
            self.assertEqual(account["plan"], "Grátis")
            self.assertEqual(account["modelKey"], "haiku")
            self.assertEqual(account["price"], 0)
            self.assertGreater(account["dailyLimit"], 0)

            message = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "Explique soma"}],
                },
            )
            self.assertEqual(message.status_code, 200)
            self.assertEqual(message.json()["content"][0]["text"], "model=deepseek/deepseek-v4-flash")

    def test_purchase_approval_upgrades_account_and_tracks_revenue(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            settings.mercado_pago_access_token = "mp-test-token"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Upgrade",
                    "login": "upgrade@example.com",
                    "password": "secret-upgrade",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            with patch("claude_gateway.main.httpx.AsyncClient", FakeMercadoPagoClient):
                purchase = client.post(
                    "/v1/billing/purchases",
                    headers=customer_headers,
                    json={"planId": "pro"},
                )
            self.assertEqual(purchase.status_code, 200)
            purchase_id = purchase.json()["purchase"]["id"]
            self.assertEqual(purchase.json()["purchase"]["status"], "pending")
            self.assertEqual(purchase.json()["purchase"]["mercadoPagoPreferenceId"], "pref_test")
            self.assertIn("mercadopago.com.br", purchase.json()["purchase"]["checkoutUrl"])

            admin_purchases = client.get("/v1/admin/purchases", headers=self.headers)
            self.assertEqual(admin_purchases.status_code, 200)
            self.assertEqual(admin_purchases.json()["data"][0]["price"], 125)

            approved = client.post(
                f"/v1/admin/purchases/{purchase_id}/approve",
                headers=self.headers,
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["purchase"]["status"], "paid")

            accounts = client.get("/v1/admin/accounts", headers=self.headers).json()["data"]
            upgraded = next(item for item in accounts if item["login"] == "upgrade@example.com")
            self.assertEqual(upgraded["modelKey"], "sonnet")
            self.assertEqual(upgraded["price"], 125)

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

    def test_cors_allows_production_origin_by_default(self) -> None:
        app = create_app(settings=make_settings(), client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.options(
            "/v1/messages",
            headers={
                "Origin": "https://claude-code-api.squareweb.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "https://claude-code-api.squareweb.app",
        )

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
        self.assertEqual(data["selected_openrouter_model"], "qwen/qwen3-coder-next")
        self.assertEqual(data["agents"]["reasoning"], "tencent/hy3-preview")
        self.assertEqual(data["agents"]["review"], "deepseek/deepseek-v4-pro")
        self.assertTrue(data["cost_estimate"]["effective_path"]["within_budget"])
        self.assertLessEqual(
            data["cost_estimate"]["effective_path"]["cost_ratio_vs_claude"],
            0.5,
        )

    def test_simple_frontend_fix_uses_deepseek_flash(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-ui",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Corrija um typo simples no CSS do frontend"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "ui")
        self.assertEqual(data["task_type"], "frontend")
        self.assertEqual(data["complexity"], "low")
        self.assertEqual(data["selected_openrouter_model"], "deepseek/deepseek-v4-flash")

    def test_integral_project_analysis_uses_qwen_thinking(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": "Analise a arquitetura integral de todo o projeto e encontre riscos",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "ultra")
        self.assertEqual(data["task_type"], "architecture")
        self.assertEqual(data["selected_openrouter_model"], "qwen/qwen3-235b-a22b-thinking-2507")
        self.assertEqual(data["agents"]["reasoning"], "qwen/qwen3-235b-a22b-thinking-2507")
        self.assertEqual(data["agents"]["coding"], "moonshotai/kimi-k2.6")

    def test_critical_ultra_reasoning_uses_r1_only_when_needed(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": "Corrija um bug critical de auth em production com race condition",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "ultra")
        self.assertEqual(data["complexity"], "critical")
        self.assertEqual(data["selected_openrouter_model"], "deepseek/deepseek-r1")
        self.assertEqual(data["agents"]["reasoning"], "deepseek/deepseek-r1")

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
        self.assertEqual(response.json()["content"][0]["text"], "model=qwen/qwen3-coder-next")
        self.assertEqual(len(self.app.state.openrouter.calls), 2)
        self.assertEqual(self.app.state.openrouter.calls[0][0], "google/gemini-2.5-flash-lite")
        self.assertIn("Internal Gemini coding guidance", self.app.state.openrouter.calls[-1][1]["system"])

    def test_auto_routes_terminal_file_edits_to_pro_coder(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-auto",
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": "No terminal, mexer nos arquivos e aplicar patch para corrigir o bug",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "pro")
        self.assertEqual(data["task_type"], "file_edit")
        self.assertEqual(data["selected_openrouter_model"], "qwen/qwen3-coder-next")
        self.assertTrue(data["use_orchestration"])

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
        self.assertEqual(len(app.state.openai_helper.calls), 2)
        self.assertIn("decision director", app.state.openai_helper.calls[0]["instructions"])
        self.assertIn("helper reviewing", app.state.openai_helper.calls[1]["instructions"])
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
                "model=deepseek/deepseek-v4-flash",
            )

            usage = client.get("/v1/usage", headers=customer_headers)
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()["customer"]["allowed_model"], "claude-code-economy")
            self.assertGreater(usage.json()["today"]["requests"], 0)

    def test_customer_wildcard_model_starts_on_ultra_by_default(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.customer_accounts = "wild-token|Maria|9999|100000|*|true"
            settings.quota_data_file = f"{tmpdir}/usage.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            response = client.post(
                "/v1/router/debug",
                headers={"Authorization": "Bearer wild-token"},
                json={
                    "model": "claude-code-pro",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Implemente uma API"}],
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["requested_model"], "claude-code-ultra")
            self.assertEqual(response.json()["public_model"], "claude-code-ultra")

    def test_customer_wildcard_model_downgrades_when_daily_tokens_are_low(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.customer_accounts = "quota-token|Maria|9999|100000|*|true"
            settings.quota_data_file = f"{tmpdir}/usage.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)
            plan = parse_customer_accounts(settings)["quota-token"]
            self.assertIsNotNone(plan)

            with app.state.customer_usage._connect() as db:
                db.execute(
                    """
                    INSERT INTO customer_usage (
                        day, token_hash, requests, reserved_cost_usd, reserved_tokens
                    ) VALUES (?, ?, 1, 0.01, 97000)
                    """,
                    (_today(), plan.token_hash),
                )

            response = client.post(
                "/v1/router/debug",
                headers={"Authorization": "Bearer quota-token"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Implemente uma API"}],
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["requested_model"], "claude-code-economy")
            self.assertEqual(response.json()["public_model"], "claude-code-economy")

    def test_openai_design_director_guides_frontend_tool_requests(self) -> None:
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
                "model": "claude-code-ultra",
                "max_tokens": 256,
                "tools": [{"name": "write_file", "input_schema": {"type": "object"}}],
                "messages": [
                    {
                        "role": "user",
                        "content": "Crie uma landing page premium moderna em React e Tailwind",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.openai_helper.calls), 1)
        self.assertEqual(len(app.state.openrouter.calls), 2)
        self.assertEqual(app.state.openrouter.calls[0][0], "google/gemini-2.5-flash-lite")
        payload = app.state.openrouter.calls[-1][1]
        self.assertIn("Internal execution guidance", payload["system"])
        self.assertIn("Internal Gemini coding guidance", payload["system"])
        self.assertIn("Use stricter validation", payload["system"])

    def test_openai_decision_director_guides_pro_tool_requests(self) -> None:
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
                "max_tokens": 256,
                "tools": [{"name": "write_file", "input_schema": {"type": "object"}}],
                "messages": [
                    {
                        "role": "user",
                        "content": "Implemente uma API e escolha a melhor estrutura de arquivos",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.openai_helper.calls), 1)
        self.assertIn("decision director", app.state.openai_helper.calls[0]["instructions"])
        payload = app.state.openrouter.calls[-1][1]
        self.assertIn("Internal execution guidance", payload["system"])
        self.assertIn("choose defaults", payload["system"])

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
            self.assertTrue(account["apiToken"].startswith("sk-"))
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
            self.assertEqual(response.json()["requested_model"], "claude-code-pro")
            self.assertEqual(response.json()["public_model"], "claude-code-pro")

    def test_openai_responses_endpoint_accepts_gateway_token(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "input": "Escreva uma função pequena",
                "max_output_tokens": 128,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["model"], "claude-code-pro")
        self.assertEqual(body["output"][0]["content"][0]["type"], "output_text")

    def test_openai_chat_completions_endpoint_accepts_gateway_token(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "messages": [{"role": "user", "content": "Diga oi"}],
                "max_tokens": 128,
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "claude-code-pro")
        self.assertEqual(body["choices"][0]["message"]["role"], "assistant")

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
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        self.assertEqual(self.app.state.openrouter.calls[-1][1]["__gateway_reasoning"], "none")

    def test_streaming_complex_request_uses_internal_pipeline(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "corrija esse bug no projeto"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"text_delta", body)
        self.assertGreaterEqual(len(self.app.state.openrouter.calls), 5)
        self.assertEqual(self.app.state.openrouter.calls[0][1]["__gateway_reasoning"], "low")

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
        self.assertIn("Match Anthropic Claude Code response behavior", payload["system"])
        self.assertIn("Do not mention internal routing providers", payload["system"])

    def test_streaming_proxy_rewrites_message_start_to_public_model(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "oi"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b'"model": "claude-code-pro"', body)

    def test_tool_payload_uses_anthropic_compatible_style_prompt(self) -> None:
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
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertIn("Claude Sonnet 4.6", payload["system"])
        self.assertIn("Match Anthropic Claude Code response behavior", payload["system"])
        self.assertEqual(payload["max_tokens"], 16000)

    def test_public_identity_prompt_includes_current_date_context(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "oi"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertIn("Current date for user-facing and factual work:", payload["system"])
        self.assertIn("America/Recife", payload["system"])

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
        self.assertNotIn("__gateway_reasoning", payload)

    def test_openrouter_payload_allows_hidden_reasoning_for_complex_tasks(self) -> None:
        client = OpenRouterClient(make_settings())

        payload = client._payload_for_model(
            {"messages": [], "__gateway_reasoning": "low"},
            "qwen/qwen3-coder-flash",
        )

        self.assertEqual(payload["reasoning"], {"effort": "low", "exclude": True})
        self.assertFalse(payload["include_reasoning"])
        self.assertNotIn("__gateway_reasoning", payload)

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
