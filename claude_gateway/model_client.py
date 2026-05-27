from __future__ import annotations

import asyncio
import json
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
        return self._url("/v1/messages")

    @property
    def chat_completions_url(self) -> str:
        return self._url("/v1/chat/completions")

    def _api_format(self) -> str:
        value = self.settings.vps_model_api_format.strip().lower().replace("_", "-")
        if value in {"openai", "openai-chat", "chat-completions", "vllm"}:
            return "openai-chat"
        return "anthropic"

    def _url(self, path: str) -> str:
        base = self.settings.vps_model_base_url.rstrip("/")
        if base.endswith("/v1") and path.startswith("/v1/"):
            return f"{base}{path[3:]}"
        return f"{base}{path}"

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
        if self._api_format() == "openai-chat":
            return await self._complete_openai_chat(payload)

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
        if self._api_format() == "openai-chat":
            async for chunk in self._stream_openai_chat(payload):
                yield chunk
            return

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

    def _openai_chat_payload(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        outgoing: dict[str, Any] = {
            "model": self.settings.vps_model_id,
            "messages": self._messages_to_openai(payload),
            "max_tokens": int(payload.get("max_tokens") or 4096),
            "stream": stream,
        }
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "stop"):
            if key in payload:
                outgoing[key] = payload[key]
        tools = self._tools_to_openai(payload.get("tools"))
        if tools:
            outgoing["tools"] = tools
            if payload.get("tool_choice"):
                outgoing["tool_choice"] = payload["tool_choice"]
        return outgoing

    def _messages_to_openai(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system_text = self._content_to_text(payload.get("system"))
        if system_text:
            messages.append({"role": "system", "content": system_text})

        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            if role not in {"user", "assistant", "system"}:
                role = "user"
            messages.append({"role": role, "content": self._content_to_text(message.get("content"))})

        return messages or [{"role": "user", "content": ""}]

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block_type == "tool_result":
                parts.append(f"Tool result: {self._content_to_text(block.get('content'))}")
            elif block_type == "tool_use":
                parts.append(
                    "Tool use: "
                    + json.dumps(
                        {"name": block.get("name"), "input": block.get("input")},
                        ensure_ascii=True,
                    )
                )
        return "\n".join(part for part in parts if part)

    def _tools_to_openai(self, tools: Any) -> list[dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("input_schema") or {"type": "object"},
                    },
                }
            )
        return converted

    async def _complete_openai_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        outgoing = self._openai_chat_payload(payload, stream=False)
        timeout = httpx.Timeout(self.settings.vps_model_timeout_seconds)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.chat_completions_url,
                    headers=self._headers(),
                    json=outgoing,
                )
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
        return self._anthropic_from_openai_chat(data)

    def _anthropic_from_openai_chat(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        if not isinstance(message, dict):
            message = {}

        content: list[dict[str, Any]] = []
        text = message.get("content")
        if isinstance(text, str) and text:
            content.append({"type": "text", "text": text})
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                content.append(
                    {
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": self._json_object(function.get("arguments")),
                    }
                )
        if not content:
            content.append({"type": "text", "text": ""})

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return {
            "id": str(data.get("id") or "msg_vps"),
            "type": "message",
            "role": "assistant",
            "model": self.settings.vps_model_id,
            "content": content,
            "stop_reason": "tool_use" if any(block.get("type") == "tool_use" for block in content) else "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
        }

    def _json_object(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    async def _stream_openai_chat(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        outgoing = self._openai_chat_payload(payload, stream=True)
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
                    self.chat_completions_url,
                    headers=self._headers(),
                    json=outgoing,
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise OpenRouterError(body.decode("utf-8", "replace"), response.status_code)
                    async for chunk in self._openai_sse_to_anthropic(response.aiter_bytes()):
                        yield chunk
        except OpenRouterError:
            raise
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"VPS model stream failed: {exc}", status_code=502) from exc

    async def _openai_sse_to_anthropic(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        yield b'event: message_start\ndata: {"type":"message_start","message":{"model":"'
        yield self.settings.vps_model_id.encode("utf-8")
        yield b'","role":"assistant","content":[]}}\n\n'
        yield b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'

        buffer = ""
        async for chunk in chunks:
            buffer += chunk.decode("utf-8", "replace")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                text_delta = self._openai_text_delta(event)
                if text_delta:
                    yield (
                        "event: content_block_delta\n"
                        f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text_delta}})}"
                        "\n\n"
                    ).encode("utf-8")

        if buffer:
            text_delta = self._openai_text_delta(buffer)
            if text_delta:
                yield (
                    "event: content_block_delta\n"
                    f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text_delta}})}"
                    "\n\n"
                ).encode("utf-8")

        yield b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":0}}\n\n'
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    def _openai_text_delta(self, event: str) -> str:
        data_lines = [
            line.removeprefix("data:").strip()
            for line in event.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            return ""
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            return ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else {}
        if not isinstance(delta, dict):
            return ""
        content = delta.get("content")
        return content if isinstance(content, str) else ""


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
