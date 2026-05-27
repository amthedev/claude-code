from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any, Protocol

import httpx

from .config import Settings
from .openrouter import OpenRouterClient, OpenRouterError


class AnthropicModelClient(Protocol):
    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        ...

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        ...


class VPSAnthropicClient:
    provider_name = "vps"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def messages_url(self) -> str:
        return f"{self.settings.vps_model_base_url.rstrip('/')}/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.vps_model_api_key:
            headers["Authorization"] = f"Bearer {self.settings.vps_model_api_key}"
        return headers

    def _payload_for_model(self, payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
        outgoing = deepcopy(payload)
        for key in list(outgoing):
            if key.startswith("__gateway_"):
                outgoing.pop(key, None)
        outgoing["model"] = self.settings.vps_model_id
        outgoing.pop("include_reasoning", None)
        outgoing.pop("reasoning", None)
        return outgoing

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = False
        timeout = httpx.Timeout(self.settings.vps_model_timeout_seconds)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.messages_url, headers=self._headers(), json=outgoing)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"VPS model request failed: {exc}", status_code=502) from exc

        if response.status_code >= 400:
            raise OpenRouterError(response.text, status_code=response.status_code)

        try:
            data = response.json()
        except ValueError as exc:
            raise OpenRouterError("VPS model returned invalid JSON.", status_code=502) from exc
        if not isinstance(data, dict):
            raise OpenRouterError("VPS model returned an invalid response object.", status_code=502)
        return data

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = True
        timeout = httpx.Timeout(
            connect=30.0,
            read=self.settings.vps_model_timeout_seconds,
            write=30.0,
            pool=30.0,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    self.messages_url,
                    headers=self._headers(),
                    json=outgoing,
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise OpenRouterError(body.decode("utf-8", "replace"), response.status_code)
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except OpenRouterError:
            raise
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"VPS model stream failed: {exc}", status_code=502) from exc


class EmergencyFallbackModelClient:
    provider_name = "vps"
    fallback_provider_name = "openrouter"

    def __init__(
        self,
        settings: Settings,
        primary: AnthropicModelClient,
        fallback: AnthropicModelClient | None,
    ) -> None:
        self.settings = settings
        self.primary = primary
        self.fallback = fallback
        self.fallback_uses = 0
        self._logger = logging.getLogger("claude_gateway.model_client")

    def _can_fallback(self) -> bool:
        return bool(
            self.settings.openrouter_emergency_fallback
            and self.settings.openrouter_api_key
            and self.fallback is not None
        )

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        if not self._can_fallback():
            return await self.primary.complete_messages(payload, model)

        try:
            return await asyncio.wait_for(
                self.primary.complete_messages(payload, model),
                timeout=self.settings.vps_model_slow_fallback_seconds,
            )
        except Exception as exc:
            return await self._fallback_complete(payload, model, exc)

    async def _fallback_complete(
        self,
        payload: dict[str, Any],
        model: str,
        exc: BaseException,
    ) -> dict[str, Any]:
        self._record_fallback("complete", exc)
        assert self.fallback is not None
        return await self.fallback.complete_messages(payload, model)

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        if not self._can_fallback():
            async for chunk in self.primary.stream_messages(payload, model):
                yield chunk
            return

        iterator = self.primary.stream_messages(payload, model)
        try:
            first_chunk = await asyncio.wait_for(
                anext(iterator),
                timeout=self.settings.vps_model_slow_fallback_seconds,
            )
        except StopAsyncIteration as exc:
            async for chunk in self._fallback_stream(payload, model, exc):
                yield chunk
            return
        except Exception as exc:
            async for chunk in self._fallback_stream(payload, model, exc):
                yield chunk
            return

        yield first_chunk
        try:
            async for chunk in iterator:
                yield chunk
        except Exception as exc:
            self._logger.warning("Primary VPS stream failed after first chunk: %s", exc)
            raise

    async def _fallback_stream(
        self,
        payload: dict[str, Any],
        model: str,
        exc: BaseException,
    ) -> AsyncIterator[bytes]:
        self._record_fallback("stream", exc)
        assert self.fallback is not None
        async for chunk in self.fallback.stream_messages(payload, model):
            yield chunk

    def _record_fallback(self, operation: str, exc: BaseException) -> None:
        self.fallback_uses += 1
        self._logger.warning(
            "Using OpenRouter emergency fallback for %s after VPS failure: %s",
            operation,
            exc,
        )


def default_model_client(
    settings: Settings,
    *,
    primary_factory: type[AnthropicModelClient] | None = None,
    fallback_factory: type[AnthropicModelClient] | None = None,
) -> AnthropicModelClient:
    primary = (primary_factory or VPSAnthropicClient)(settings)
    fallback = None
    if settings.openrouter_emergency_fallback and settings.openrouter_api_key:
        fallback = (fallback_factory or OpenRouterClient)(settings)
    return EmergencyFallbackModelClient(settings, primary, fallback)
