from __future__ import annotations

import json
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
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        reasoning_mode = str(outgoing.pop("__gateway_reasoning", "none"))
        for key in list(outgoing):
            if key.startswith("__gateway_"):
                outgoing.pop(key, None)

        outgoing["model"] = model
        if reasoning_mode in {"low", "medium", "high"}:
            outgoing["reasoning"] = {"effort": reasoning_mode, "exclude": True}
        else:
            outgoing["reasoning"] = {"effort": "none", "exclude": True}
        outgoing["include_reasoning"] = False
        return outgoing

    def _strip_reasoning_from_response(self, response: dict[str, Any]) -> dict[str, Any]:
        reasoning_fields = ("reasoning", "reasoning_content", "reasoning_details")
        reasoning_block_types = {"thinking", "redacted_thinking"}

        for field in reasoning_fields:
            response.pop(field, None)

        content = response.get("content")
        if isinstance(content, list):
            response["content"] = [
                block
                for block in content
                if not (
                    isinstance(block, dict)
                    and block.get("type") in reasoning_block_types
                )
            ]

        choices = response.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    for field in reasoning_fields:
                        message.pop(field, None)
                    message_content = message.get("content")
                    if isinstance(message_content, list):
                        message["content"] = [
                            block
                            for block in message_content
                            if not (
                                isinstance(block, dict)
                                and block.get("type") in reasoning_block_types
                            )
                        ]
        return response

    def _should_drop_stream_event(self, event: str, suppressed_indices: set[int]) -> bool:
        data_lines = [
            line.removeprefix("data:").strip()
            for line in event.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            return False

        data = "\n".join(data_lines)
        if not data or data == "[DONE]":
            return False

        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return False

        reasoning_fields = {"reasoning", "reasoning_content", "reasoning_details"}
        if any(field in payload for field in reasoning_fields):
            return True

        event_type = payload.get("type")
        index = payload.get("index")
        content_block = payload.get("content_block")
        if (
            event_type == "content_block_start"
            and isinstance(index, int)
            and isinstance(content_block, dict)
            and content_block.get("type") in {"thinking", "redacted_thinking"}
        ):
            suppressed_indices.add(index)
            return True

        if isinstance(index, int) and index in suppressed_indices:
            if event_type == "content_block_stop":
                suppressed_indices.discard(index)
            return True

        delta = payload.get("delta")
        if isinstance(delta, dict) and delta.get("type") in {
            "thinking_delta",
            "signature_delta",
            "reasoning_delta",
        }:
            return True

        choices = payload.get("choices")
        if isinstance(choices, list):
            visible_choice = False
            for choice in choices:
                if not isinstance(choice, dict):
                    visible_choice = True
                    continue
                choice_delta = choice.get("delta")
                if isinstance(choice_delta, dict):
                    for field in reasoning_fields:
                        choice_delta.pop(field, None)
                    if choice_delta:
                        visible_choice = True
                else:
                    visible_choice = True
            return not visible_choice

        return False

    async def _filter_reasoning_stream(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        buffer = ""
        suppressed_indices: set[int] = set()

        async for chunk in chunks:
            buffer += chunk.decode("utf-8", "replace")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                if not self._should_drop_stream_event(event, suppressed_indices):
                    yield f"{event}\n\n".encode("utf-8")

        if buffer and not self._should_drop_stream_event(buffer, suppressed_indices):
            yield buffer.encode("utf-8")

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = False

        response = await self._client.post(
            self.messages_url,
            headers=self._headers(),
            json=outgoing,
            timeout=self.settings.request_timeout_seconds,
        )

        if response.status_code >= 400:
            raise OpenRouterError(response.text, status_code=response.status_code)

        return self._strip_reasoning_from_response(response.json())

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = True

        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
        async with self._client.stream(
            "POST",
            self.messages_url,
            headers=self._headers(),
            json=outgoing,
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise OpenRouterError(body.decode("utf-8", "replace"), response.status_code)

            async for chunk in self._filter_reasoning_stream(response.aiter_bytes()):
                yield chunk
