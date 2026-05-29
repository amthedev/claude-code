from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import os
import time
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
from claude_gateway.customers import _today, daily_cost_budget_usd, estimate_reserved_tokens, parse_customer_accounts
from claude_gateway.main import _anthropic_stream_to_response_sse, _public_model_stream, create_app
from claude_gateway.model_client import VPSAnthropicClient
from claude_gateway.openrouter import OpenRouterClient, OpenRouterError
from claude_gateway.openai_compat import chat_to_anthropic, responses_to_anthropic
from claude_gateway.research import (
    WebSearchResult,
    WebSource,
    parse_openrouter_web_search_response,
    parse_web_search_response,
)
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


class FakeUsageStreamingOpenRouterClient(FakeOpenRouterClient):
    async def stream_messages(self, payload: dict[str, Any], model: str):
        self.calls.append((model, payload))
        yield b"event: message_start\n"
        yield f'data: {{"message": {{"model": "{model}", "provider": "fake"}}}}\n\n'.encode()
        yield b'event: message_delta\ndata: {"type":"message_delta","usage":{"input_tokens":3,"output_tokens":5}}\n\n'
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class FakeFailingVPSClient(FakeOpenRouterClient):
    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        self.calls.append((model, payload))
        raise OpenRouterError("VPS failed", status_code=502)

    async def stream_messages(self, payload: dict[str, Any], model: str):
        self.calls.append((model, payload))
        raise OpenRouterError("VPS stream failed", status_code=502)
        yield b""


class FakeSlowVPSClient(FakeOpenRouterClient):
    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        self.calls.append((model, payload))
        await asyncio.sleep(0.05)
        return await super().complete_messages(payload, model)

    async def stream_messages(self, payload: dict[str, Any], model: str):
        self.calls.append((model, payload))
        await asyncio.sleep(0.05)
        yield b"event: message_start\n"


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


class FakeHangingWebSearchClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, *, required: bool = True) -> WebSearchResult:
        self.calls.append({"query": query, "required": required})
        await asyncio.sleep(1)
        return WebSearchResult(summary="late", sources=())


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


async def _collect_async_bytes(chunks) -> list[bytes]:
    return [chunk async for chunk in chunks]


def make_settings() -> Settings:
    return Settings(
        gateway_api_keys=("test-token",),
        allow_admin_model_access=True,
        openrouter_api_key="test-openrouter-token",
        vps_model_id="qwen-14b",
        enable_agent_orchestration=True,
    )


class GatewayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(settings=make_settings(), client_factory=FakeOpenRouterClient)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer test-token"}

    def test_expensive_model_environment_overrides_are_ignored(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_HELPER_FOR_CUSTOMERS": "true",
                "ENABLE_GEMINI_CODE_HELPER": "true",
                "PREMIUM_FALLBACK": "moonshotai/kimi-k2.6",
                "BACKEND_PARTNER_AGENT": "moonshotai/kimi-k2.6",
                "PROJECT_REASONING_AGENT": "qwen/qwen3-235b-a22b-thinking-2507",
                "DEEP_REASONING_AGENT": "deepseek/deepseek-r1",
                "OPENROUTER_EMERGENCY_FALLBACK": "true",
                "API_RATE_LIMIT": "120",
            },
        ):
            settings = Settings.from_env()

        self.assertFalse(settings.openai_helper_for_customers)
        self.assertFalse(settings.enable_gemini_code_helper)
        self.assertEqual(settings.premium_fallback, "deepseek/deepseek-v4-pro")
        self.assertEqual(settings.backend_partner_agent, "deepseek/deepseek-v4-pro")
        self.assertEqual(settings.project_reasoning_agent, "deepseek/deepseek-v4-pro")
        self.assertEqual(settings.deep_reasoning_agent, "deepseek/deepseek-v4-pro")
        self.assertFalse(settings.openrouter_emergency_fallback)
        self.assertEqual(settings.api_rate_limit, 600)

    def test_messages_strip_provider_cache_metadata_before_backend(self) -> None:
        payload = {
            "model": "claude-code-pro",
            "max_tokens": 200,
            "system": [
                {
                    "type": "text",
                    "text": "System prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Leia isso.",
                            "cache_control": {"type": "ephemeral"},
                            "cacheId": "provider-owned-cache",
                        }
                    ],
                }
            ],
            "tools": [
                {
                    "name": "Read",
                    "description": "Read files",
                    "input_schema": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "cacheControl": {"type": "ephemeral"},
                    },
                    "container": {"id": "provider-container"},
                }
            ],
        }

        response = self.client.post("/v1/messages", headers=self.headers, json=payload)

        self.assertEqual(response.status_code, 200)
        sent_payload = self.app.state.model_client.calls[-1][1]
        serialized = json.dumps(sent_payload, default=str)
        self.assertNotIn("cache_control", serialized)
        self.assertNotIn("cacheControl", serialized)
        self.assertNotIn("cacheId", serialized)
        self.assertNotIn("container", serialized)

    def test_vps_anthropic_openrouter_target_reuses_openrouter_credentials(self) -> None:
        settings = make_settings()
        settings.openrouter_api_key = "sk-or-test"
        settings.openrouter_site_url = "https://example.com"
        settings.openrouter_app_name = "Gateway Test"
        settings.vps_model_base_url = "https://openrouter.ai/api"
        settings.vps_model_api_key = ""

        client = VPSAnthropicClient(settings)

        headers = client._headers(client._default_target())

        self.assertEqual(headers["Authorization"], "Bearer sk-or-test")
        self.assertEqual(headers["HTTP-Referer"], "https://example.com")
        self.assertEqual(headers["X-OpenRouter-Title"], "Gateway Test")
        self.assertEqual(headers["X-Title"], "Gateway Test")
        self.assertEqual(client.messages_url, "https://openrouter.ai/api/v1/messages")

    def test_x_api_key_takes_priority_over_stale_authorization_header(self) -> None:
        response = self.client.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer stale-claude-ai-token",
                "X-API-Key": "test-token",
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_admin_token_is_blocked_from_model_generation_by_default(self) -> None:
        settings = make_settings()
        settings.allow_admin_model_access = False
        settings.customer_accounts = "customer-token|Cliente|65|60000|claude-code-pro|true"
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        payload = {
            "model": "claude-code-pro",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Diga oi"}],
        }

        admin_response = client.post("/v1/messages", headers=self.headers, json=payload)
        customer_response = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer customer-token"},
            json=payload,
        )

        self.assertEqual(admin_response.status_code, 403)
        self.assertEqual(admin_response.json()["detail"], "Customer API token required.")
        self.assertEqual(customer_response.status_code, 200)

    def test_settings_ignore_vps_model_env_and_use_runpod_backend(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GATEWAY_SKIP_DOTENV": "1",
                "RUNPOD_AUTO_DISCOVER_ACTIVE": "false",
                "RUNPOD_POD_ID": "pod123",
                "RUNPOD_VLLM_PORT": "8001",
                "RUNPOD_VLLM_API_KEY": "vllm-secret",
                "VPS_MODEL_BASE_URL": "https://ignored-provider.example/anthropic",
                "VPS_MODEL_ID": "claude-sonnet-4-6",
                "VPS_MODEL_API_FORMAT": "anthropic",
                "VPS_MODEL_API_KEY": "tokies-key-one,tokies-key-two",
                "VPS_FAST_MODEL_BASE_URL": "https://wrong-fast.example/v1",
                "VPS_FAST_MODEL_ID": "wrong-fast-model",
                "VPS_FAST_MODEL_API_FORMAT": "anthropic",
                "VPS_FAST_MODEL_API_KEY": "wrong-fast-key",
                "VPS_STRONG_MODEL_BASE_URL": "https://wrong-strong.example/v1",
                "VPS_STRONG_MODEL_ID": "wrong-strong-model",
                "VPS_STRONG_MODEL_API_FORMAT": "anthropic",
                "VPS_STRONG_MODEL_API_KEY": "wrong-strong-key",
            },
            clear=False,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.vps_model_base_url, "https://pod123-8001.proxy.runpod.net/v1")
        self.assertEqual(settings.vps_model_id, "qwen25-coder-14b")
        self.assertEqual(settings.vps_model_api_format, "openai-chat")
        self.assertEqual(settings.vps_fast_model_base_url, "https://pod123-8001.proxy.runpod.net/v1")
        self.assertEqual(settings.vps_fast_model_id, "qwen25-coder-14b")
        self.assertEqual(settings.vps_model_api_key, "vllm-secret")
        self.assertEqual(settings.vps_fast_model_api_key, "vllm-secret")
        self.assertEqual(settings.vps_strong_model_base_url, "")
        self.assertEqual(settings.vps_strong_model_id, "")
        self.assertEqual(settings.vps_strong_model_api_key, "")

    def test_settings_discovers_active_runpod_migration_when_pod_id_is_stale(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "myself": {
                        "pods": [
                            {
                                "id": "oldpod",
                                "name": "ia-qwen3-32b-a40-vllm",
                                "desiredStatus": "EXITED",
                                "runtime": None,
                            },
                            {
                                "id": "newpod",
                                "name": "ia-qwen3-32b-a40-vllm-migration",
                                "desiredStatus": "RUNNING",
                                "runtime": {
                                    "ports": [{"privatePort": 8001, "type": "http"}],
                                },
                            },
                        ]
                    }
                }
            }
        ).encode()

        class FakeRunPodResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return body

        with patch.dict(
            os.environ,
            {
                "GATEWAY_SKIP_DOTENV": "1",
                "RUNPOD_API_KEY": "rpa_test",
                "RUNPOD_POD_ID": "oldpod",
                "RUNPOD_VLLM_PORT": "8001",
                "VPS_MODEL_BASE_URL": "https://ignored-provider.example/anthropic",
                "VPS_MODEL_ID": "claude-sonnet-4-6",
                "VPS_MODEL_API_FORMAT": "anthropic",
            },
            clear=False,
        ), patch("claude_gateway.config.urlopen", return_value=FakeRunPodResponse()):
            settings = Settings.from_env()

        self.assertEqual(settings.vps_model_base_url, "https://newpod-8001.proxy.runpod.net/v1")
        self.assertEqual(settings.vps_fast_model_base_url, "https://newpod-8001.proxy.runpod.net/v1")

    def test_settings_discovers_active_runpod_pod_before_runtime_ports_are_ready(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "myself": {
                        "pods": [
                            {
                                "id": "oldpod",
                                "name": "ia-qwen-14b-vllm",
                                "desiredStatus": "EXITED",
                                "runtime": None,
                            },
                            {
                                "id": "newpod",
                                "name": "ia-qwen-14b-vllm-migration",
                                "desiredStatus": "RUNNING",
                                "runtime": {"ports": []},
                            },
                        ]
                    }
                }
            }
        ).encode()

        class FakeRunPodResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return body

        with patch.dict(
            os.environ,
            {
                "GATEWAY_SKIP_DOTENV": "1",
                "RUNPOD_API_KEY": "rpa_test",
                "RUNPOD_POD_ID": "oldpod",
                "RUNPOD_VLLM_PORT": "8001",
            },
            clear=False,
        ), patch("claude_gateway.config.urlopen", return_value=FakeRunPodResponse()):
            settings = Settings.from_env()

        self.assertEqual(settings.vps_model_base_url, "https://newpod-8001.proxy.runpod.net/v1")
        self.assertEqual(settings.vps_fast_model_base_url, "https://newpod-8001.proxy.runpod.net/v1")

    def test_admin_can_purge_accounts_and_old_api_tokens_stop_working(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 24},
            )
            self.assertEqual(created.status_code, 200)
            old_token = created.json()["account"]["apiToken"]
            before = client.get("/v1/admin/accounts", headers=self.headers)
            self.assertEqual(len(before.json()["data"]), 1)

            purged = client.post("/v1/admin/accounts/purge", headers=self.headers, json={})
            after = client.get("/v1/admin/accounts", headers=self.headers)
            old_token_response = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {old_token}"},
                json={
                    "model": "claude-code-pro",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Diga oi"}],
                },
            )

            self.assertEqual(purged.status_code, 200)
            self.assertEqual(purged.json()["accounts"], 1)
            self.assertEqual(after.json()["data"], [])
            self.assertEqual(old_token_response.status_code, 403)

    def test_admin_can_purge_accounts_and_gift_cards_together(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            gift = client.post(
                "/v1/admin/gift-cards",
                headers=self.headers,
                json={"code": "APAGAR-TUDO", "plan": "Pro", "price": 65, "model": "sonnet"},
            )
            token = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "API", "price": 50, "durationHours": 24},
            )
            self.assertEqual(gift.status_code, 200)
            self.assertEqual(token.status_code, 200)

            purged = client.post(
                "/v1/admin/accounts/purge",
                headers=self.headers,
                json={"includeGiftCards": True},
            )
            accounts = client.get("/v1/admin/accounts", headers=self.headers)
            gift_cards = client.get("/v1/admin/gift-cards", headers=self.headers)

            self.assertEqual(purged.status_code, 200)
            self.assertEqual(purged.json()["accounts"], 1)
            self.assertEqual(purged.json()["gift_cards"], 1)
            self.assertEqual(accounts.json()["data"], [])
            self.assertEqual(gift_cards.json()["data"], [])

    def test_api_rate_limit_defaults_to_shared_token_per_client_ip(self) -> None:
        settings = make_settings()
        settings.api_rate_limit = 1
        settings.trust_proxy_headers = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        payload = {
            "model": "claude-code-pro",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Diga oi"}],
        }

        first = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer test-token", "X-Forwarded-For": "203.0.113.10"},
            json=payload,
        )
        blocked_same_ip = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer test-token", "X-Forwarded-For": "203.0.113.10"},
            json=payload,
        )
        allowed_other_ip = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer test-token", "X-Forwarded-For": "203.0.113.11"},
            json=payload,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(blocked_same_ip.status_code, 429)
        self.assertEqual(allowed_other_ip.status_code, 200)

    def test_api_rate_limit_can_be_forced_to_token_only_scope(self) -> None:
        settings = make_settings()
        settings.api_rate_limit = 1
        settings.rate_limit_token_scope = "token"
        settings.trust_proxy_headers = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        payload = {
            "model": "claude-code-pro",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Diga oi"}],
        }

        first = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer test-token", "X-Forwarded-For": "203.0.113.10"},
            json=payload,
        )
        blocked_other_ip = client.post(
            "/v1/messages",
            headers={"Authorization": "Bearer test-token", "X-Forwarded-For": "203.0.113.11"},
            json=payload,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(blocked_other_ip.status_code, 429)

    def test_oversized_context_is_rejected_instead_of_silently_trimmed(self) -> None:
        settings = make_settings()
        settings.max_request_input_chars = 1000
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 16,
                "messages": [
                    {"role": "user", "content": "contexto antigo " + ("x" * 5000)},
                    {"role": "assistant", "content": "resposta antiga"},
                    {"role": "user", "content": "responda agora"},
                ],
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(app.state.openrouter.calls, [])

    def test_quick_greeting_ignores_claude_code_system_reminders(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 16000,
                "messages": [
                    {
                        "role": "user",
                        "content": "oi\n\n<system-reminder>hidden runtime note</system-reminder>",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"][0]["text"], "Oi! Estou aqui. O que vamos resolver?")
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_vps_payload_uses_single_configured_model_and_strips_internal_fields(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "vps-main"
        settings.vps_model_api_key = ""
        client = VPSAnthropicClient(settings)

        outgoing = client._payload_for_model(
            {
                "model": "claude-code-ultra",
                "__gateway_reasoning": "high",
                "__gateway_route_decision": object(),
                "reasoning": {"effort": "high"},
                "include_reasoning": True,
                "thinking": {"type": "enabled", "budget_tokens": 4096},
                "messages": [{"role": "user", "content": "Explique uma função simples"}],
            },
            "deepseek/deepseek-v4-pro",
        )

        self.assertEqual(outgoing["model"], "vps-main")
        self.assertNotIn("__gateway_reasoning", outgoing)
        self.assertNotIn("__gateway_route_decision", outgoing)
        self.assertNotIn("reasoning", outgoing)
        self.assertNotIn("include_reasoning", outgoing)
        self.assertNotIn("thinking", outgoing)
        self.assertNotIn("Authorization", client._headers())

        settings.vps_model_api_key = "vps-secret"
        self.assertEqual(client._headers()["Authorization"], "Bearer vps-secret")
        self.assertEqual(client._headers()["x-api-key"], "vps-secret")

    def test_vps_does_not_rotate_comma_separated_api_keys(self) -> None:
        class FakePostClient:
            def __init__(self) -> None:
                self.keys: list[str] = []

            async def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
                key = str(kwargs["headers"]["x-api-key"])
                self.keys.append(key)
                return FakeHttpResponse(
                    {
                        "id": "msg_single_key",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                )

        settings = make_settings()
        settings.vps_model_base_url = "https://runpod.example/v1"
        settings.vps_model_api_format = "anthropic"
        settings.vps_model_api_key = "one-key,two-key"
        client = VPSAnthropicClient(settings)
        fake_client = FakePostClient()
        client._client = fake_client  # type: ignore[assignment]

        response = asyncio.run(
            client.complete_messages(
                {"max_tokens": 8, "messages": [{"role": "user", "content": "Oi"}]},
                settings.vps_model_id,
            )
        )

        self.assertEqual(fake_client.keys, ["one-key,two-key"])
        self.assertEqual(response["content"][0]["text"], "ok")

    def test_vps_routes_fast_and_strong_models_when_configured(self) -> None:
        settings = make_settings()
        settings.vps_model_base_url = "https://runpod.example/v1"
        settings.vps_model_id = "qwen-14b"
        settings.vps_model_api_format = "openai-chat"
        settings.vps_model_api_key = "shared-secret"
        settings.vps_fast_model_base_url = "https://runpod.example/v1"
        settings.vps_fast_model_id = "qwen-14b"
        settings.vps_strong_model_base_url = "https://runpod-strong.example/v1"
        settings.vps_strong_model_id = "qwen3-32b"
        client = VPSAnthropicClient(settings)

        fast = client._openai_chat_payload(
            {"messages": [{"role": "user", "content": "Oi"}]},
            stream=False,
            model=settings.fast_agent,
        )
        strong = client._openai_chat_payload(
            {"messages": [{"role": "user", "content": "Corrija este bug de producao"}]},
            stream=False,
            model=settings.code_agent,
        )

        self.assertEqual(fast["model"], "qwen-14b")
        self.assertEqual(strong["model"], "qwen3-32b")
        strong_target = client._target_for_model(settings.code_agent)
        self.assertEqual(
            client._url("/v1/chat/completions", strong_target),
            "https://runpod-strong.example/v1/chat/completions",
        )

    def test_vps_routes_fast_model_when_only_fast_target_is_configured(self) -> None:
        settings = make_settings()
        settings.vps_model_base_url = "https://runpod-default.example/v1"
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        settings.vps_fast_model_base_url = "https://runpod-fast.example/v1"
        settings.vps_fast_model_id = "qwen3-14b"
        settings.vps_strong_model_base_url = ""
        settings.vps_strong_model_id = ""
        client = VPSAnthropicClient(settings)

        fast = client._openai_chat_payload(
            {"messages": [{"role": "user", "content": "Oi"}]},
            stream=False,
            model=settings.fast_agent,
        )
        fast_target = client._target_for_model(settings.fast_agent)

        self.assertEqual(fast["model"], "qwen3-14b")
        self.assertEqual(
            client._url("/v1/chat/completions", fast_target),
            "https://runpod-fast.example/v1/chat/completions",
        )

    def test_vps_openai_chat_adds_no_think_for_fast_qwen3_requests(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_reasoning": "none",
                "messages": [{"role": "user", "content": "Responda rapido."}],
            },
            stream=False,
            model=settings.fast_agent,
        )

        self.assertEqual(outgoing["messages"][0]["content"], "/no_think\n\nResponda rapido.")
        self.assertEqual(outgoing["chat_template_kwargs"], {"enable_thinking": False})

    def test_vps_openai_chat_adds_no_think_for_fast_non_qwen_without_extra_body(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "generic-coder"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_reasoning": "none",
                "messages": [{"role": "user", "content": "Responda rapido."}],
            },
            stream=False,
            model=settings.fast_agent,
        )

        self.assertEqual(outgoing["messages"][0]["content"], "/no_think\n\nResponda rapido.")
        self.assertNotIn("chat_template_kwargs", outgoing)

    def test_vps_openai_chat_adds_no_think_for_qwen3_tool_requests(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "Use a ferramenta e responda."}],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )

        self.assertIn("Local workspace tool behavior override", outgoing["messages"][0]["content"])
        self.assertTrue(outgoing["messages"][1]["content"].startswith("/no_think\n\nUse a ferramenta e responda."))
        self.assertEqual(outgoing["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(outgoing["tool_choice"], "required")

    def test_vps_openai_chat_adds_no_think_for_claude_code_qwen3_requests(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "Responda no terminal."}],
                "tools": [],
            },
            stream=True,
        )

        self.assertEqual(outgoing["messages"][0]["content"], "/no_think\n\nResponda no terminal.")
        self.assertEqual(outgoing["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("tool_choice", outgoing)

    def test_vps_openai_chat_allows_qwen_thinking_for_high_text_only_requests(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "Analise profundamente a arquitetura."}],
            },
            stream=True,
        )

        self.assertEqual(outgoing["messages"][0]["content"], "Analise profundamente a arquitetura.")
        self.assertNotIn("chat_template_kwargs", outgoing)

    def test_vps_openai_chat_guides_claude_code_to_use_tools_without_asking(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "system": "You are a Claude agent.",
                "messages": [{"role": "user", "content": "Analise meu projeto."}],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )

        self.assertIn("immediately use the available tools", outgoing["messages"][0]["content"])
        self.assertIn("ignore dependency/cache folders", outgoing["messages"][0]["content"])
        self.assertIn("Never end with permission questions", outgoing["messages"][0]["content"])
        self.assertIn("immediately call Read again", outgoing["messages"][0]["content"])
        self.assertIn("Answer in the user's language", outgoing["messages"][0]["content"])
        self.assertIn("call LS/Glob/Grep to discover files", outgoing["messages"][0]["content"])
        self.assertIn("Execute the user's project request now", outgoing["messages"][-1]["content"])
        self.assertIn("Never ask permission to begin or continue", outgoing["messages"][-1]["content"])
        self.assertIn("retry Read immediately", outgoing["messages"][-1]["content"])
        self.assertIn("Reply in the same language as the user", outgoing["messages"][-1]["content"])
        self.assertEqual(outgoing["tool_choice"], "required")
        self.assertNotIn("stop", outgoing)

        after_tool = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {"role": "user", "content": "Analise meu projeto."},
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "README.md"}],
                    },
                ],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertNotIn("tool_choice", after_tool)
        self.assertEqual(after_tool["messages"][-2], {"role": "tool", "tool_call_id": "toolu_1", "content": "README.md"})
        self.assertIn("Use the latest tool result exactly once", after_tool["messages"][-1]["content"])
        self.assertIn("Do not repeat the same tool call", after_tool["messages"][-1]["content"])

        generic_after_tool = {
            "id": "chatcmpl_generic_after_tool",
            "choices": [{"message": {"content": "Entendi. Como posso ajudar você hoje?"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 6},
        }
        generic_anthropic = client._anthropic_from_openai_chat(generic_after_tool)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {"role": "user", "content": "Analise meu projeto."},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_glob", "name": "Glob", "input": {"pattern": "**/*.md"}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_glob", "content": "README.md\ndocs/BENCHMARK.md"}],
                    },
                ],
                "tools": [{"name": "Glob", "input_schema": {"type": "object"}}],
            },
            generic_anthropic,
        )
        self.assertEqual(generic_anthropic["stop_reason"], "end_turn")
        self.assertIn("README.md", generic_anthropic["content"][0]["text"])
        self.assertNotIn("Como posso ajudar", generic_anthropic["content"][0]["text"])

        generic_pt_after_reading = {
            "id": "chatcmpl_generic_pt_after_tool",
            "choices": [{"message": {"content": "Li 6 arquivos. Em que posso ajudá-lo agora?"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 8},
        }
        generic_pt_anthropic = client._anthropic_from_openai_chat(generic_pt_after_reading)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {"role": "user", "content": "me de um resumo"},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "README.md"}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": "# Projeto\nResumo do repo."}],
                    },
                ],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            },
            generic_pt_anthropic,
        )
        self.assertEqual(generic_pt_anthropic["stop_reason"], "end_turn")
        self.assertIn("Resumo do repo", generic_pt_anthropic["content"][0]["text"])
        self.assertNotIn("ajudá-lo", generic_pt_anthropic["content"][0]["text"])

        non_executing_after_reading = {
            "id": "chatcmpl_non_executing_after_reading",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Entendido! Vou seguir as instruções fornecidas e usar as ferramentas "
                            "disponíveis para auxiliar o usuário da melhor maneira possível."
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 18},
        }
        non_executing_anthropic = client._anthropic_from_openai_chat(non_executing_after_reading)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {
                        "role": "user",
                        "content": "Aqui temos a pasta, voce tem as orientacoes certo? trabalhe",
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_read",
                                "content": "# Projeto\nUse scripts/importar.txt e scripts/exportar.txt.",
                            }
                        ],
                    },
                ],
                "tools": [
                    {"name": "Read", "input_schema": {"type": "object"}},
                    {"name": "LS", "input_schema": {"type": "object"}},
                    {"name": "Write", "input_schema": {"type": "object"}},
                ],
            },
            non_executing_anthropic,
        )
        self.assertEqual(non_executing_anthropic["stop_reason"], "tool_use")
        self.assertEqual(non_executing_anthropic["content"][0]["type"], "tool_use")
        self.assertEqual(non_executing_anthropic["content"][0]["name"], "LS")

        english_non_executing_after_instruction = {
            "id": "chatcmpl_english_non_executing_after_instruction",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Understood! I'll follow the instructions carefully and ensure that I use the "
                            "latest tool result exactly once. Let's get started!"
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 20},
        }
        english_non_executing_anthropic = client._anthropic_from_openai_chat(
            english_non_executing_after_instruction
        )
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {
                        "role": "user",
                        "content": "Aqui temos a pasta, voce tem as orientacoes certo? trabalhe",
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_read",
                                "content": "# Projeto\nUse scripts/importar.txt e scripts/exportar.txt.",
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": (
                            "Por favor, execute as seguintes etapas com base nos documentos que voce leu: "
                            "analise, proponha a arquitetura do bot e liste os proximos passos."
                        ),
                    },
                ],
                "tools": [
                    {"name": "Read", "input_schema": {"type": "object"}},
                    {"name": "LS", "input_schema": {"type": "object"}},
                    {"name": "Write", "input_schema": {"type": "object"}},
                ],
            },
            english_non_executing_anthropic,
        )
        self.assertEqual(english_non_executing_anthropic["stop_reason"], "tool_use")
        self.assertEqual(english_non_executing_anthropic["content"][0]["type"], "tool_use")
        self.assertEqual(english_non_executing_anthropic["content"][0]["name"], "LS")

        english_generic_after_tools = {
            "id": "chatcmpl_english_generic_after_tools",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Understood! Let's proceed with the task you've requested. Please provide the "
                            "specific task or question you'd like assistance with, and I'll use the "
                            "appropriate tools to help you."
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 24},
        }
        english_generic_anthropic = client._anthropic_from_openai_chat(english_generic_after_tools)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {
                        "role": "user",
                        "content": "Aqui temos a pasta, voce tem as orientacoes certo? trabalhe",
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_ls", "name": "LS", "input": {"path": "."}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_ls", "content": "README.md"}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_bash", "name": "Bash", "input": {"command": "pwd"}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_bash", "content": "/repo"}],
                    },
                    {
                        "role": "user",
                        "content": (
                            "execute as etapas: analise as frentes, proponha arquitetura do bot e liste "
                            "arquivos para modificar."
                        ),
                    },
                ],
                "tools": [
                    {"name": "Read", "input_schema": {"type": "object"}},
                    {"name": "LS", "input_schema": {"type": "object"}},
                    {"name": "Bash", "input_schema": {"type": "object"}},
                ],
            },
            english_generic_anthropic,
        )
        self.assertEqual(english_generic_anthropic["stop_reason"], "tool_use")
        self.assertEqual(english_generic_anthropic["content"][0]["name"], "LS")

        portuguese_detail_request_after_worktree = {
            "id": "chatcmpl_portuguese_detail_request_after_worktree",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Vou analisar seu projeto agora. Por favor, forneça mais detalhes sobre os "
                            "aspectos específicos que você gostaria de melhorar, exceto pela calculadora."
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 24},
        }
        portuguese_detail_anthropic = client._anthropic_from_openai_chat(
            portuguese_detail_request_after_worktree
        )
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {
                        "role": "user",
                        "content": "analise meu projeto e veja oq eu preciso melhorrar analise tudo menos a parte da claculadora",
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_worktree",
                                "name": "EnterWorktree",
                                "input": {"name": "analysis-worktree"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_worktree", "content": "Switched."}],
                    },
                ],
                "tools": [
                    {"name": "LS", "input_schema": {"type": "object"}},
                    {"name": "Read", "input_schema": {"type": "object"}},
                    {"name": "EnterWorktree", "input_schema": {"type": "object"}},
                ],
            },
            portuguese_detail_anthropic,
        )
        self.assertEqual(portuguese_detail_anthropic["stop_reason"], "tool_use")
        self.assertEqual(portuguese_detail_anthropic["content"][0]["name"], "LS")

        portuguese_no_local_access_response = {
            "id": "chatcmpl_portuguese_no_local_access",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Desculpe, mas parece que houve um mal-entendido. Eu não consigo acessar ou "
                            "analisar diretamente seu projeto no momento, pois não tenho acesso aos arquivos "
                            "locais do seu computador.\n\nPara poder ajudar de maneira eficaz, você precisará "
                            "fornecer mais informações sobre seu projeto."
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 35},
        }
        portuguese_no_access_anthropic = client._anthropic_from_openai_chat(
            portuguese_no_local_access_response
        )
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {
                        "role": "user",
                        "content": "analise meu projeto e veja oq eu preciso melhorrar",
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_ls",
                                "name": "LS",
                                "input": {"path": "."},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_ls", "content": "README.md"}],
                    },
                ],
                "tools": [
                    {"name": "LS", "input_schema": {"type": "object"}},
                    {"name": "Read", "input_schema": {"type": "object"}},
                ],
            },
            portuguese_no_access_anthropic,
        )
        self.assertEqual(portuguese_no_access_anthropic["stop_reason"], "tool_use")
        self.assertEqual(portuguese_no_access_anthropic["content"][0]["name"], "LS")

        false_done_after_reading = {
            "id": "chatcmpl_false_done_after_reading",
            "choices": [{"message": {"content": "Criei os arquivos e esta tudo pronto."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 8},
        }
        false_done_anthropic = client._anthropic_from_openai_chat(false_done_after_reading)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {"role": "user", "content": "crie uma calculadora em python"},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "README.md"}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": "# Projeto"}],
                    },
                ],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            },
            false_done_anthropic,
        )
        self.assertEqual(false_done_anthropic["stop_reason"], "end_turn")
        self.assertIn("nao apliquei nenhuma mudanca fisica", false_done_anthropic["content"][0]["text"])
        self.assertNotIn("tudo pronto", false_done_anthropic["content"][0]["text"])

        status_after_only_reading = {
            "id": "chatcmpl_status_after_only_reading",
            "choices": [{"message": {"content": "Resumo do Sistema\nO sistema possui..."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 8},
        }
        status_anthropic = client._anthropic_from_openai_chat(status_after_only_reading)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {"role": "user", "content": "crie uma calculadora em python"},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "README.md"}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": "# Projeto"}],
                    },
                    {"role": "user", "content": "fez as alterações?"},
                ],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            },
            status_anthropic,
        )
        self.assertEqual(status_anthropic["stop_reason"], "end_turn")
        self.assertIn("Nao. Ate agora nao houve alteracao fisica", status_anthropic["content"][0]["text"])
        self.assertNotIn("Resumo do Sistema", status_anthropic["content"][0]["text"])

        after_only_inspection_for_edit = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {"role": "user", "content": "quero que vc fassa o site do neymar"},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_ls", "name": "LS", "input": {"path": "."}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_ls", "content": "README.md"}],
                    },
                ],
                "tools": [
                    {"name": "LS", "input_schema": {"type": "object"}},
                    {"name": "Write", "input_schema": {"type": "object"}},
                ],
            },
            stream=True,
        )
        self.assertEqual(after_only_inspection_for_edit["tool_choice"], "required")

        after_confirmation_keeps_original_edit_goal = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {"role": "user", "content": "quero que vc fassa o site do neymar"},
                    {"role": "assistant", "content": "Posso começar pela interface."},
                    {"role": "user", "content": "concordo pode fazer"},
                ],
                "tools": [
                    {"name": "Read", "input_schema": {"type": "object"}},
                    {"name": "Write", "input_schema": {"type": "object"}},
                ],
            },
            stream=True,
        )
        self.assertEqual(after_confirmation_keeps_original_edit_goal["tool_choice"], "required")

        after_write_for_edit = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {"role": "user", "content": "quero que vc fassa o site do neymar"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_write",
                                "name": "Write",
                                "input": {"file_path": "index.html", "content": "<!doctype html>"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "toolu_write", "content": "ok"}],
                    },
                ],
                "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertNotIn("tool_choice", after_write_for_edit)

        failed_write_payload = {
            "__gateway_client": "claude-code",
            "__gateway_reasoning": "high",
            "messages": [
                {"role": "user", "content": "crie uma calculadora em python"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_write_failed",
                            "name": "Write",
                            "input": {"file_path": "calculadora.py", "content": "print('ok')"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_write_failed",
                            "is_error": True,
                            "content": "This background session hasn't isolated its changes yet. Call EnterWorktree first.",
                        }
                    ],
                },
            ],
            "tools": [
                {"name": "EnterWorktree", "input_schema": {"type": "object"}},
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
        }
        after_failed_write_requires_worktree = client._openai_chat_payload(
            failed_write_payload,
            stream=True,
        )
        self.assertEqual(after_failed_write_requires_worktree["tool_choice"], "required")

        ignored_worktree_response = {
            "id": "chatcmpl_worktree_retry",
            "choices": [{"message": {"content": "Vou tentar novamente."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        worktree_anthropic = client._anthropic_from_openai_chat(ignored_worktree_response)
        client._ensure_required_tool_call(
            failed_write_payload,
            worktree_anthropic,
        )
        self.assertEqual(worktree_anthropic["content"][0]["name"], "EnterWorktree")
        self.assertEqual(worktree_anthropic["content"][0]["input"], {"name": "calculadora-worktree"})

        broken_worktree_response = {
            "id": "chatcmpl_worktree_retry_json",
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"name":"EnterWorktree","arguments":{}}\n```',
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        broken_worktree_anthropic = client._anthropic_from_openai_chat(broken_worktree_response)
        client._ensure_required_tool_call(
            failed_write_payload,
            broken_worktree_anthropic,
        )
        self.assertEqual(broken_worktree_anthropic["content"][0]["name"], "EnterWorktree")
        self.assertEqual(broken_worktree_anthropic["content"][0]["input"], {"name": "calculadora-worktree"})

        edit_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "Corrija o bug e rode os testes."}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(edit_request["tool_choice"], "required")
        self.assertIn("Execute the user's project request now", edit_request["messages"][-1]["content"])

        unaccented_create_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "faca um site em uma nova pasta"}],
                "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(unaccented_create_request["tool_choice"], "required")
        self.assertIn("do not answer with instructions", unaccented_create_request["messages"][-1]["content"])

        terminal_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "execute isso no terminal"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(terminal_request["tool_choice"], "required")

        implicit_command_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "deixa esse projeto funcionando"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(implicit_command_request["tool_choice"], "required")

        question_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "qual seu nome?"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(question_request["tool_choice"], "none")

        workspace_question_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "nao consegue acessar o diretorio?"}],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(workspace_question_request["tool_choice"], "required")
        self.assertIn("Execute the user's project request now", workspace_question_request["messages"][-1]["content"])

        mixed_question_command_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {
                        "role": "user",
                        "content": "Aqui temos a pasta, voce tem as orientacoes certo? trabalhe",
                    }
                ],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(mixed_question_command_request["tool_choice"], "required")

        greeting_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "eae"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )
        self.assertEqual(greeting_request["tool_choice"], "none")

        simple_text_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "responda apenas ping"}],
                "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )
        self.assertEqual(simple_text_request["tool_choice"], "none")

        auto_tool_choice_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "mande meu projeto pro github"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )
        self.assertEqual(auto_tool_choice_request["tool_choice"], "required")

        site_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "quero que vc fassa o site do neymar"}],
                "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )
        self.assertEqual(site_request["tool_choice"], "required")

        delete_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "apague a calculadora"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )
        self.assertEqual(delete_request["tool_choice"], "required")

        continuation_all_request = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {
                        "role": "user",
                        "content": "agora eu quero que voce analise todo meu projeto e diga oq vc acha dele atualmente",
                    },
                    {"role": "assistant", "content": "Claro. Quais arquivos voce quer que eu examine?"},
                    {"role": "user", "content": "todos"},
                ],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )
        self.assertEqual(continuation_all_request["tool_choice"], "required")

        guessed_analysis = {
            "id": "chatcmpl_guessed_analysis",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Vou começar analisando diretório atual. Vejo .gitignore, .claude, "
                            "calculadora.py e index.html."
                        )
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 16},
        }
        guessed_analysis_anthropic = client._anthropic_from_openai_chat(guessed_analysis)
        client._ensure_required_tool_call(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {
                        "role": "user",
                        "content": "agora eu quero que voce analise todo meu projeto e diga oq vc acha dele atualmente",
                    },
                    {"role": "assistant", "content": "Claro. Quais arquivos voce quer que eu examine?"},
                    {"role": "user", "content": "todos"},
                ],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            },
            guessed_analysis_anthropic,
        )
        self.assertEqual(guessed_analysis_anthropic["stop_reason"], "tool_use")
        self.assertEqual(guessed_analysis_anthropic["content"][0]["name"], "LS")

    def test_vps_openai_chat_guides_desktop_workspace_tools_for_file_edits(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_reasoning": "high",
                "messages": [{"role": "user", "content": "Me ajude com alterações em um arquivo."}],
                "tools": [
                    {"name": "read_file", "input_schema": {"type": "object"}},
                    {"name": "apply_patch", "input_schema": {"type": "object"}},
                ],
                "tool_choice": {"type": "auto"},
            },
            stream=True,
        )

        self.assertEqual(outgoing["tool_choice"], "required")
        self.assertIn("Local workspace tool behavior override", outgoing["messages"][0]["content"])
        self.assertIn("read_file/list_files/apply_patch/write_file/run_tests", outgoing["messages"][0]["content"])
        self.assertIn("use Brazilian Portuguese", outgoing["messages"][0]["content"])
        self.assertIn("choose a simple filename", outgoing["messages"][0]["content"])
        self.assertIn("Execute the user's project request now", outgoing["messages"][-1]["content"])

    def test_vps_required_tool_fallback_reads_explicit_absolute_path(self) -> None:
        settings = make_settings()
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {
                    "role": "user",
                    "content": "oq e o arquivo /Users/allanmatheus/Documents/claudecode/claude_gateway/vps_scheduler.py",
                }
            ],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        }

        fallback = client._fallback_tool_use_for_required_action(payload)

        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback["name"], "Read")
        self.assertEqual(
            fallback["input"],
            {"file_path": "/Users/allanmatheus/Documents/claudecode/claude_gateway/vps_scheduler.py"},
        )

    def test_vps_required_tool_fallback_uses_path_for_mcp_read_file(self) -> None:
        settings = make_settings()
        client = VPSAnthropicClient(settings)
        payload = {
            "messages": [{"role": "user", "content": "leia claude_gateway/vps_scheduler.py"}],
            "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
        }

        fallback = client._fallback_tool_use_for_required_action(payload)

        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback["name"], "read_file")
        self.assertEqual(fallback["input"], {"path": "claude_gateway/vps_scheduler.py"})

    def test_vps_required_tool_fallback_does_not_repeat_same_gateway_inspection(self) -> None:
        settings = make_settings()
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {"role": "user", "content": "concordo pode fazer o batch dos txt"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_gateway_inspect_0",
                            "name": "LS",
                            "input": {"path": "."},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_gateway_inspect_0",
                            "content": "importar.txt\nexportar.txt",
                        }
                    ],
                },
            ],
            "tools": [
                {"name": "LS", "input_schema": {"type": "object"}},
                {"name": "Glob", "input_schema": {"type": "object"}},
                {"name": "Bash", "input_schema": {"type": "object"}},
            ],
        }

        fallback = client._fallback_tool_use_for_required_action(payload)

        self.assertIsNotNone(fallback)
        assert fallback is not None
        self.assertEqual(fallback["name"], "Glob")
        self.assertEqual(fallback["input"], {"pattern": "**/*"})

    def test_vps_openai_chat_retries_400_required_tool_choice_without_backend_flag(self) -> None:
        class FakeResponse:
            def __init__(self, status_code: int, data: dict[str, Any] | None = None, text: str = "") -> None:
                self.status_code = status_code
                self._data = data or {}
                self.text = text

            def json(self) -> dict[str, Any]:
                return self._data

        class FakePostClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def post(self, *_args, json: dict[str, Any], **_kwargs) -> FakeResponse:
                self.calls.append(json)
                if len(self.calls) == 1:
                    return FakeResponse(400, text="tool_choice required is not supported")
                return FakeResponse(
                    200,
                    {
                        "id": "chatcmpl_retry",
                        "choices": [{"message": {"content": "Vou inspecionar."}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 3},
                    },
                )

        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        fake_client = FakePostClient()
        client._client = fake_client  # type: ignore[assignment]

        response = asyncio.run(
            client._complete_openai_chat(
                {
                    "__gateway_client": "claude-code",
                    "messages": [{"role": "user", "content": "analise o projeto"}],
                    "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
                },
                "claude-code-pro",
            )
        )

        self.assertEqual(len(fake_client.calls), 2)
        self.assertEqual(fake_client.calls[0]["tool_choice"], "required")
        self.assertNotIn("tool_choice", fake_client.calls[1])
        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(response["content"][0]["name"], "LS")

    def test_vps_openai_chat_stream_retries_400_required_tool_choice_without_backend_flag(self) -> None:
        class FakeStreamResponse:
            def __init__(self, status_code: int, body: bytes = b"", chunks: list[bytes] | None = None) -> None:
                self.status_code = status_code
                self._body = body
                self._chunks = chunks or []

            async def __aenter__(self) -> "FakeStreamResponse":
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def aread(self) -> bytes:
                return self._body

            async def aiter_bytes(self):
                for chunk in self._chunks:
                    yield chunk

        class FakeStreamClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def stream(self, *_args, json: dict[str, Any], **_kwargs) -> FakeStreamResponse:
                self.calls.append(json)
                if len(self.calls) == 1:
                    return FakeStreamResponse(400, b"unsupported tool_choice required")
                return FakeStreamResponse(
                    200,
                    chunks=[
                        b'data: {"choices":[{"delta":{"content":"Vou inspecionar."},"finish_reason":null}]}\n\n',
                        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
                        b"data: [DONE]\n\n",
                    ],
                )

        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        fake_client = FakeStreamClient()
        client._client = fake_client  # type: ignore[assignment]

        body = b"".join(
            asyncio.run(
                _collect_async_bytes(
                    client._stream_openai_chat(
                        {
                            "__gateway_client": "claude-code",
                            "messages": [{"role": "user", "content": "analise o projeto"}],
                            "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
                        },
                        "claude-code-pro",
                    )
                )
            )
        ).decode("utf-8")

        self.assertEqual(len(fake_client.calls), 2)
        self.assertEqual(fake_client.calls[0]["tool_choice"], "required")
        self.assertNotIn("tool_choice", fake_client.calls[1])
        self.assertIn('"name": "LS"', body)
        self.assertIn('"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_uses_current_claude_code_prompt_for_action_detection(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "<system-reminder>arquivo projeto read test</system-reminder>\n\n"
                            "qual seu nome?\n"
                            "from __future__ import annotations\n"
                            "print('historico com projeto e arquivos')"
                        ),
                    }
                ],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )

        self.assertEqual(outgoing["tool_choice"], "none")
        self.assertNotIn("Execute the user's project request now", str(outgoing["messages"]))

    def test_vps_openai_chat_compacts_large_tool_schemas_to_leave_output_room(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        tools = [
            {
                "name": f"tool_{index}",
                "description": "descricao longa " * 600,
                "input_schema": {
                    "type": "object",
                    "description": "schema longo " * 200,
                    "properties": {
                        "path": {"type": "string", "description": "caminho " * 80},
                        "options": {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "description": "modo " * 80},
                            },
                        },
                    },
                    "required": ["path"],
                },
            }
            for index in range(28)
        ]

        outgoing = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "__gateway_reasoning": "high",
                "max_tokens": 64000,
                "system": "instrucoes do agente " * 900,
                "messages": [{"role": "user", "content": "Responda no terminal."}],
                "tools": tools,
            },
            stream=True,
        )

        estimated_input = client._estimate_openai_chat_input_tokens(outgoing["messages"], outgoing.get("tools", []))
        self.assertLessEqual(estimated_input, 18000)
        self.assertGreater(outgoing["max_tokens"], 1000)

    def test_vps_openai_chat_caps_output_to_fit_context_window(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "system": "instrucoes longas " * 3000,
                "max_tokens": 16000,
                "messages": [{"role": "user", "content": "oi"}],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=False,
        )

        estimated_input = client._estimate_openai_chat_input_tokens(outgoing["messages"], outgoing.get("tools", []))
        self.assertLess(outgoing["max_tokens"], 16000)
        self.assertLessEqual(estimated_input + outgoing["max_tokens"], 24576 - 512)
        self.assertEqual(outgoing["tool_choice"], "none")

    def test_vps_code_tool_requests_use_short_backend_timeout(self) -> None:
        settings = make_settings()
        settings.vps_model_timeout_seconds = 55
        settings.vps_code_timeout_seconds = 8
        client = VPSAnthropicClient(settings)

        code_timeout = client._stream_timeout(
            {
                "__gateway_client": "claude-code",
                "messages": [{"role": "user", "content": "leia o projeto"}],
                "tools": [{"name": "LS", "input_schema": {"type": "object"}}],
            }
        )
        text_timeout = client._stream_timeout(
            {
                "messages": [{"role": "user", "content": "explique arquitetura"}],
            }
        )

        self.assertEqual(code_timeout.read, 8)
        self.assertEqual(code_timeout.connect, 8)
        self.assertEqual(text_timeout.read, 55)

    def test_vps_openai_chat_respects_configured_context_window(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        settings.vps_openai_chat_context_tokens = 24_576
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "max_tokens": 14638,
                "messages": [{"role": "user", "content": "contexto " * 11000}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
            stream=False,
        )

        estimated_input = client._estimate_openai_chat_input_tokens(outgoing["messages"], outgoing.get("tools", []))
        self.assertLess(outgoing["max_tokens"], 14638)
        self.assertLessEqual(
            estimated_input + outgoing["max_tokens"],
            settings.vps_openai_chat_context_tokens - 512,
        )

    def test_vps_openai_chat_trims_large_history_to_fit_input_budget(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "system": "system guidance " * 5000,
                "max_tokens": 16000,
                "messages": [
                    {"role": "user", "content": "historico antigo " * 4000},
                    {"role": "assistant", "content": "resposta antiga " * 4000},
                    {"role": "user", "content": "como vc ta"},
                ],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=False,
        )

        joined = "\n".join(message["content"] for message in outgoing["messages"])
        estimated_input = client._estimate_openai_chat_input_tokens(outgoing["messages"], outgoing.get("tools", []))
        self.assertLessEqual(estimated_input, 18000)
        self.assertLessEqual(estimated_input + outgoing["max_tokens"], 24576 - 512)
        self.assertIn("como vc ta", joined)
        self.assertNotIn("historico antigo historico antigo", joined)
        self.assertIn("previous content omitted", joined)

    def test_vps_openai_chat_format_converts_anthropic_payload(self) -> None:
        settings = make_settings()
        settings.vps_model_base_url = "https://runpod.example/v1"
        settings.vps_model_id = "qwen-14b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "model": "claude-code-ultra",
                "__gateway_reasoning": "high",
                "system": "Seja direto.",
                "max_tokens": 99,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Oi"}]}],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            stream=False,
        )

        self.assertEqual(client.chat_completions_url, "https://runpod.example/v1/chat/completions")
        self.assertEqual(outgoing["model"], "qwen-14b")
        self.assertEqual(outgoing["messages"][0], {"role": "system", "content": "Seja direto."})
        self.assertEqual(outgoing["messages"][1], {"role": "user", "content": "/no_think\n\nOi"})
        self.assertNotIn("tools", outgoing)
        self.assertEqual(outgoing["tool_choice"], "none")
        self.assertNotIn("__gateway_reasoning", outgoing)

        tool_choice = client._openai_chat_payload(
            {
                "messages": [{"role": "user", "content": "Use a ferramenta."}],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "tool", "name": "read_file"},
            },
            stream=False,
        )
        self.assertEqual(
            tool_choice["tool_choice"],
            {"type": "function", "function": {"name": "read_file"}},
        )

        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_test",
                "choices": [{"message": {"content": "Olá!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        )
        self.assertEqual(response["model"], "qwen-14b")
        self.assertEqual(response["content"], [{"type": "text", "text": "Olá!"}])
        self.assertEqual(response["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_vps_openai_chat_response_separates_qwen_thinking_text(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_test",
                "choices": [
                    {
                        "message": {"content": "<think>\n\nrascunho interno\n</think>\n\nResposta limpa."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        )

        self.assertEqual(
            response["content"],
            [
                {"type": "thinking", "thinking": "rascunho interno"},
                {"type": "text", "text": "Resposta limpa."},
            ],
        )

    def test_vps_openai_chat_falls_back_to_tool_when_required_tool_is_ignored(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        payload = {
            "__gateway_client": "claude-code",
            "messages": [{"role": "user", "content": "mande meu projeto pro github"}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        }
        response = {
            "id": "chatcmpl_test",
            "choices": [{"message": {"content": "Voce precisa executar git push."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }

        anthropic = client._anthropic_from_openai_chat(response)
        client._ensure_required_tool_call(payload, anthropic)

        self.assertEqual(anthropic["stop_reason"], "tool_use")
        self.assertEqual(anthropic["content"][0]["type"], "tool_use")
        self.assertEqual(anthropic["content"][0]["name"], "Bash")
        self.assertIn("find .", anthropic["content"][0]["input"]["command"])

    def test_vps_openai_chat_converts_textual_tool_use_to_real_tool_call(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_text_tool",
                "choices": [
                    {
                        "message": {
                            "content": 'Tool use: {"name":"Read","input":{"file_path":"docs/visual-direction.md"}}'
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(
            response["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call_text_0",
                    "name": "Read",
                    "input": {"file_path": "docs/visual-direction.md"},
                }
            ],
        )

        raw_json_response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_raw_json_tool",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "name": "Write",
                                    "arguments": {
                                        "file_path": "/Users/allanmatheus/Documents/claudecode/teste.txt",
                                        "content": "oi",
                                    },
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        self.assertEqual(raw_json_response["stop_reason"], "tool_use")
        self.assertEqual(raw_json_response["content"][0]["name"], "Write")
        self.assertEqual(
            raw_json_response["content"][0]["input"],
            {
                "file_path": "/Users/allanmatheus/Documents/claudecode/teste.txt",
                "content": "oi",
            },
        )

        fenced_json_response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_fenced_json_tool",
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Entendi. Vou usar o `EnterWorktre`.\n\n"
                                "```json\n"
                                '{"name":"EnterWorktre","arguments":{}}\n'
                                "```"
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        self.assertEqual(fenced_json_response["stop_reason"], "tool_use")
        self.assertEqual(fenced_json_response["content"][0]["name"], "EnterWorktree")
        self.assertEqual(fenced_json_response["content"][0]["input"], {})

    def test_vps_openai_chat_converts_file_code_blocks_to_write_tools(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        payload = {
            "__gateway_client": "claude-code",
            "messages": [{"role": "user", "content": "fassa o site do neymar"}],
            "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
        }
        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_file_blocks",
                "choices": [
                    {
                        "message": {
                            "content": (
                                "index.html\n\n"
                                "```html\n"
                                "<!doctype html>\n<title>Neymar</title>\n"
                                "```\n\n"
                                "styles.css\n\n"
                                "```css\n"
                                "body { margin: 0; }\n"
                                "```\n"
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        client._ensure_required_tool_call(payload, response)

        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual([block["name"] for block in response["content"]], ["Write", "Write"])
        self.assertEqual(response["content"][0]["input"]["file_path"], "index.html")
        self.assertIn("<!doctype html>", response["content"][0]["input"]["content"])
        self.assertEqual(response["content"][1]["input"]["file_path"], "styles.css")
        self.assertIn("body { margin: 0; }", response["content"][1]["input"]["content"])

    def test_vps_openai_chat_converts_file_code_blocks_to_mcp_write_file(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        payload = {
            "messages": [{"role": "user", "content": "crie uma calculadora em python"}],
            "tools": [{"name": "write_file", "input_schema": {"type": "object"}}],
        }
        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_mcp_file_block",
                "choices": [
                    {
                        "message": {
                            "content": (
                                "calculadora.py\n\n"
                                "```python\n"
                                "def somar(a, b):\n"
                                "    return a + b\n"
                                "```\n"
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        client._ensure_required_tool_call(payload, response)

        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(response["content"][0]["name"], "write_file")
        self.assertEqual(response["content"][0]["input"]["path"], "calculadora.py")
        self.assertIn("def somar", response["content"][0]["input"]["content"])

    def test_vps_openai_chat_converts_chatty_file_draft_to_write_tool(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        payload = {
            "__gateway_client": "claude-code",
            "messages": [{"role": "user", "content": "pegue o Calculadora.py e transforme em uma lista de tarefas"}],
            "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
        }
        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_chatty_file_draft",
                "choices": [
                    {
                        "message": {
                            "content": (
                                "Calculadora.py\n\n"
                                "```python\n"
                                "import json\n\n"
                                "def main():\n"
                                "    tarefas = []\n"
                                "    print('Lista de tarefas')\n\n"
                                "if __name__ == '__main__':\n"
                                "    main()\n"
                                "```\n\n"
                                "Este código cria uma aplicação de lista de tarefas funcional editável. "
                                "Você pode adicionar, remover e listar tarefas."
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        client._ensure_required_tool_call(payload, response)

        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(response["content"][0]["name"], "Write")
        self.assertEqual(response["content"][0]["input"]["file_path"], "Calculadora.py")
        self.assertIn("Lista de tarefas", response["content"][0]["input"]["content"])
        self.assertNotIn("Este código cria", response["content"][0]["input"]["content"])

    def test_vps_openai_chat_preserves_tool_history_as_native_openai_messages(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "__gateway_client": "claude-code",
                "messages": [
                    {"role": "user", "content": "Leia o README."},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_read",
                                "content": "# Projeto\n\nConteudo.",
                            }
                        ],
                    },
                ],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
            },
            stream=True,
        )

        serialized = json.dumps(outgoing["messages"])
        self.assertNotIn("Tool use:", serialized)
        self.assertIn(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "toolu_read",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": '{"file_path": "README.md"}',
                        },
                    }
                ],
            },
            outgoing["messages"],
        )
        self.assertIn(
            {"role": "tool", "tool_call_id": "toolu_read", "content": "# Projeto\n\nConteudo."},
            outgoing["messages"],
        )

    def test_vps_openai_chat_preserves_image_attachments_as_image_url_parts(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        outgoing = client._openai_chat_payload(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Analise esta imagem."},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aW1hZ2U=",
                                },
                            },
                        ],
                    }
                ],
            },
            stream=False,
        )

        content = outgoing["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "Analise esta imagem."})
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}},
        )

    def test_vps_openai_chat_normalizes_claude_code_tool_aliases(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        response = client._anthropic_from_openai_chat(
            {
                "id": "chatcmpl_test",
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_write",
                                    "function": {
                                        "name": "Write",
                                        "arguments": json.dumps(
                                            {"path": "site-neymar/index.html", "content": "<html></html>"}
                                        ),
                                    },
                                },
                                {
                                    "id": "call_bash",
                                    "function": {
                                        "name": "Bash",
                                        "arguments": json.dumps({"cmd": "mkdir -p site-neymar"}),
                                    },
                                },
                                {
                                    "id": "call_grep",
                                    "function": {
                                        "name": "Grep",
                                        "arguments": json.dumps(
                                            {
                                                "query": "Neymar",
                                                "files": ["index.html", "styles.css"],
                                                "file_pattern": "*.html",
                                            }
                                        ),
                                    },
                                },
                                {
                                    "id": "call_glob",
                                    "function": {
                                        "name": "Glob",
                                        "arguments": json.dumps({"glob": "**/*.py", "dir": "claude_gateway"}),
                                    },
                                },
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )

        self.assertEqual(
            response["content"][0]["input"],
            {"file_path": "site-neymar/index.html", "content": "<html></html>"},
        )
        self.assertEqual(response["content"][1]["input"], {"command": "mkdir -p site-neymar"})
        self.assertEqual(response["content"][2]["input"], {"glob": "*.html", "pattern": "Neymar"})
        self.assertEqual(response["content"][3]["input"], {"path": "claude_gateway", "pattern": "**/*.py"})

    def test_vps_openai_chat_stream_is_converted_to_anthropic_sse(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen-14b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"Ol"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks()))))
        self.assertIn(b"event: message_start", body)
        self.assertIn(b'"text": "Ol"', body)
        self.assertIn(b'"text": "a"', body)
        self.assertIn(b"event: message_stop", body)

    def test_vps_openai_chat_stream_separates_qwen_thinking_sse(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"<think>ras"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"cunho</think>Resposta"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks()))))

        self.assertIn(b'"content_block": {"type": "thinking", "thinking": ""}', body)
        self.assertIn(b'"delta": {"type": "thinking_delta", "thinking": "ras"', body)
        self.assertIn(b'"delta": {"type": "thinking_delta", "thinking": "cunho"', body)
        self.assertIn(b'"content_block": {"type": "text", "text": ""}', body)
        self.assertIn(b'"delta": {"type": "text_delta", "text": "Resposta"', body)

    def test_vps_openai_chat_stream_does_not_buffer_when_tool_call_is_required(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"Use git push."}}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(
            asyncio.run(
                _collect_async_bytes(client._openai_sse_to_anthropic(chunks(), require_tool_call=False))
            )
        )
        self.assertIn(b"event: message_start", body)
        self.assertIn(b"Use git push.", body)
        self.assertIn(b"event: message_stop", body)

    def test_vps_openai_chat_stream_converts_textual_tool_use_to_tool_call(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"Tool use: {\\"name\\":\\"Read\\","}}]}\n\n'
            yield (
                b'data: {"choices":[{"delta":{"content":"\\"input\\":{\\"file_path\\":\\"docs/design-system.md\\"}}"}}]}\n\n'
            )
            yield b"data: [DONE]\n\n"

        body = b"".join(
            asyncio.run(
                _collect_async_bytes(client._openai_sse_to_anthropic(chunks(), require_tool_call=True))
            )
        )

        self.assertIn(b'"content_block": {"type": "tool_use", "id": "call_text_0", "name": "Read", "input": {}}', body)
        self.assertIn(b'"partial_json": "{\\"file_path\\": \\"docs/design-system.md\\"}"', body)
        self.assertNotIn(b"Tool use:", body)
        self.assertIn(b'"stop_reason": "tool_use"', body)

        async def raw_json_chunks():
            yield (
                "data: "
                + json.dumps({"choices": [{"delta": {"content": '{\n  "name": "Write",'}}]})
                + "\n\n"
            ).encode()
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": '\n  "arguments": {\n    "file_path": "teste.txt",\n    "content": "oi"\n  }\n}'
                                }
                            }
                        ]
                    }
                )
                + "\n\n"
            ).encode()
            yield b"data: [DONE]\n\n"

        raw_body = b"".join(
            asyncio.run(
                _collect_async_bytes(client._openai_sse_to_anthropic(raw_json_chunks(), require_tool_call=True))
            )
        )
        self.assertIn(b'"name": "Write"', raw_body)
        self.assertIn(b'\\"file_path\\": \\"teste.txt\\"', raw_body)
        self.assertNotIn(b'"type": "text_delta"', raw_body)
        self.assertIn(b'"stop_reason": "tool_use"', raw_body)

    def test_vps_openai_chat_stream_falls_back_to_tool_when_required_tool_is_ignored(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        payload = {
            "__gateway_client": "claude-code",
            "messages": [{"role": "user", "content": "quero que vc fassa o site do neymar"}],
            "tools": [
                {"name": "Bash", "input_schema": {"type": "object"}},
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
        }

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"Vamos criar index.html e style.css."}}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(
            asyncio.run(
                _collect_async_bytes(
                    client._openai_sse_to_anthropic(
                        chunks(),
                        require_tool_call=True,
                        payload=payload,
                    )
                )
            )
        )

        self.assertIn(b'"content_block": {"type": "tool_use"', body)
        self.assertIn(b'"name": "Bash"', body)
        self.assertIn(b"find .", body)
        self.assertNotIn(b"Vamos criar index.html", body)
        self.assertIn(b'"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_stream_blocks_generic_reply_after_tool_result(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {"role": "user", "content": "me de um resumo"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_glob", "name": "Glob", "input": {"pattern": "**/*.md"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_glob", "content": "README.md\ndocs/BENCHMARK.md"}],
                },
            ],
            "tools": [{"name": "Glob", "input_schema": {"type": "object"}}],
        }

        async def chunks():
            yield 'data: {"choices":[{"delta":{"content":"Li 6 arquivos. Em que posso ajudá-lo agora?"}}]}\n\n'.encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks(), payload=payload))))

        self.assertIn(b"README.md", body)
        self.assertNotIn(b"posso", body)
        self.assertIn(b'"stop_reason": "end_turn"', body)

    def test_vps_openai_chat_stream_continues_when_model_only_promises_after_tool_result(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {
                    "role": "user",
                    "content": "Aqui temos a pasta, voce tem as orientacoes certo? trabalhe",
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_read",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_read",
                            "content": "# Projeto\nUse scripts/importar.txt e scripts/exportar.txt.",
                        }
                    ],
                },
            ],
            "tools": [
                {"name": "Read", "input_schema": {"type": "object"}},
                {"name": "LS", "input_schema": {"type": "object"}},
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
        }

        async def chunks():
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": (
                                        "Entendido! Vou seguir as instruções fornecidas e usar as ferramentas "
                                        "disponíveis para auxiliar o usuário da melhor maneira possível."
                                    )
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            ).encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks(), payload=payload))))

        self.assertIn(b'"content_block": {"type": "tool_use"', body)
        self.assertIn(b'"name": "LS"', body)
        self.assertNotIn("Entendido!".encode(), body)
        self.assertIn(b'"stop_reason": "tool_use"', body)

        async def english_chunks():
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": (
                                        "Understood! I'll follow the instructions carefully and ensure that "
                                        "I use the latest tool result exactly once. Let's get started!"
                                    )
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            ).encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(
            asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(english_chunks(), payload=payload)))
        )

        self.assertIn(b'"content_block": {"type": "tool_use"', body)
        self.assertIn(b'"name": "LS"', body)
        self.assertNotIn(b"Understood!", body)
        self.assertIn(b'"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_stream_converts_post_error_textual_worktree_to_valid_tool_call(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {"role": "user", "content": "crie uma calculadora em python"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_write_failed",
                            "name": "Write",
                            "input": {"file_path": "calculadora.py", "content": "print('ok')"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_write_failed",
                            "content": (
                                "Error writing file\n"
                                "This background session hasn't isolated its changes yet. "
                                "Call EnterWorktree first."
                            ),
                        }
                    ],
                },
            ],
            "tools": [
                {"name": "EnterWorktree", "input_schema": {"type": "object"}},
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
        }

        async def chunks():
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": (
                                        "Entendi. Vou usar o EnterWorktre.\n"
                                        "{\n"
                                        '  "name": "EnterWorktre",\n'
                                        '  "arguments": {}\n'
                                        "}"
                                    )
                                }
                            }
                        ]
                    }
                )
                + "\n\n"
            ).encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(
            asyncio.run(
                _collect_async_bytes(
                    client._openai_sse_to_anthropic(
                        chunks(),
                        require_tool_call=False,
                        payload=payload,
                    )
                )
            )
        )

        self.assertIn(b'"content_block": {"type": "tool_use"', body)
        self.assertIn(b'"name": "EnterWorktree"', body)
        self.assertIn(b'\\"name\\": \\"calculadora-worktree\\"', body)
        self.assertNotIn(b"Entendi. Vou usar", body)
        self.assertIn(b'"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_stream_converts_tool_calls_to_anthropic_sse(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_abc",'
                b'"type":"function","function":{"name":"read_file","arguments":"{\\\\\\"path\\\\\\":"}}]}}]}\n\n'
            )
            yield (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"arguments":"\\\\\\"README.md\\\\\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'
            )
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks()))))

        self.assertIn(b'"content_block": {"type": "tool_use", "id": "call_abc", "name": "read_file", "input": {}}', body)
        self.assertIn(b'"delta": {"type": "input_json_delta", "partial_json": "{\\"path\\": \\"README.md\\"}"}', body)
        self.assertIn(b'"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_stream_normalizes_claude_code_tool_aliases(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_write",'
                b'"type":"function","function":{"name":"Write","arguments":"{\\\\\\"path\\\\\\":"}}]}}]}\n\n'
            )
            yield (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                b'"function":{"arguments":"\\\\\\"site-neymar/index.html\\\\\\",\\\\\\"content\\\\\\":\\\\\\"<html></html>\\\\\\"}"}}]},'
                b'"finish_reason":"tool_calls"}]}\n\n'
            )
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks()))))

        self.assertIn(b'"content_block": {"type": "tool_use", "id": "call_write", "name": "Write", "input": {}}', body)
        self.assertIn(
            b'"partial_json": "{\\"content\\": \\"<html></html>\\", \\"file_path\\": \\"site-neymar/index.html\\"}"',
            body,
        )
        self.assertNotIn(b'\\"path\\"', body)

    def test_vps_openai_chat_stream_does_not_emit_tool_use_without_tool_block(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)

        async def chunks():
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks()))))

        self.assertIn(b"chamada de ferramenta valida", body)
        self.assertIn(b'"stop_reason": "end_turn"', body)
        self.assertNotIn(b'"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_stream_summarizes_tool_result_when_empty_after_reading(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {"role": "user", "content": "leia os arquivos e diga o que falta"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "README.md"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": "README.md: falta configurar deploy"}],
                },
            ],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        }

        async def chunks():
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks(), payload=payload))))

        self.assertIn(b"README.md", body)
        self.assertIn(b"deploy", body)
        self.assertNotIn(b"chamada de ferramenta valida", body)
        self.assertIn(b'"stop_reason": "end_turn"', body)
        self.assertNotIn(b'"stop_reason": "tool_use"', body)

    def test_vps_openai_chat_stream_blocks_false_done_without_mutation(self) -> None:
        settings = make_settings()
        settings.vps_model_id = "qwen3-32b"
        settings.vps_model_api_format = "openai-chat"
        client = VPSAnthropicClient(settings)
        payload = {
            "__gateway_client": "claude-code",
            "messages": [
                {"role": "user", "content": "crie uma calculadora em python"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "README.md"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": "# Projeto"}],
                },
            ],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        }

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"Criei os arquivos e esta tudo pronto."}}]}\n\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        body = b"".join(asyncio.run(_collect_async_bytes(client._openai_sse_to_anthropic(chunks(), payload=payload))))

        self.assertIn(b"nao apliquei nenhuma mudanca fisica", body)
        self.assertNotIn(b"tudo pronto", body)
        self.assertIn(b'"stop_reason": "end_turn"', body)

    def test_exact_greeting_returns_local_answer_without_upstream(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "oi"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"][0]["text"], "Oi! Estou aqui. O que vamos resolver?")
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_streaming_exact_greeting_returns_local_answer_without_upstream(self) -> None:
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

        self.assertIn(b"Oi! Estou aqui", body)
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_common_president_question_returns_local_answer_without_upstream(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "quem é o presidente do brasil"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Luiz Inácio Lula da Silva", response.json()["content"][0]["text"])
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_generic_portuguese_president_question_returns_local_answer_without_upstream(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "quem e o presidente"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Luiz Inácio Lula da Silva", response.json()["content"][0]["text"])
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_vps_is_primary_and_openrouter_is_emergency_fallback(self) -> None:
        settings = make_settings()
        settings.openrouter_emergency_fallback = True
        app = create_app(
            settings=settings,
            client_factory=FakeFailingVPSClient,
            openrouter_fallback_factory=FakeOpenRouterClient,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Diga oi"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.state.model_client.fallback_uses, 1)
        self.assertEqual(len(app.state.model_client.primary.calls), 1)
        self.assertEqual(len(app.state.model_client.fallback.calls), 1)

    def test_slow_vps_uses_openrouter_emergency_fallback_before_first_response(self) -> None:
        settings = make_settings()
        settings.openrouter_emergency_fallback = True
        settings.vps_model_slow_fallback_seconds = 0.01
        app = create_app(
            settings=settings,
            client_factory=FakeSlowVPSClient,
            openrouter_fallback_factory=FakeOpenRouterClient,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Diga oi"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.state.model_client.fallback_uses, 1)
        self.assertEqual(len(app.state.model_client.fallback.calls), 1)

    def test_simple_requests_are_capped_to_fast_output_budget(self) -> None:
        settings = make_settings()
        settings.simple_request_max_output_tokens = 256
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 16000,
                "messages": [{"role": "user", "content": "Diga quem é o presidente do Brasil"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        sent_payload = app.state.openrouter.calls[-1][1]
        self.assertEqual(sent_payload["max_tokens"], 256)

    def test_vps_error_without_fallback_returns_error(self) -> None:
        settings = make_settings()
        settings.openrouter_emergency_fallback = False
        app = create_app(
            settings=settings,
            client_factory=FakeFailingVPSClient,
            openrouter_fallback_factory=FakeOpenRouterClient,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Diga oi"}],
            },
        )

        self.assertEqual(response.status_code, 502)
        self.assertIsNone(app.state.model_client.fallback)

    def test_vps_error_without_openrouter_key_does_not_try_fallback(self) -> None:
        settings = make_settings()
        settings.openrouter_api_key = ""
        app = create_app(
            settings=settings,
            client_factory=FakeFailingVPSClient,
            openrouter_fallback_factory=FakeOpenRouterClient,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Diga oi"}],
            },
        )

        self.assertEqual(response.status_code, 502)
        self.assertIsNone(app.state.model_client.fallback)

    def test_streaming_vps_failure_before_first_chunk_uses_openrouter_fallback(self) -> None:
        settings = make_settings()
        settings.openrouter_emergency_fallback = True
        app = create_app(
            settings=settings,
            client_factory=FakeFailingVPSClient,
            openrouter_fallback_factory=FakeOpenRouterClient,
        )
        client = TestClient(app)

        with client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Diga oi"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            body = b"".join(response.iter_bytes())

        self.assertIn(b"event: message_start", body)
        self.assertEqual(app.state.model_client.fallback_uses, 1)
        self.assertEqual(len(app.state.model_client.fallback.calls), 1)

    def test_models_allows_public_discovery_without_generation_auth(self) -> None:
        response = self.client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "list")

        invalid_response = self.client.get(
            "/v1/models",
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(invalid_response.status_code, 200)

        response = self.client.get("/v1/models", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        model_ids = {model["id"] for model in response.json()["data"]}
        self.assertEqual(model_ids, {"claude-code-pro", "claude-sonnet-4.6"})
        self.assertEqual({model["object"] for model in response.json()["data"]}, {"model"})
        self.assertNotIn("cost_target", response.json()["data"][0])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertNotIn("localhost", response.headers["content-security-policy"])
        self.assertNotIn("127.0.0.1", response.headers["content-security-policy"])

    def test_model_retrieve_accepts_ansi_suffix_from_desktop_client(self) -> None:
        response = self.client.get("/v1/models/claude-code-pro%5B1m%5D", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "claude-code-pro")

    def test_router_normalizes_ansi_suffix_from_selected_model(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-pro[1m]",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "meu nome e allan"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requested_model"], "claude-code-pro")
        self.assertEqual(response.json()["public_model"], "claude-code-pro")

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
        admin_data = admin_response.json()
        self.assertEqual(admin_data["public_model"], "Claude Sonnet 4.5")
        self.assertTrue(admin_data["model_backend_configured"])
        self.assertFalse(admin_data["external_fallback_configured"])
        self.assertNotIn("vps_model_id", admin_data)
        self.assertNotIn("vps_model_base_url", admin_data)
        self.assertIn("cost_target", admin_data)

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
        serialized = json.dumps(data).lower()
        for forbidden in ("qwen", "deepseek", "tencent", "openrouter", "selected_openrouter_model", "agents"):
            self.assertNotIn(forbidden, serialized)

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

    def test_public_stream_hides_qwen_thinking_tags(self) -> None:
        payloads = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "<think>"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "\n\nanalisando"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "</think>\n\nResposta final."}},
        ]

        text = asyncio.run(collect_stream_text(stream_events(payloads)))

        self.assertEqual(text, "Resposta final.")

    def test_public_stream_drops_thinking_blocks_and_remaps_visible_indices(self) -> None:
        async def chunks():
            yield (
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"model":"qwen3","content":[]}}\n\n'
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,'
                b'"content_block":{"type":"thinking","thinking":""}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"thinking_delta","thinking":"analisando"}}\n\n'
                b'event: content_block_stop\n'
                b'data: {"type":"content_block_stop","index":0}\n\n'
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":1,'
                b'"content_block":{"type":"text","text":""}}\n\n'
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":1,'
                b'"delta":{"type":"text_delta","text":"Resposta final."}}\n\n'
            )

        body = b"".join(asyncio.run(_collect_async_bytes(_public_model_stream(chunks(), "claude-code-pro"))))

        self.assertNotIn(b"thinking", body)
        self.assertIn(b'"model": "claude-code-pro"', body)
        self.assertIn(b'"index": 0', body)
        self.assertIn(b"Resposta final.", body)

    def test_public_stream_preserves_spaces_when_words_share_one_letter(self) -> None:
        payloads = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Deixe"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " esfriar antes"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " de servir com toque"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " extra ou confeiteiro"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " ou calda."}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " Faça uma"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " massa simples."}},
        ]

        text = asyncio.run(collect_stream_text(stream_events(payloads)))

        self.assertEqual(
            text,
            "Deixe esfriar antes de servir com toque extra ou confeiteiro ou calda. Faça uma massa simples.",
        )

    def test_public_stream_does_not_overlap_short_word_boundaries(self) -> None:
        payloads = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Nao foi possivel encontrar"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "arquivos na pasta."}},
        ]

        text = asyncio.run(collect_stream_text(stream_events(payloads)))

        self.assertEqual(text, "Nao foi possivel encontrar arquivos na pasta.")

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

    def test_clean_model_text_removes_qwen_thinking_blocks(self) -> None:
        broken = "<think>\n\nvou pensar escondido\n</think>\n\nOi, Allan."

        self.assertEqual(clean_model_text(broken), "Oi, Allan.")

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

    def test_clean_model_text_repairs_common_recipe_portuguese(self) -> None:
        broken = "Unte uma forma redonda comanteiga. Adicione os ovos, o leite o óleo. Transfira massa para forma e deixe esfriar por completo antes desenformar."

        self.assertEqual(
            clean_model_text(broken),
            "Unte uma forma redonda com manteiga. Adicione os ovos, o leite e o óleo. Transfira a massa para a forma e deixe esfriar por completo antes de desenformar.",
        )

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

    def test_admin_can_create_one_day_api_token_with_twenty_percent_profit(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={
                    "name": "Fornecedor Teste",
                    "price": 50,
                    "durationHours": 24,
                },
            )

            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]
            self.assertTrue(account["apiToken"].startswith("sk-"))
            self.assertTrue(account["apiOnly"])
            self.assertTrue(account["active"])
            self.assertFalse(account["publicTrialActive"])
            self.assertEqual(account["giftCardCode"], "__api_only__")
            self.assertEqual(account["price"], 50)
            self.assertEqual(account["modelKey"], "opus")
            self.assertEqual(account["dailyLimit"], 66_875_648)
            self.assertAlmostEqual(account["maxCostUsd"], 40 / 5.5)

            usage = client.get(
                "/v1/usage",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
            )
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()["today"]["daily_cost_budget_usd"], round(40 / 5.5, 8))

            with app.state.account_store._connect() as db:
                db.execute(
                    "UPDATE accounts SET trial_expires_at = ? WHERE id = ?",
                    ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), account["id"]),
                )
                db.commit()

            accounts = client.get("/v1/admin/accounts", headers=self.headers)
            self.assertEqual(accounts.status_code, 200)
            expired = next(item for item in accounts.json()["data"] if item["id"] == account["id"])
            self.assertFalse(expired["active"])

    def test_admin_can_create_multiple_api_tokens_with_same_manual_limit(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={
                    "name": "Equipe API",
                    "price": 50,
                    "durationHours": 24,
                    "quantity": 3,
                    "manualLimit": 123456,
                },
            )

            self.assertEqual(created.status_code, 200)
            accounts = created.json()["accounts"]
            self.assertEqual(len(accounts), 3)
            self.assertEqual({account["dailyLimit"] for account in accounts}, {123456})
            self.assertEqual(len({account["apiToken"] for account in accounts}), 3)

    def test_admin_can_attach_existing_api_token_to_web_account_limits(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)
            custom_token = "sk-existing-provider-token"

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={
                    "name": "API Cliente",
                    "price": 50,
                    "durationHours": 24,
                    "manualLimit": 987654,
                    "apiToken": custom_token,
                },
            )

            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]
            self.assertEqual(account["apiToken"], custom_token)
            self.assertEqual(account["dailyLimit"], 987654)

            usage = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {custom_token}"})
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()["account"]["dailyLimit"], 987654)

            duplicate = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Duplicado", "price": 50, "apiToken": custom_token},
            )
            self.assertEqual(duplicate.status_code, 409)

    def test_unlimited_api_token_has_no_daily_cap_but_records_usage(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Ilimitado", "price": 50, "durationHours": 24, "unlimited": True},
            )
            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]
            self.assertTrue(account["unlimited"])
            self.assertEqual(account["dailyLimit"], 0)

            response = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Diga oi"}],
                },
            )
            self.assertEqual(response.status_code, 200)
            usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {account['apiToken']}"})
            self.assertIsNone(usage.json()["today"]["remaining_tokens"])

    def test_admin_can_create_vps_12h_cycle_schedule(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)
            start_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

            created = client.post(
                "/v1/admin/vps/schedules",
                headers=self.headers,
                json={"name": "Ciclo", "startAt": start_at, "days": 4, "onHours": 12, "offHours": 12},
            )

            self.assertEqual(created.status_code, 200)
            schedule = created.json()["schedule"]
            self.assertEqual(schedule["onHours"], 12)
            self.assertEqual(schedule["offHours"], 12)
            listed = client.get("/v1/admin/vps/schedules", headers=self.headers)
            self.assertEqual(listed.status_code, 200)
            self.assertFalse(listed.json()["status"]["configured"])
            self.assertEqual(len(listed.json()["data"]), 1)

    def test_admin_can_start_vps_for_12_hours(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.runpod_api_key = "runpod-token"
            settings.runpod_pod_id = "pod-123"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)
            actions: list[str] = []

            async def fake_runpod(action: str) -> None:
                actions.append(action)

            app.state.vps_schedules._runpod = fake_runpod

            response = client.post(
                "/v1/admin/vps/actions",
                headers=self.headers,
                json={"action": "start"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(actions, ["start"])
            self.assertEqual(response.json()["status"]["action"], "start")
            self.assertEqual(response.json()["status"]["desiredState"], "on")
            self.assertTrue(response.json()["status"]["runpodApiConfigured"])
            self.assertIn("12h", response.json()["status"]["message"])
            self.assertEqual(response.json()["status"]["schedule"]["id"], "manual_12h")
            self.assertEqual(response.json()["status"]["schedule"]["onHours"], 12)
            self.assertTrue(response.json()["status"]["nextTransitionAt"])

    def test_admin_vps_manual_action_returns_controlled_error(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.runpod_api_key = "runpod-token"
            settings.runpod_pod_id = "pod-123"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            async def fake_runpod(action: str) -> None:
                raise RuntimeError("RunPod start failed with HTTP 401: unauthorized")

            app.state.vps_schedules._runpod = fake_runpod

            response = client.post(
                "/v1/admin/vps/actions",
                headers=self.headers,
                json={"action": "start"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"]["status"], "error")
            self.assertIn("HTTP 401", response.json()["status"]["error"])

    def test_prompt_command_only_returns_confirmation_without_model_call(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-auto",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "/modelo opus\n/raciocinio extra forte"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.6")
        self.assertIn("Configuração aplicada", response.json()["content"][0]["text"])
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_prompt_commands_switch_model_and_reasoning_for_current_request(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-auto",
                "max_tokens": 128,
                "gateway_web_search": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": "/modelo sonnet\n/raciocinio rapido\nPesquise notícias atuais de IA",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["requested_model"], "claude-code-pro")
        self.assertEqual(data["public_model"], "claude-code-pro")
        self.assertEqual(data["web_search_policy"], "auto")
        self.assertTrue(data["web_search_should_search"])

    def test_account_prompt_commands_are_saved_as_api_preferences(self) -> None:
        with TemporaryDirectory() as directory:
            settings = make_settings()
            settings.account_data_file = f"{directory}/gateway.sqlite3"
            settings.quota_data_file = f"{directory}/gateway.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 24},
            )
            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]
            token_headers = {"Authorization": f"Bearer {account['apiToken']}"}

            first = client.post(
                "/v1/router/debug",
                headers=token_headers,
                json={
                    "model": "claude-code-economy",
                    "max_tokens": 128,
                    "messages": [
                        {
                            "role": "user",
                            "content": "/modelo sonnet\n/raciocinio forte\nExplique essa função",
                        }
                    ],
                },
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["requested_model"], "claude-code-pro")

            accounts = client.get("/v1/admin/accounts", headers=self.headers).json()["data"]
            stored = next(item for item in accounts if item["id"] == account["id"])
            self.assertEqual(stored["preferredModel"], "claude-code-pro")
            self.assertEqual(stored["preferredReasoning"], "strong")

            second = client.post(
                "/v1/router/debug",
                headers=token_headers,
                json={
                    "model": "claude-code-economy",
                    "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Explique de novo"}],
                },
            )
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["requested_model"], "claude-code-pro")

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
            self.assertEqual(account["dailyLimit"], 1600)

            message = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-ultra",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Diga oi"}],
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
            self.assertLess(usage.json()["account"]["usedToday"], 100)
            self.assertEqual(usage.json()["account"]["usageDay"], _today())

            with app.state.account_store._connect() as db:
                db.execute(
                    "UPDATE accounts SET used_today = 1599, usage_day = ? WHERE id = ?",
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
                    "UPDATE accounts SET used_today = 1600, usage_day = '2000-01-01' WHERE id = ?",
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
            settings.public_trial_daily_limit = 1200000
            settings.public_trial_label = "Teste grátis 24h"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            plans = client.get("/v1/plans").json()
            self.assertTrue(plans["public_trial"]["active"])
            self.assertEqual(plans["public_trial"]["dailyLimit"], 1200000)

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
            self.assertEqual(account["dailyLimit"], 1200000)
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
            self.assertEqual(debug.json()["public_model"], "claude-code-pro")

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
            self.assertEqual(free["dailyLimit"], 1600)

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
            self.assertEqual(promoted["dailyLimit"], 1200000)
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
            self.assertEqual(account["dailyLimit"], 1600)
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
            self.assertEqual(migrated["dailyLimit"], 1600)

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

    def test_mercado_pago_webhook_rejects_invalid_signature_when_secret_configured(self) -> None:
        settings = make_settings()
        settings.mercado_pago_access_token = "mp-test-token"
        settings.mercado_pago_webhook_secret = "mp-webhook-secret"
        settings.mercado_pago_webhook_tolerance_seconds = 600
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        ts = str(int(time.time() * 1000))

        response = client.post(
            "/v1/billing/mercadopago/webhook?data.id=123456&type=payment",
            headers={
                "X-Request-Id": "req-test",
                "X-Signature": f"ts={ts},v1=bad-signature",
            },
            json={"type": "payment", "data": {"id": "123456"}},
        )

        self.assertEqual(response.status_code, 403)

    def test_mercado_pago_webhook_accepts_valid_signature_when_secret_configured(self) -> None:
        settings = make_settings()
        settings.mercado_pago_access_token = "mp-test-token"
        settings.mercado_pago_webhook_secret = "mp-webhook-secret"
        settings.mercado_pago_webhook_tolerance_seconds = 600
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)
        ts = str(int(time.time() * 1000))
        request_id = "req-test"
        manifest = f"request-id:{request_id};ts:{ts};"
        signature = hmac.new(
            settings.mercado_pago_webhook_secret.encode(),
            manifest.encode(),
            hashlib.sha256,
        ).hexdigest()

        response = client.post(
            "/v1/billing/mercadopago/webhook?type=payment",
            headers={
                "X-Request-Id": request_id,
                "X-Signature": f"ts={ts},v1={signature}",
            },
            json={"type": "payment"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

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

    def test_cors_allows_configured_production_origin(self) -> None:
        settings = make_settings()
        settings.cors_allowed_origins = ("https://your-subdomain.squareweb.app",)
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.options(
            "/v1/messages",
            headers={
                "Origin": "https://your-subdomain.squareweb.app",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"],
            "https://your-subdomain.squareweb.app",
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
        self.assertEqual(response.status_code, 200)

    def test_auto_defaults_frontend_to_fast_route(self) -> None:
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
        self.assertEqual(data["mode"], "economy")
        self.assertEqual(data["model_label"], "Claude Sonnet 4.5")
        self.assertNotIn("selected_openrouter_model", data)
        self.assertNotIn("agents", data)
        self.assertTrue(data["cost_estimate"]["effective_path"]["within_budget"])
        self.assertLessEqual(
            data["cost_estimate"]["effective_path"]["cost_ratio_vs_claude"],
            0.5,
        )
        serialized = json.dumps(data).lower()
        for forbidden in ("qwen", "deepseek", "tencent", "openrouter", "selected_openrouter_model", "agents"):
            self.assertNotIn(forbidden, serialized)

    def test_auto_routes_short_simple_messages_to_economy(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-auto",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Oi"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "economy")
        self.assertEqual(data["task_type"], "explanation")
        self.assertEqual(data["complexity"], "low")
        self.assertEqual(data["model_label"], "Claude Sonnet 4.5")
        self.assertFalse(data["use_orchestration"])

    def test_default_reasoning_mode_is_fast_without_hidden_thinking_for_simple_requests(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Explique uma função simples"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertEqual(payload["__gateway_reasoning_mode"], "fast")
        self.assertEqual(payload["__gateway_reasoning"], "none")

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
        self.assertEqual(data["model_label"], "Claude Sonnet 4.5")

    def test_integral_project_analysis_avoids_expensive_thinking_defaults(self) -> None:
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
        self.assertNotIn("selected_openrouter_model", data)
        self.assertNotIn("agents", data)
        self.assertLessEqual(data["cost_estimate"]["effective_path"]["cost_ratio_vs_claude"], 0.2)

    def test_critical_ultra_reasoning_avoids_r1_by_default(self) -> None:
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
        self.assertEqual(data["model_label"], "Claude Sonnet 4.6")
        self.assertNotIn("agents", data)

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
        self.assertEqual(response.json()["content"][0]["text"], "model=deepseek/deepseek-v4-flash")
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        self.assertNotIn("Internal Gemini coding guidance", str(self.app.state.openrouter.calls[-1][1]))

    def test_auto_defaults_terminal_file_edits_to_fast_route(self) -> None:
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
        self.assertEqual(data["mode"], "economy")
        self.assertEqual(data["task_type"], "file_edit")
        self.assertEqual(data["model_label"], "Claude Sonnet 4.5")
        self.assertNotIn("selected_openrouter_model", data)
        self.assertFalse(data["use_orchestration"])

    def test_non_streaming_pro_uses_single_fast_call_by_default(self) -> None:
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
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.5")
        self.assertEqual(len(self.app.state.openrouter.calls), 1)

    def test_ultra_model_uses_stronger_thinking_by_default(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Explique uma funcao"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.6")
        final_payload = self.app.state.openrouter.calls[-1][1]
        self.assertEqual(final_payload["__gateway_reasoning"], "high")

    def test_explicit_extra_strong_admin_request_can_use_agent_pipeline(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "gateway_reasoning_mode": "xstrong",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Corrija esse bug difficult critical de auth"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.6")
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
        self.assertEqual(payload["max_tokens"], 768)
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
                "gateway_reasoning_mode": "xstrong",
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
                "gateway_reasoning_mode": "xstrong",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Corrija esse bug crítico de auth"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.6")
        called_models = [model for model, _payload in self.app.state.openrouter.calls]
        self.assertGreaterEqual(len(called_models), 6)
        self.assertNotIn("anthropic/claude-sonnet-4.6", called_models)
        self.assertNotIn("anthropic/claude-sonnet-4.6", called_models)

    def test_budget_endpoint_reports_default_models_under_target(self) -> None:
        response = self.client.get("/v1/budget", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["max_cost_ratio_vs_claude"], 0.5)
        self.assertFalse(data["allow_premium_fallback"])
        self.assertIn("web_search", data)
        self.assertEqual(data["web_search"]["model"], "gpt-5.5")
        for model in data["model_roles"].values():
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

    def test_fast_reasoning_keeps_auto_web_search_available_when_fresh_info_is_needed(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "claude-code-auto",
                "max_tokens": 128,
                "gateway_reasoning_mode": "fast",
                "messages": [{"role": "user", "content": "Pesquise notícias atuais de IA"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["public_model"], "claude-code-pro")
        self.assertEqual(data["web_search_policy"], "auto")
        self.assertTrue(data["web_search_should_search"])
        self.assertFalse(data["use_orchestration"])

    def test_web_search_context_is_injected_when_required(self) -> None:
        settings = make_settings()
        settings.enable_web_search = True
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

    def test_auto_web_search_runs_only_for_fresh_information_requests(self) -> None:
        settings = make_settings()
        settings.enable_web_search = True
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
                "messages": [{"role": "user", "content": "Pesquise notícias atuais de IA"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.web_search.calls), 1)
        self.assertIn("Internal web research context", app.state.openrouter.calls[-1][1]["system"])

    def test_web_search_does_not_run_for_stable_or_off_requests(self) -> None:
        settings = make_settings()
        settings.enable_web_search = True
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
        settings = make_settings()
        settings.openrouter_api_key = ""
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        response = client.post(
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
        payload = app.state.openrouter.calls[-1][1]
        self.assertIn("web search was needed", payload["system"])

    def test_web_search_can_use_openrouter_when_openai_key_is_missing(self) -> None:
        settings = make_settings()
        settings.enable_web_search = True
        settings.openai_api_key = ""
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
                "messages": [{"role": "user", "content": "Pesquise notícias atuais de IA"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(app.state.web_search.calls), 1)
        payload = app.state.openrouter.calls[-1][1]
        self.assertIn("Internal web research context", payload["system"])

    def test_web_search_timeout_falls_back_to_model_quickly(self) -> None:
        settings = make_settings()
        settings.enable_web_search = True
        settings.openai_api_key = "test-openai-token"
        settings.web_search_timeout_seconds = 0.01
        app = create_app(
            settings=settings,
            client_factory=FakeOpenRouterClient,
            web_search_factory=FakeHangingWebSearchClient,
        )
        client = TestClient(app)

        started = time.perf_counter()
        response = client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "gateway_web_search": "required",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "Quem é o presidente do Brasil hoje?"}],
            },
        )
        elapsed = time.perf_counter() - started

        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(len(app.state.web_search.calls), 1)
        payload = app.state.openrouter.calls[-1][1]
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

    def test_openrouter_web_search_response_extracts_citations_and_sources(self) -> None:
        result = parse_openrouter_web_search_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Resumo via OpenRouter.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "title": "Fonte OpenRouter",
                                        "url": "https://example.com/openrouter",
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"server_tool_use": {"web_search_requests": 1}},
            }
        )

        self.assertTrue(result.searched)
        self.assertEqual(result.summary, "Resumo via OpenRouter.")
        self.assertEqual(result.sources[0].title, "Fonte OpenRouter")
        self.assertEqual(result.sources[0].url, "https://example.com/openrouter")

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
        self.assertNotIn("selected_openrouter_model", data)
        self.assertTrue(data["cost_estimate"]["effective_path"]["within_budget"])

    def test_external_model_request_is_budget_routed_by_default(self) -> None:
        response = self.client.post(
            "/v1/router/debug",
            headers=self.headers,
            json={
                "model": "anthropic/claude-sonnet-4.6",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Implemente uma API"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotEqual(data["mode"], "direct")
        self.assertNotIn("selected_openrouter_model", data)
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
                "model": "anthropic/claude-sonnet-4.6",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "Implemente uma API"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["mode"], "direct")
        self.assertNotIn("selected_openrouter_model", data)
        self.assertEqual(data["model_label"], "Claude Sonnet 4.6")

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
                    "model": "claude-sonnet-4.6",
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
            self.assertEqual(response.json()["requested_model"], "claude-code-pro")
            self.assertEqual(response.json()["public_model"], "claude-code-pro")

    def test_customer_ultra_xstrong_avoids_expensive_models_by_default(self) -> None:
        expensive_models = {
            "deepseek/deepseek-r1",
            "moonshotai/kimi-k2.6",
            "qwen/qwen3-235b-a22b-thinking-2507",
        }
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
                    "model": "claude-code-ultra",
                    "gateway_reasoning_mode": "xstrong",
                    "max_tokens": 128,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Analise um bug critical de auth em production "
                                "e corrija a arquitetura do projeto"
                            ),
                        }
                    ],
                },
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            serialized = json.dumps(data).lower()
            for model in expensive_models:
                self.assertNotIn(model, serialized)
            self.assertFalse(data["use_orchestration"])

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
            self.assertEqual(response.json()["requested_model"], "claude-code-pro")
            self.assertEqual(response.json()["public_model"], "claude-code-pro")

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

    def test_tool_contract_reservation_does_not_apply_reasoning_multiplier(self) -> None:
        settings = make_settings()
        planner = create_app(settings=settings, client_factory=FakeOpenRouterClient).state.planner
        base_payload = {
            "model": "claude-code-pro",
            "max_tokens": 16000,
            "__gateway_reasoning_mode": "normal",
            "messages": [{"role": "user", "content": "apague o squarecloud.app do meu projeto"}],
        }
        tool_payload = {
            **base_payload,
            "tools": [{"name": "delete_file", "input_schema": {"type": "object"}}],
        }
        base_decision = planner.plan(base_payload)
        tool_decision = planner.plan(tool_payload)

        base_reserved = estimate_reserved_tokens(base_payload, settings, base_decision)
        tool_reserved = estimate_reserved_tokens(tool_payload, settings, tool_decision)

        self.assertEqual(base_reserved, tool_reserved * 8)
        self.assertLess(tool_reserved, 20000)

    def test_ultra_model_reservation_costs_one_point_five_x_tokens(self) -> None:
        settings = make_settings()
        planner = create_app(settings=settings, client_factory=FakeOpenRouterClient).state.planner
        base_payload = {
            "model": "claude-code-pro",
            "max_tokens": 512,
            "__gateway_reasoning_mode": "normal",
            "messages": [{"role": "user", "content": "Explique uma funcao"}],
        }
        ultra_payload = {**base_payload, "model": "claude-code-ultra"}
        base_reserved = estimate_reserved_tokens(base_payload, settings, planner.plan(base_payload))
        ultra_reserved = estimate_reserved_tokens(ultra_payload, settings, planner.plan(ultra_payload))

        self.assertEqual(ultra_reserved, math.ceil(base_reserved * 1.5))

    def test_account_usage_is_settled_to_actual_response_usage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)
            account = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 24, "model": "opus"},
            ).json()["account"]

            response = client.post(
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-pro",
                    "max_tokens": 16000,
                    "messages": [{"role": "user", "content": "apague o squarecloud.app do meu projeto"}],
                },
            )

            self.assertEqual(response.status_code, 200)
            usage = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {account['apiToken']}"})
            self.assertGreater(usage.json()["account"]["usedToday"], 0)
            self.assertLess(usage.json()["account"]["usedToday"], 100)

    def test_streaming_account_usage_is_settled_to_actual_response_usage(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeUsageStreamingOpenRouterClient)
            client = TestClient(app)
            account = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 24, "model": "opus"},
            ).json()["account"]

            with client.stream(
                "POST",
                "/v1/messages",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-pro",
                    "stream": True,
                    "max_tokens": 16000,
                    "tools": [{"name": "delete_file", "input_schema": {"type": "object"}}],
                    "messages": [{"role": "user", "content": "apague o squarecloud.app do meu projeto"}],
                },
            ) as response:
                self.assertEqual(response.status_code, 200)
                body = b"".join(response.iter_bytes())

            self.assertIn(b"message_delta", body)
            usage = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {account['apiToken']}"})
            self.assertEqual(usage.json()["account"]["usedToday"], 8)

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

    def test_admin_can_recharge_account_with_tokens_or_brl(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 24, "model": "opus"},
            )
            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]
            original_limit = account["dailyLimit"]

            token_topup = client.patch(
                f"/v1/admin/accounts/{account['id']}",
                headers=self.headers,
                json={"addTokens": 12345},
            )
            self.assertEqual(token_topup.status_code, 200)
            token_account = token_topup.json()["account"]
            self.assertEqual(token_account["dailyLimit"], original_limit + 12345)
            self.assertEqual(token_account["manualLimit"], token_account["dailyLimit"])

            brl_topup = client.patch(
                f"/v1/admin/accounts/{account['id']}",
                headers=self.headers,
                json={"rechargeBrl": 50},
            )
            self.assertEqual(brl_topup.status_code, 200)
            brl_account = brl_topup.json()["account"]
            self.assertGreater(brl_account["dailyLimit"], token_account["dailyLimit"])
            self.assertEqual(brl_account["price"], 100)
            self.assertEqual(brl_account["manualLimit"], brl_account["dailyLimit"])

    def test_admin_can_bulk_add_50m_daily_tokens_to_all_accounts(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            first = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 24, "model": "opus"},
            ).json()["account"]
            second = client.post(
                "/v1/auth/signup",
                json={
                    "name": "Cliente App",
                    "login": "bulk@example.com",
                    "password": "secret-bulk",
                },
            ).json()["account"]

            response = client.post(
                "/v1/admin/accounts/bulk-recharge",
                headers=self.headers,
                json={"addTokens": 50_000_000},
            )
            accounts = client.get("/v1/admin/accounts", headers=self.headers).json()["data"]
            by_id = {account["id"]: account for account in accounts}

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["accounts"], 2)
            self.assertEqual(by_id[first["id"]]["dailyLimit"], first["dailyLimit"] + 50_000_000)
            self.assertEqual(by_id[second["id"]]["dailyLimit"], second["dailyLimit"] + 50_000_000)
            self.assertEqual(by_id[first["id"]]["manualLimit"], by_id[first["id"]]["dailyLimit"])
            self.assertEqual(by_id[second["id"]]["manualLimit"], by_id[second["id"]]["dailyLimit"])

    def test_api_only_token_defaults_to_28_hours_and_reports_pacing_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "model": "opus"},
            )
            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]
            self.assertIn("API avulsa 28h", account["plan"])

            usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {account['apiToken']}"})
            self.assertEqual(usage.status_code, 200)
            self.assertTrue(usage.json()["customer"]["api_only"])
            self.assertTrue(usage.json()["customer"]["expires_at"])

    def test_api_only_wildcard_downgrades_when_usage_is_ahead_of_time_pace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            created = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 50, "durationHours": 28, "model": "opus"},
            )
            self.assertEqual(created.status_code, 200)
            account = created.json()["account"]

            with app.state.account_store._connect() as db:
                db.execute(
                    "UPDATE accounts SET used_today = ? WHERE id = ?",
                    (int(account["dailyLimit"] * 0.90), account["id"]),
                )
                db.commit()

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

    def test_customer_first_10_requests_are_forced_fast_even_when_heavy_is_requested(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 500, "durationHours": 28, "model": "opus"},
            ).json()["account"]
            headers = {"Authorization": f"Bearer {account['apiToken']}"}

            locked = client.post(
                "/v1/router/debug",
                headers=headers,
                json={
                    "model": "claude-code-ultra",
                    "gateway_reasoning_mode": "xstrong",
                    "max_tokens": 128,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Corrija bug critical de auth em production com multiple files",
                        }
                    ],
                },
            )
            self.assertEqual(locked.status_code, 200)
            self.assertEqual(locked.json()["mode"], "ultra")
            self.assertFalse(locked.json()["use_orchestration"])
            self.assertEqual(locked.json()["web_search_policy"], "auto")

            with app.state.account_store._connect() as db:
                db.execute(
                    "UPDATE accounts SET requests_today = 10 WHERE id = ?",
                    (account["id"],),
                )
                db.commit()

            unlocked = client.post(
                "/v1/router/debug",
                headers=headers,
                json={
                    "model": "claude-code-ultra",
                    "gateway_reasoning_mode": "xstrong",
                    "max_tokens": 128,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Corrija bug critical de auth em production com multiple files",
                        }
                    ],
                },
            )
            self.assertEqual(unlocked.status_code, 200)
            self.assertEqual(unlocked.json()["mode"], "ultra")

    def test_customer_fast_tool_requests_stay_on_fast_route(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = make_settings()
            settings.account_data_file = f"{tmpdir}/accounts.sqlite3"
            app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
            client = TestClient(app)

            account = client.post(
                "/v1/admin/api-tokens",
                headers=self.headers,
                json={"name": "Fornecedor API", "price": 500, "durationHours": 28, "model": "opus"},
            ).json()["account"]
            response = client.post(
                "/v1/router/debug",
                headers={"Authorization": f"Bearer {account['apiToken']}"},
                json={
                    "model": "claude-code-pro",
                    "gateway_reasoning_mode": "fast",
                    "max_tokens": 256,
                    "tools": [{"name": "Write", "input_schema": {"type": "object"}}],
                    "messages": [{"role": "user", "content": "Crie um arquivo txt simples"}],
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "economy")
            self.assertFalse(response.json()["use_orchestration"])

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

    def test_openai_chat_completions_stream_uses_live_sse_proxy(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "messages": [{"role": "user", "content": "Explique a função"}],
                "max_tokens": 128,
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"chat.completion.chunk", body)
        self.assertIn(b"data: [DONE]", body)
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        self.assertTrue(self.app.state.openrouter.calls[-1][1]["stream"])

    def test_openai_responses_stream_converts_anthropic_tool_use_to_function_call_events(self) -> None:
        async def chunks():
            yield (
                b'event: message_start\ndata: {"type":"message_start","message":{"model":"qwen","role":"assistant","content":[]}}\n\n'
            )
            yield (
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_read","name":"read_file","input":{}}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"README.md\\"}"}}\n\n'
            )
            yield b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
            yield (
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":0}}\n\n'
            )
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        body = b"".join(asyncio.run(_collect_async_bytes(_anthropic_stream_to_response_sse(chunks(), "claude-code-pro"))))

        self.assertIn(b"response.output_item.added", body)
        self.assertIn(b'"type": "function_call"', body)
        self.assertIn(b'"name": "read_file"', body)
        self.assertIn(b"response.function_call_arguments.delta", body)
        self.assertIn(b'\\"path\\":\\"README.md\\"', body)
        self.assertIn(b"response.function_call_arguments.done", body)
        self.assertIn(b"response.completed", body)

    def test_openai_chat_tool_history_maps_to_anthropic_tool_use_before_result(self) -> None:
        outgoing = chat_to_anthropic(
            {
                "model": "claude-code-pro",
                "messages": [
                    {"role": "user", "content": "Qual o clima?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{\"city\":\"Recife\"}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_weather",
                        "content": "{\"temperature\":29}",
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                "tool_choice": "required",
                "parallel_tool_calls": False,
            }
        )

        self.assertEqual(outgoing["tool_choice"], {"type": "any", "disable_parallel_tool_use": True})
        self.assertEqual(outgoing["messages"][1]["role"], "assistant")
        self.assertEqual(
            outgoing["messages"][1]["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call_weather",
                    "name": "get_weather",
                    "input": {"city": "Recife"},
                }
            ],
        )
        self.assertEqual(outgoing["messages"][2]["content"][0]["tool_use_id"], "call_weather")

    def test_openai_responses_function_call_history_maps_to_anthropic_tool_use_before_result(self) -> None:
        outgoing = responses_to_anthropic(
            {
                "model": "claude-code-pro",
                "input": [
                    {"role": "user", "content": "Qual o clima?"},
                    {
                        "type": "function_call",
                        "call_id": "call_weather",
                        "name": "get_weather",
                        "arguments": "{\"city\":\"Recife\"}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_weather",
                        "output": "{\"temperature\":29}",
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
                "tool_choice": {"type": "function", "name": "get_weather"},
                "parallel_tool_calls": False,
            }
        )

        self.assertEqual(
            outgoing["tool_choice"],
            {"type": "tool", "name": "get_weather", "disable_parallel_tool_use": True},
        )
        self.assertEqual(
            outgoing["messages"][1]["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call_weather",
                    "name": "get_weather",
                    "input": {"city": "Recife"},
                }
            ],
        )
        self.assertEqual(outgoing["messages"][2]["content"][0]["tool_use_id"], "call_weather")

    def test_messages_count_tokens_endpoint_is_anthropic_compatible(self) -> None:
        response = self.client.post(
            "/v1/messages/count_tokens?beta=true",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "messages": [{"role": "user", "content": "Conte estes tokens"}],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["input_tokens"], 1)

    def test_root_head_is_supported_for_desktop_health_checks(self) -> None:
        response = self.client.head("/")
        self.assertEqual(response.status_code, 200)

    def test_unversioned_compatibility_aliases_work_for_desktop_clients(self) -> None:
        models = self.client.get("/models", headers=self.headers)
        self.assertEqual(models.status_code, 200)
        self.assertIn("claude-code-pro", {item["id"] for item in models.json()["data"]})

        response = self.client.post(
            "/messages/count_tokens",
            headers=self.headers,
            json={"model": "claude-code-pro", "messages": [{"role": "user", "content": "oi"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["input_tokens"], 1)

    def test_openrouter_style_api_v1_aliases_work(self) -> None:
        models = self.client.get("/api/v1/models", headers=self.headers)
        self.assertEqual(models.status_code, 200)
        self.assertEqual(models.json()["object"], "list")
        self.assertIn("claude-code-pro", {item["id"] for item in models.json()["data"]})

        model_count = self.client.get("/api/v1/models/count", headers=self.headers)
        self.assertEqual(model_count.status_code, 200)
        self.assertGreaterEqual(model_count.json()["data"]["count"], 1)

        token_count = self.client.post(
            "/api/v1/messages/count_tokens",
            headers=self.headers,
            json={"model": "claude-code-pro", "messages": [{"role": "user", "content": "oi"}]},
        )
        self.assertEqual(token_count.status_code, 200)
        self.assertGreaterEqual(token_count.json()["input_tokens"], 1)

        chat = self.client.post(
            "/api/v1/chat/completions",
            headers=self.headers,
            json={"model": "claude-code-pro", "messages": [{"role": "user", "content": "oi"}]},
        )
        self.assertEqual(chat.status_code, 200)
        self.assertEqual(chat.json()["object"], "chat.completion")

        messages = self.client.post(
            "/api/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "oi"}],
            },
        )
        self.assertEqual(messages.status_code, 200)
        self.assertEqual(messages.json()["type"], "message")

    def test_openrouter_account_probe_endpoints_work(self) -> None:
        key = self.client.get("/api/v1/key", headers=self.headers)
        self.assertEqual(key.status_code, 200)
        self.assertIn("data", key.json())
        self.assertEqual(key.json()["data"]["limit_reset"], "daily")
        self.assertIn("rate_limit", key.json()["data"])

        credits = self.client.get("/api/v1/credits", headers=self.headers)
        self.assertEqual(credits.status_code, 200)
        self.assertIn("total_credits", credits.json()["data"])
        self.assertIn("total_usage", credits.json()["data"])

        generation = self.client.get("/api/v1/generation?id=gen_test", headers=self.headers)
        self.assertEqual(generation.status_code, 200)
        self.assertEqual(generation.json()["data"]["id"], "gen_test")
        self.assertEqual(generation.json()["data"]["provider_name"], "Claude Gateway")

    def test_openai_chat_stream_can_include_usage_chunk(self) -> None:
        app = create_app(settings=make_settings(), client_factory=FakeUsageStreamingOpenRouterClient)
        client = TestClient(app)

        with client.stream(
            "POST",
            "/chat/completions",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "Explique"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b'"choices": []', body)
        self.assertIn(b'"usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}', body)
        self.assertTrue(body.rstrip().endswith(b"data: [DONE]"))

    def test_code_requests_use_full_upstream_timeout_by_default(self) -> None:
        settings = make_settings()
        client = VPSAnthropicClient(settings)
        try:
            timeout = client._timeout_seconds_for_payload(
                {
                    "__gateway_client": "claude-code",
                    "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
                    "messages": [{"role": "user", "content": "analise o projeto"}],
                }
            )
        finally:
            asyncio.run(client.aclose())

        self.assertEqual(timeout, settings.vps_model_timeout_seconds)

    def test_gateway_preserves_long_chat_history_before_upstream(self) -> None:
        messages = [
            {"role": "user", "content": f"pergunta {index} " + ("contexto " * 200)}
            if index % 2 == 0
            else {"role": "assistant", "content": f"resposta {index} " + ("detalhe " * 200)}
            for index in range(30)
        ]
        messages.append({"role": "user", "content": "Explique a função final"})

        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "messages": messages,
                "max_tokens": 128,
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            b"".join(response.iter_bytes())

        sent = self.app.state.openrouter.calls[-1][1]
        self.assertEqual(len(sent["messages"]), len(messages))
        self.assertNotIn("__gateway_context_trimmed", sent)
        self.assertIn("Explique a função final", str(sent["messages"][-1]["content"]))

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

    def test_economy_model_uses_auto_reasoning_for_complex_default_request(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "corrija esse bug no projeto"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"event: message_start", body)
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        self.assertEqual(self.app.state.openrouter.calls[-1][1]["__gateway_reasoning"], "medium")

    def test_economy_model_can_raise_reasoning_when_user_selects_higher_level(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "stream": True,
                "max_tokens": 128,
                "gateway_reasoning_mode": "medium",
                "messages": [{"role": "user", "content": "corrija esse bug no projeto"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"event: message_start", body)
        self.assertEqual(self.app.state.openrouter.calls[-1][1]["__gateway_reasoning"], "medium")

    def test_extra_strong_reasoning_enables_hidden_thinking_for_text_only_requests(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-economy",
                "stream": True,
                "max_tokens": 128,
                "gateway_reasoning_mode": "xstrong",
                "messages": [{"role": "user", "content": "Explique a função"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"event: message_start", body)
        self.assertEqual(self.app.state.openrouter.calls[-1][1]["__gateway_reasoning"], "high")

    def test_streaming_complex_request_uses_direct_proxy_by_default(self) -> None:
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

        self.assertIn(b"event: message_start", body)
        self.assertEqual(len(self.app.state.openrouter.calls), 1)
        self.assertTrue(self.app.state.openrouter.calls[-1][1]["stream"])
        self.assertEqual(self.app.state.openrouter.calls[-1][1]["__gateway_reasoning"], "medium")

    def test_streaming_agent_pipeline_can_be_enabled_explicitly(self) -> None:
        settings = make_settings()
        settings.enable_stream_agent_orchestration = True
        app = create_app(settings=settings, client_factory=FakeOpenRouterClient)
        client = TestClient(app)

        with client.stream(
            "POST",
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "max_tokens": 128,
                "gateway_reasoning_mode": "xstrong",
                "messages": [{"role": "user", "content": "corrija esse bug no projeto"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b"text_delta", body)
        self.assertGreaterEqual(len(app.state.openrouter.calls), 5)
        self.assertEqual(app.state.openrouter.calls[0][1]["__gateway_reasoning"], "high")

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
        self.assertIn("Claude Sonnet 4.6", payload["system"])
        self.assertIn("Keep Anthropic-compatible API behavior", payload["system"])
        self.assertIn("Respond in the same language as the user's latest message", payload["system"])
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
                "messages": [{"role": "user", "content": "explique uma funcao"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())

        self.assertIn(b'"model": "Claude Sonnet 4.5"', body)

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
        self.assertIn("Claude Sonnet 4.5", payload["system"])
        self.assertIn("Keep Anthropic-compatible API behavior", payload["system"])
        self.assertEqual(payload["max_tokens"], 256)

    def test_claude_code_tool_payload_is_detected_without_beta_header(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 256,
                "tools": [
                    {
                        "name": "Write",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["file_path", "content"],
                        },
                    }
                ],
                "messages": [{"role": "user", "content": "Crie uma nova pasta e faca um site"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertEqual(payload["__gateway_client"], "claude-code")
        self.assertEqual(payload["max_tokens"], 256)

    def test_claude_code_tool_requests_are_capped_to_latency_budget(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "max_tokens": 16000,
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "Corrija o bug e rode os testes"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = self.app.state.openrouter.calls[-1][1]
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["__gateway_reasoning"], "none")

    def test_public_identity_prompt_includes_current_date_context(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-pro",
                "stream": True,
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "explique uma funcao"}],
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
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.6")
        self.assertEqual(response.json()["content"][0]["text"], "Claude Sonnet 4.6")
        self.assertEqual(self.app.state.openrouter.calls, [])

    def test_previous_model_identity_question_does_not_override_next_request(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers=self.headers,
            json={
                "model": "claude-code-ultra",
                "max_tokens": 128,
                "messages": [
                    {"role": "user", "content": "qual modelo e voce?"},
                    {
                        "role": "assistant",
                        "content": "Eu sou o Claude Sonnet 4.6, o modo selecionado neste chat.",
                    },
                    {"role": "user", "content": "apague o squarecloud.app do meu projeto"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "Claude Sonnet 4.6")
        self.assertNotIn("modo selecionado neste chat", response.json()["content"][0]["text"])
        self.assertGreaterEqual(len(self.app.state.openrouter.calls), 1)

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

        self.assertIn(b"Claude Sonnet 4.5", body)
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
            {"messages": [], "__gateway_reasoning": "low", "thinking": {"type": "enabled"}},
            "qwen/qwen3-coder-flash",
        )

        self.assertEqual(payload["reasoning"], {"effort": "low", "exclude": True})
        self.assertFalse(payload["include_reasoning"])
        self.assertNotIn("__gateway_reasoning", payload)
        self.assertNotIn("thinking", payload)

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
