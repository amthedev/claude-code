from __future__ import annotations

import asyncio
import base64
import io
import json
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from claude_gateway.anthropic import clean_model_text
from claude_gateway.accounts import _calculate_limit
from claude_gateway.config import Settings
from claude_gateway.customers import _today, daily_cost_budget_usd, parse_customer_accounts
from claude_gateway.main import _public_model_stream, create_app
from claude_gateway.openrouter import OpenRouterClient
from claude_gateway.research import WebSearchResult, WebSource, parse_web_search_response
from claude_gateway.skills import SKILL_CATALOG, select_skills


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


class FakeWebSearchClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, required: bool = True) -> WebSearchResult:
        self.calls.append({"query": query, "required": required})
        return WebSearchResult(
            summary="Resultado atual confirmado por fontes.",
            sources=(WebSource(title="Fonte oficial", url="https://example.com/source"),),
        )


class FakeHttpResponse:
    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self) -> dict[str, Any]:
        return self._data


class FakeMercadoPagoClient:
    last_post_json: dict[str, Any] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeMercadoPagoClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.__class__.last_post_json = kwargs.get("json")
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


class FakeGitHubClient:
    put_calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeGitHubClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        if url.endswith("/user/repos"):
            return FakeHttpResponse(
                [
                    {
                        "id": 1,
                        "name": "app",
                        "full_name": "amthedev/app",
                        "owner": {"login": "amthedev"},
                        "private": True,
                        "default_branch": "main",
                        "html_url": "https://github.com/amthedev/app",
                        "description": "Projeto de teste",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ]
            )
        if "/contents/" in url:
            return FakeHttpResponse({"message": "Not Found"}, status_code=404)
        if "/repos/amthedev/app" in url:
            return FakeHttpResponse({"default_branch": "main"})
        return FakeHttpResponse({}, status_code=404)

    async def put(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.__class__.put_calls.append({"url": url, "json": kwargs.get("json")})
        return FakeHttpResponse({"content": {"sha": "new-sha"}}, status_code=201)


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

    def test_skill_catalog_has_many_automatic_situations(self) -> None:
        self.assertGreaterEqual(len(SKILL_CATALOG), 40)
        selected = select_skills(
            "Conectar GitHub, listar repositorios, rodar testes e publicar alteracoes",
            "testing",
        )
        selected_ids = {skill.id for skill in selected}
        self.assertIn("github_connect", selected_ids)
        self.assertIn("test_runner", selected_ids)

    def test_skill_catalog_selects_senior_delivery_situations(self) -> None:
        selected = select_skills(
            "Faça uma entrega profissional senior para produção com rollback, logs e critérios de aceite",
            "architecture",
        )
        selected_ids = {skill.id for skill in selected}
        self.assertIn("senior_delivery", selected_ids)
        self.assertIn("incident_response", selected_ids)
        self.assertIn("observability", selected_ids)

    def test_public_health_is_minimal_and_admin_health_has_details(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        admin_response = self.client.get("/v1/admin/health", headers=self.headers)
        self.assertEqual(admin_response.status_code, 200)
        self.assertTrue(admin_response.json()["openrouter_configured"])
        self.assertIn("cost_target", admin_response.json())

    def test_admin_benchmark_reports_setup_and_route_results_without_upstream_call(self) -> None:
        response = self.client.post("/v1/admin/benchmark", headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertFalse(data["summary"]["spends_credits"])
        self.assertEqual(data["summary"]["mode"], "safe_router_only")
        self.assertGreaterEqual(data["summary"]["passed"], 10)
        self.assertEqual(self.app.state.openrouter.calls, [])

        rows = {row["id"]: row for row in data["results"]}
        self.assertEqual(rows["simple_pro"]["status"], "OK")
        self.assertFalse(rows["simple_pro"]["orchestration"])
        self.assertEqual(rows["current_web_auto"]["status"], "OK")
        self.assertTrue(rows["current_web_auto"]["web_search"])
        self.assertEqual(rows["tool_contract"]["status"], "OK")
        self.assertFalse(rows["tool_contract"]["orchestration"])
        self.assertEqual(rows["architecture_ultra"]["status"], "OK")
        self.assertLessEqual(rows["architecture_ultra"]["cost_ratio"], 0.4)

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

    def test_public_stream_normalizes_case_changed_cumulative_restart(self) -> None:
        payloads = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Oi! Como pos"}},
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Oi! Como Posso ajudar você hoje?"},
            },
        ]

        text = asyncio.run(collect_stream_text(stream_events(payloads)))

        self.assertEqual(text, "Oi! Como posso ajudar você hoje?")

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

    def test_clean_model_text_repairs_short_restarted_greeting(self) -> None:
        broken = "Oi! Como posOi! Como Posso ajudar você hoje?"

        self.assertEqual(clean_model_text(broken), "Oi! Como Posso ajudar você hoje?")

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

    def test_clean_model_text_does_not_trim_legitimate_word_endings(self) -> None:
        self.assertEqual(clean_model_text("Compre banana madura e teste a resposta."), "Compre banana madura e teste a resposta.")

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
                json={"message": "Quero falar com o Mano"},
            ).json()["ticket"]
            ticket_two = client.post(
                "/v1/support/tickets",
                headers=customer_two_headers,
                json={"message": "Quero falar com atendente humano"},
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

    def test_support_ai_answers_before_escalating_to_human(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Suporte",
                    "login": "suporte@example.com",
                    "password": "secret-support",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            response = client.post(
                "/v1/support/tickets",
                headers=customer_headers,
                json={"message": "Como conecto o GitHub?"},
            )
            self.assertEqual(response.status_code, 200)
            ticket = response.json()["ticket"]
            self.assertEqual(ticket["status"], "ai")
            self.assertEqual(ticket["messages"][-1]["author"], "Assistente")
            self.assertIn("GitHub", ticket["messages"][-1]["body"])

            queue = client.get("/v1/admin/support/tickets", headers=self.headers).json()
            self.assertEqual(queue["waiting"], [])

            response = client.post(
                f"/v1/support/tickets/{ticket['id']}/messages",
                headers=customer_headers,
                json={"message": "Agora quero falar com o Mano"},
            )
            self.assertEqual(response.status_code, 200)
            escalated = response.json()["ticket"]
            self.assertEqual(escalated["status"], "waiting")

            queue = client.get("/v1/admin/support/tickets", headers=self.headers).json()
            self.assertEqual([ticket["id"] for ticket in queue["waiting"]], [ticket["id"]])

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
            self.assertEqual(account["dailyLimit"], 200)

            message = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Oi"}],
                },
            )
            self.assertEqual(message.status_code, 200)
            self.assertEqual(message.json()["content"][0]["text"], "model=deepseek/deepseek-v4-flash")

            usage = client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
            )
            self.assertEqual(usage.status_code, 200)
            self.assertGreater(usage.json()["account"]["usedToday"], 0)
            self.assertEqual(usage.json()["account"]["usageDay"], _today())

            with app.state.account_store._connect() as db:
                db.execute(
                    "UPDATE accounts SET used_today = 199, usage_day = ? WHERE id = ?",
                    (_today(), account["id"]),
                )
                db.commit()

            blocked = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Oi de novo"}],
                },
            )
            self.assertEqual(blocked.status_code, 429)

            with app.state.account_store._connect() as db:
                db.execute(
                    "UPDATE accounts SET used_today = 200, usage_day = '2000-01-01' WHERE id = ?",
                    (account["id"],),
                )
                db.commit()

            reset = client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
            )
            self.assertEqual(reset.status_code, 200)
            self.assertEqual(reset.json()["account"]["usedToday"], 0)
            self.assertEqual(reset.json()["account"]["usageDay"], _today())

    def test_public_trial_signup_creates_temporary_max_account(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            settings.public_trial_enabled = True
            settings.public_trial_end_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
            settings.public_trial_plan_id = "ultra"
            settings.public_trial_daily_limit = 150000
            settings.public_trial_label = "Teste grátis 24h"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            plans = client.get("/v1/plans").json()
            self.assertTrue(plans["public_trial"]["active"])
            self.assertEqual(plans["public_trial"]["dailyLimit"], 150000)

            response = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Trial",
                    "login": "trial@example.com",
                    "password": "secret-trial",
                },
            )

            self.assertEqual(response.status_code, 200)
            account = response.json()["account"]
            self.assertEqual(account["plan"], "Teste grátis 24h")
            self.assertEqual(account["modelKey"], "opus")
            self.assertEqual(account["price"], 0)
            self.assertEqual(account["dailyLimit"], 150000)
            self.assertTrue(account["publicTrialActive"])
            self.assertTrue(account["trialExpiresAt"])

            debug = client.post(
                "/v1/router/debug",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Analise a arquitetura inteira"}],
                },
            )
            self.assertEqual(debug.status_code, 200)
            self.assertEqual(debug.json()["public_model"], "claude-code-ultra")

    def test_public_trial_promotes_existing_free_accounts_and_expires_to_free(self) -> None:
        with TemporaryDirectory() as directory:
            base_settings = make_settings()
            base_settings.account_data_file = f"{directory}/gateway.sqlite3"
            base_settings.quota_data_file = f"{directory}/gateway.sqlite3"
            base_app = create_app(settings=base_settings, client_factory=FakeOpenRouterClient)
            base_client = TestClient(base_app)

            free = base_client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Free",
                    "login": "free-trial@example.com",
                    "password": "secret-free",
                },
            ).json()["account"]
            self.assertEqual(free["dailyLimit"], 200)

            trial_settings = make_settings()
            trial_settings.account_data_file = f"{directory}/gateway.sqlite3"
            trial_settings.quota_data_file = f"{directory}/gateway.sqlite3"
            trial_settings.public_trial_enabled = True
            trial_settings.public_trial_end_at = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
            trial_app = create_app(settings=trial_settings, client_factory=FakeOpenRouterClient)
            trial_client = TestClient(trial_app)

            login = trial_client.post(
                "/v1/auth/login",
                json={"login": "free-trial@example.com", "password": "secret-free"},
            )
            self.assertEqual(login.status_code, 200)
            promoted = login.json()["account"]
            self.assertEqual(promoted["modelKey"], "opus")
            self.assertEqual(promoted["dailyLimit"], 150000)
            self.assertTrue(promoted["publicTrialActive"])

            expired_settings = make_settings()
            expired_settings.account_data_file = f"{directory}/gateway.sqlite3"
            expired_settings.quota_data_file = f"{directory}/gateway.sqlite3"
            expired_settings.public_trial_enabled = False
            expired_settings.public_trial_end_at = trial_settings.public_trial_end_at
            expired_app = create_app(settings=expired_settings, client_factory=FakeOpenRouterClient)
            expired_client = TestClient(expired_app)

            expired = expired_client.get(
                "/v1/auth/me",
                headers={"Authorization": f"Bearer {promoted['apiToken']}"},
            )
            self.assertEqual(expired.status_code, 200)
            account = expired.json()["account"]
            self.assertEqual(account["plan"], "Grátis")
            self.assertEqual(account["modelKey"], "haiku")
            self.assertEqual(account["dailyLimit"], 200)
            self.assertFalse(account["publicTrialActive"])
            self.assertEqual(account["trialExpiresAt"], "")

    def test_existing_unpaid_signup_account_is_migrated_to_free_limit(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Antigo",
                    "login": "antigo@example.com",
                    "password": "secret-old",
                },
            ).json()["account"]

            with app.state.account_store._connect() as db:
                db.execute(
                    """
                    UPDATE accounts
                       SET plan = 'Básico',
                           model_key = 'haiku',
                           manual_limit = 2500,
                           daily_limit = 2500,
                           computed_daily_tokens = 2500
                     WHERE id = ?
                    """,
                    (account["id"],),
                )
                db.commit()

            migrated_app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            migrated_client = TestClient(migrated_app)
            login = migrated_client.post(
                "/v1/auth/login",
                json={"login": "antigo@example.com", "password": "secret-old"},
            )

            self.assertEqual(login.status_code, 200)
            migrated = login.json()["account"]
            self.assertEqual(migrated["plan"], "Grátis")
            self.assertEqual(migrated["modelKey"], "haiku")
            self.assertEqual(migrated["price"], 0)
            self.assertEqual(migrated["dailyLimit"], 200)

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
                    json={
                        "planId": "pro",
                        "paymentMethod": "card_subscription",
                        "payerDocument": "123.456.789-09",
                    },
                )
            self.assertEqual(purchase.status_code, 200)
            purchase_id = purchase.json()["purchase"]["id"]
            self.assertEqual(purchase.json()["purchase"]["status"], "pending")
            self.assertEqual(purchase.json()["purchase"]["mercadoPagoPreferenceId"], "pref_test")
            self.assertIn("mercadopago.com.br", purchase.json()["purchase"]["checkoutUrl"])
            subscription_payload = FakeMercadoPagoClient.last_post_json or {}
            self.assertEqual(subscription_payload["external_reference"], purchase_id)
            self.assertEqual(subscription_payload["payer_email"], "upgrade@example.com")
            self.assertEqual(subscription_payload["auto_recurring"]["frequency_type"], "months")
            self.assertEqual(subscription_payload["auto_recurring"]["transaction_amount"], 125)

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
        self.assertEqual(data["agents"]["premium_review"], "deepseek/deepseek-v4-pro")
        self.assertLessEqual(data["cost_estimate"]["effective_path"]["cost_ratio_vs_claude"], 0.4)

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
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        self.assertNotIn("Internal Gemini coding guidance", str(self.app.state.openrouter.calls[-1][1]))

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

    def test_simple_pro_request_uses_single_fast_call_and_smaller_default_output(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "messages": [{"role": "user", "content": "Explique uma função Python simples"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertNotIn("Internal Gemini coding guidance", str(payload))

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
        self.assertIn("web_search", data)
        self.assertEqual(data["web_search"]["model"], "gpt-5.5")
        for model in data["models"].values():
            self.assertTrue(model["within_budget"], model)
            self.assertLessEqual(model["cost_ratio_vs_claude"], 0.5)

    def test_router_debug_reports_web_search_decision(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Explique uma função Python simples"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        stable = response.json()
        self.assertEqual(stable["web_search_policy"], "auto")
        self.assertFalse(stable["web_search_should_search"])
        self.assertEqual(stable["web_search_reason"], "stable_request")
        self.assertFalse(stable["use_orchestration"])

        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "gateway_web_search": "required",
                "messages": [{"role": "user", "content": "Pesquise notícias atuais de IA"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        required = response.json()
        self.assertEqual(required["web_search_policy"], "required")
        self.assertTrue(required["web_search_should_search"])
        self.assertEqual(required["web_search_reason"], "explicit")

        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "gateway_web_search": "off",
                "messages": [{"role": "user", "content": "Pesquise notícias atuais de IA"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        off = response.json()
        self.assertEqual(off["web_search_policy"], "off")
        self.assertFalse(off["web_search_should_search"])

    def test_web_search_context_is_injected_when_required(self) -> None:
        settings = make_settings()
        settings.openai_api_key = "test-openai-token"
        app = create_app(
            settings=settings,
            client_factory=FakeOpenRouterClient,
            web_search_factory=FakeWebSearchClient,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "max_tokens": 128,
                "gateway_web_search": "required",
                "messages": [{"role": "user", "content": "Pesquise o status atual do projeto"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.web_search.calls), 1)
        payload = app.state.openrouter.calls[-1][1]
        self.assertIn("Internal web research context", payload["system"])
        self.assertIn("https://example.com/source", payload["system"])
        self.assertNotIn("gateway_web_search", payload)

    def test_web_search_does_not_run_for_stable_or_off_requests(self) -> None:
        settings = make_settings()
        settings.openai_api_key = "test-openai-token"
        app = create_app(
            settings=settings,
            client_factory=FakeOpenRouterClient,
            web_search_factory=FakeWebSearchClient,
        )
        client = TestClient(app)

        stable = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Explique uma função Python local"}],
            },
        )
        self.assertEqual(stable.status_code, 200)

        off = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "max_tokens": 128,
                "gateway_web_search": "off",
                "messages": [{"role": "user", "content": "Pesquise notícias atuais de IA"}],
            },
        )
        self.assertEqual(off.status_code, 200)
        self.assertEqual(app.state.web_search.calls, [])

    def test_required_web_search_without_openai_key_falls_back_safely(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "max_tokens": 128,
                "gateway_web_search": "required",
                "messages": [{"role": "user", "content": "Pesquise o preço atual"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertIn("web search was needed", payload["system"])

    def test_web_search_response_extracts_citations_and_sources(self) -> None:
        result = parse_web_search_response(
            {
                "output_text": "Resumo atual.",
                "sources": [{"title": "Fonte completa", "url": "https://example.com/all"}],
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Resumo atual.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/cited",
                                        "title": "Fonte citada",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )

        self.assertTrue(result.searched)
        self.assertEqual(result.summary, "Resumo atual.")
        self.assertEqual({source.url for source in result.sources}, {"https://example.com/cited", "https://example.com/all"})

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

    def test_frontend_tool_requests_skip_internal_helpers_for_speed(self) -> None:
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
        self.assertEqual(app.state.openai_helper.calls, [])
        self.assertEqual(len(app.state.openrouter.calls), 1)
        payload = app.state.openrouter.calls[-1][1]
        self.assertNotIn("Internal execution guidance", str(payload))
        self.assertNotIn("Internal Gemini coding guidance", str(payload))

    def test_openai_decision_director_skips_simple_requests_for_speed(self) -> None:
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
                "messages": [
                    {
                        "role": "user",
                        "content": "Explique uma função Python simples",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.state.openai_helper.calls, [])
        self.assertEqual(len(app.state.openrouter.calls), 1)
        payload = app.state.openrouter.calls[-1][1]
        self.assertNotIn("Internal execution guidance", str(payload))

    def test_paid_customer_budget_has_half_revenue_safety_floor(self) -> None:
        settings = make_settings()
        settings.usd_to_brl = 5.0
        settings.customer_profit_margin = 0.10
        settings.customer_accounts = "paid-token|Maria|300|999999|claude-code-pro|true"
        plan = parse_customer_accounts(settings)["paid-token"]

        self.assertLessEqual(daily_cost_budget_usd(plan, settings), 300 * 0.50 / 5.0 / 30 + 1e-9)
        limit = _calculate_limit(300, "sonnet", 999999999, settings)
        self.assertLessEqual(float(limit["maxCostUsd"]), 300 * 0.50 / 5.0 + 1e-9)

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
            self.assertEqual(conversation["title"], "Criação de App")

            listed = client.get("/v1/conversations", headers=customer_headers)
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["data"][0]["id"], conversation["id"])

            loaded = client.get(f"/v1/conversations/{conversation['id']}", headers=customer_headers)
            self.assertEqual(loaded.status_code, 200)
            self.assertEqual(loaded.json()["conversation"]["messages"][1]["content"], "Claro, vamos montar.")

    def test_customer_can_upload_edit_and_download_code_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Codigo",
                    "login": "codigo@example.com",
                    "password": "secret-code",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr("repo/README.md", "# Olá\n")
                archive.writestr("repo/src/app.py", "print('oi')\n")

            uploaded = client.post(
                "/v1/code/workspaces/upload",
                headers=customer_headers,
                json={
                    "name": "Repo teste",
                    "zipBase64": base64.b64encode(archive_bytes.getvalue()).decode(),
                },
            )
            self.assertEqual(uploaded.status_code, 200)
            workspace_id = uploaded.json()["workspace"]["id"]

            files = client.get(f"/v1/code/workspaces/{workspace_id}/files", headers=customer_headers)
            self.assertEqual(files.status_code, 200)
            paths = {item["path"] for item in files.json()["files"]}
            self.assertIn("README.md", paths)
            self.assertIn("src/app.py", paths)

            readme = client.get(
                f"/v1/code/workspaces/{workspace_id}/files/content",
                headers=customer_headers,
                params={"path": "README.md"},
            )
            self.assertEqual(readme.status_code, 200)
            self.assertEqual(readme.json()["content"], "# Olá\n")

            saved = client.patch(
                f"/v1/code/workspaces/{workspace_id}/files/content",
                headers=customer_headers,
                json={"path": "README.md", "content": "# Atualizado\n"},
            )
            self.assertEqual(saved.status_code, 200)

            downloaded = client.get(
                f"/v1/code/workspaces/{workspace_id}/download",
                headers=customer_headers,
            )
            self.assertEqual(downloaded.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                self.assertEqual(archive.read("README.md").decode(), "# Atualizado\n")

            command = client.post(
                f"/v1/code/workspaces/{workspace_id}/terminal",
                headers=customer_headers,
                json={"command": "python3 -m pytest -q"},
            )
            self.assertEqual(command.status_code, 200)
            self.assertEqual(command.json()["result"]["command"], "python3 -m pytest -q")

    def test_customer_can_upload_code_workspace_from_folder_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Pasta",
                    "login": "pasta@example.com",
                    "password": "secret-code",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            uploaded = client.post(
                "/v1/code/workspaces/upload",
                headers=customer_headers,
                json={
                    "name": "Pasta teste",
                    "files": [
                        {
                            "path": "minha-pasta/README.md",
                            "contentBase64": base64.b64encode(b"# Pasta\n").decode(),
                        },
                        {
                            "path": "minha-pasta/src/app.py",
                            "contentBase64": base64.b64encode(b"print('pasta')\n").decode(),
                        },
                    ],
                },
            )
            self.assertEqual(uploaded.status_code, 200)
            self.assertEqual(uploaded.json()["workspace"]["source"], "folder")
            workspace_id = uploaded.json()["workspace"]["id"]

            files = client.get(f"/v1/code/workspaces/{workspace_id}/files", headers=customer_headers)
            paths = {item["path"] for item in files.json()["files"]}
            self.assertEqual(paths, {"README.md", "src/app.py"})

    def test_github_workspace_requires_access_token(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Git",
                    "login": "git@example.com",
                    "password": "secret-code",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            response = client.post(
                "/v1/code/workspaces/github",
                headers=customer_headers,
                json={"repoUrl": "https://github.com/amthedev/claude-code"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("chave", response.json()["detail"].lower())

    def test_customer_can_list_github_repositories_by_profile(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente GitHub",
                    "login": "github@example.com",
                    "password": "secret-code",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            with patch("claude_gateway.main.httpx.AsyncClient", FakeGitHubClient):
                response = client.post(
                    "/v1/code/github/repositories",
                    headers=customer_headers,
                    json={"profile": "amthedev", "githubToken": "github_pat_test"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"][0]["fullName"], "amthedev/app")
            self.assertTrue(response.json()["data"][0]["private"])

    def test_customer_can_publish_github_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            FakeGitHubClient.put_calls = []
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/gateway.sqlite3"
            settings.quota_data_file = f"{tmpdir}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente Publish",
                    "login": "publish@example.com",
                    "password": "secret-code",
                },
            ).json()["account"]
            customer_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                archive.writestr("repo/README.md", "# Publicar\n")
            workspace = app.state.code_workspaces.create_from_zip(
                account["apiToken"],
                name="amthedev/app",
                zip_bytes=archive_bytes.getvalue(),
                source="github",
                repo_url="https://github.com/amthedev/app",
                ref="main",
            )

            with patch("claude_gateway.main.httpx.AsyncClient", FakeGitHubClient):
                response = client.post(
                    f"/v1/code/workspaces/{workspace['id']}/github/publish",
                    headers=customer_headers,
                    json={"githubToken": "github_pat_test", "message": "Atualiza README"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["publish"]["created"], 1)
            self.assertEqual(len(FakeGitHubClient.put_calls), 1)
            self.assertEqual(FakeGitHubClient.put_calls[0]["json"]["message"], "Atualiza README")

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
        self.assertIn("Keep Anthropic-compatible Claude Code API behavior", payload["system"])
        self.assertIn("Do not mention internal routing providers", payload["system"])
        self.assertIn("Automatic senior skill routing is active", payload["system"])

    def test_payload_selects_relevant_automatic_skills(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": "Conectar GitHub pelo perfil, listar repositorios, rodar testes e publicar alteracoes",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertIn("Conectar GitHub", payload["system"])
        self.assertIn("Testes e Terminal", payload["system"])
        self.assertIn("Publicar Alterações no GitHub", payload["system"])

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
        self.assertIn("Keep Anthropic-compatible Claude Code API behavior", payload["system"])
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
        self.assertIn("Whenever the answer depends on information that can change over time", payload["system"])
        self.assertIn("If no browsing/search tool is available", payload["system"])

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
