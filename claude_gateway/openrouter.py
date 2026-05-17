from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx

from .config import Settings


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def messages_url(self) -> str:
        return f"{self.settings.openrouter_base_url.rstrip('/')}/v1/messages"

    def _headers(self) -> dict[str, str]:
        if not self.settings.openrouter_api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured.", status_code=503)

        return {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.openrouter_site_url,
            "X-Title": self.settings.openrouter_app_name,
        }

    def _payload_for_model(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        outgoing = deepcopy(payload)
        outgoing["model"] = model
        return outgoing

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = False

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(self.messages_url, headers=self._headers(), json=outgoing)

        if response.status_code >= 400:
            raise OpenRouterError(response.text, status_code=response.status_code)

        return response.json()

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = True

        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
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
