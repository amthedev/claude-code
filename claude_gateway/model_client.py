from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .anthropic import clean_model_text, split_thinking_text
from .config import Settings
from .openrouter import OpenRouterClient, OpenRouterError


class AnthropicModelClient(Protocol):
    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        ...

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        ...


@dataclass(frozen=True, slots=True)
class VPSTarget:
    base_url: str
    model_id: str
    api_format: str
    api_key: str


class VPSAnthropicClient:
    provider_name = "vps"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def messages_url(self) -> str:
        return self._url("/v1/messages", self._default_target())

    @property
    def chat_completions_url(self) -> str:
        return self._url("/v1/chat/completions", self._default_target())

    def _default_target(self) -> VPSTarget:
        return VPSTarget(
            base_url=self.settings.vps_model_base_url,
            model_id=self.settings.vps_model_id,
            api_format=self.settings.vps_model_api_format,
            api_key=self.settings.vps_model_api_key,
        )

    def _fast_target(self) -> VPSTarget:
        return VPSTarget(
            base_url=self.settings.vps_fast_model_base_url or self.settings.vps_model_base_url,
            model_id=self.settings.vps_fast_model_id or self.settings.vps_model_id,
            api_format=self.settings.vps_fast_model_api_format or self.settings.vps_model_api_format,
            api_key=self.settings.vps_fast_model_api_key or self.settings.vps_model_api_key,
        )

    def _strong_target(self) -> VPSTarget:
        return VPSTarget(
            base_url=self.settings.vps_strong_model_base_url or self.settings.vps_model_base_url,
            model_id=self.settings.vps_strong_model_id or self.settings.vps_model_id,
            api_format=self.settings.vps_strong_model_api_format or self.settings.vps_model_api_format,
            api_key=self.settings.vps_strong_model_api_key or self.settings.vps_model_api_key,
        )

    def _target_for_model(self, model: str | None) -> VPSTarget:
        if not self.settings.vps_strong_model_id:
            return self._default_target()

        requested = str(model or "").strip().lower()
        fast_names = {
            self.settings.cheap_code_agent.lower(),
            self.settings.fast_agent.lower(),
            self.settings.vps_fast_model_id.lower(),
            self.settings.vps_model_id.lower(),
        }
        if requested in fast_names or "flash" in requested or "economy" in requested:
            return self._fast_target()
        return self._strong_target()

    def _api_format(self, target: VPSTarget | None = None) -> str:
        target = target or self._default_target()
        value = target.api_format.strip().lower().replace("_", "-")
        if value in {"openai", "openai-chat", "chat-completions", "vllm"}:
            return "openai-chat"
        return "anthropic"

    def _url(self, path: str, target: VPSTarget | None = None) -> str:
        target = target or self._default_target()
        base = target.base_url.rstrip("/")
        if base.endswith("/v1") and path.startswith("/v1/"):
            return f"{base}{path[3:]}"
        return f"{base}{path}"

    def _headers(self, target: VPSTarget | None = None) -> dict[str, str]:
        target = target or self._default_target()
        headers = {"Content-Type": "application/json"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        return headers

    def _payload_for_model(self, payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
        target = self._target_for_model(model)
        outgoing = deepcopy(payload)
        for key in list(outgoing):
            if key.startswith("__gateway_"):
                outgoing.pop(key, None)
        outgoing["model"] = target.model_id
        outgoing.pop("include_reasoning", None)
        outgoing.pop("reasoning", None)
        return outgoing

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        target = self._target_for_model(model)
        if self._api_format(target) == "openai-chat":
            return await self._complete_openai_chat(payload, model)

        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = False
        timeout = httpx.Timeout(self.settings.vps_model_timeout_seconds)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._url("/v1/messages", target),
                    headers=self._headers(target),
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
        return data

    async def stream_messages(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        target = self._target_for_model(model)
        if self._api_format(target) == "openai-chat":
            async for chunk in self._stream_openai_chat(payload, model):
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
                    self._url("/v1/messages", target),
                    headers=self._headers(target),
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

    def _openai_chat_payload(
        self,
        payload: dict[str, Any],
        *,
        stream: bool,
        model: str | None = None,
    ) -> dict[str, Any]:
        target = self._target_for_model(model)
        messages = self._messages_to_openai(payload)
        if self._should_disable_qwen_thinking(payload, target):
            messages = self._messages_with_no_think(messages)
        outgoing: dict[str, Any] = {
            "model": target.model_id,
            "messages": messages,
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
                outgoing["tool_choice"] = self._tool_choice_to_openai(payload["tool_choice"])
        return outgoing

    def _should_disable_qwen_thinking(self, payload: dict[str, Any], target: VPSTarget) -> bool:
        if str(payload.get("__gateway_reasoning") or "").strip().lower() != "none":
            return False
        model_id = target.model_id.lower()
        return "qwen3" in model_id or "qwen/qwen3" in model_id

    def _messages_with_no_think(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = deepcopy(messages)
        for message in copied:
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "")
            if not content.lstrip().startswith("/no_think"):
                message["content"] = f"/no_think\n\n{content}"
            return copied
        copied.append({"role": "user", "content": "/no_think"})
        return copied

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

    def _tool_choice_to_openai(self, tool_choice: Any) -> Any:
        if isinstance(tool_choice, str):
            return {"any": "required"}.get(tool_choice, tool_choice)
        if not isinstance(tool_choice, dict):
            return tool_choice

        choice_type = str(tool_choice.get("type") or "").strip().lower()
        if choice_type in {"auto", "none", "required"}:
            return choice_type
        if choice_type == "any":
            return "required"
        if choice_type == "tool":
            name = tool_choice.get("name")
            if name:
                return {"type": "function", "function": {"name": str(name)}}
        if choice_type == "function":
            function = tool_choice.get("function")
            if isinstance(function, dict) and function.get("name"):
                return {"type": "function", "function": {"name": str(function["name"])}}
            name = tool_choice.get("name")
            if name:
                return {"type": "function", "function": {"name": str(name)}}
        return tool_choice

    async def _complete_openai_chat(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        target = self._target_for_model(model)
        outgoing = self._openai_chat_payload(payload, stream=False, model=model)
        timeout = httpx.Timeout(self.settings.vps_model_timeout_seconds)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._url("/v1/chat/completions", target),
                    headers=self._headers(target),
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
        return self._anthropic_from_openai_chat(data, model=model)

    def _anthropic_from_openai_chat(
        self,
        data: dict[str, Any],
        model: str | None = None,
    ) -> dict[str, Any]:
        target = self._target_for_model(model)
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        if not isinstance(message, dict):
            message = {}

        content: list[dict[str, Any]] = []
        text = message.get("content")
        if isinstance(text, str) and text:
            thinking_text, visible_text = split_thinking_text(text)
            if thinking_text:
                content.append({"type": "thinking", "thinking": thinking_text})
            if visible_text:
                content.append({"type": "text", "text": visible_text})
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
            "model": target.model_id,
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

    async def _stream_openai_chat(self, payload: dict[str, Any], model: str) -> AsyncIterator[bytes]:
        target = self._target_for_model(model)
        outgoing = self._openai_chat_payload(payload, stream=True, model=model)
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
                    self._url("/v1/chat/completions", target),
                    headers=self._headers(target),
                    json=outgoing,
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise OpenRouterError(body.decode("utf-8", "replace"), response.status_code)
                    async for chunk in self._openai_sse_to_anthropic(
                        response.aiter_bytes(),
                        model=model,
                    ):
                        yield chunk
        except OpenRouterError:
            raise
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"VPS model stream failed: {exc}", status_code=502) from exc

    async def _openai_sse_to_anthropic(
        self,
        chunks: AsyncIterator[bytes],
        model: str | None = None,
    ) -> AsyncIterator[bytes]:
        target = self._target_for_model(model)
        yield b'event: message_start\ndata: {"type":"message_start","message":{"model":"'
        yield target.model_id.encode("utf-8")
        yield b'","role":"assistant","content":[]}}\n\n'
        state = _QwenThinkingStreamState()

        buffer = ""
        async for chunk in chunks:
            buffer += chunk.decode("utf-8", "replace")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                text_delta = self._openai_text_delta(event)
                if text_delta:
                    for outgoing in state.feed(text_delta):
                        yield outgoing

        if buffer:
            text_delta = self._openai_text_delta(buffer)
            if text_delta:
                for outgoing in state.feed(text_delta):
                    yield outgoing

        for outgoing in state.finish():
            yield outgoing
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


class _QwenThinkingStreamState:
    def __init__(self) -> None:
        self.raw_text = ""
        self.thinking_text = ""
        self.visible_text = ""
        self.thinking_started = False
        self.thinking_stopped = False
        self.text_started = False

    def feed(self, delta: str) -> list[bytes]:
        self.raw_text += str(delta or "")
        next_thinking, next_visible = split_thinking_text(self.raw_text, strip=False)
        events: list[bytes] = []

        if next_thinking and not self.thinking_started:
            events.append(
                _anthropic_sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                )
            )
            self.thinking_started = True

        thinking_delta = _next_delta(self.thinking_text, next_thinking)
        if thinking_delta:
            events.append(
                _anthropic_sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": thinking_delta},
                    },
                )
            )
            self.thinking_text = next_thinking

        if next_visible and self.thinking_started and not self.thinking_stopped:
            events.append(_anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
            self.thinking_stopped = True

        text_index = 1 if self.thinking_started else 0
        if next_visible and not self.text_started:
            events.append(
                _anthropic_sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            self.text_started = True

        visible_delta = _next_delta(self.visible_text, next_visible)
        if visible_delta:
            events.append(
                _anthropic_sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": text_index,
                        "delta": {"type": "text_delta", "text": visible_delta},
                    },
                )
            )
            self.visible_text = next_visible

        return events

    def finish(self) -> list[bytes]:
        events: list[bytes] = []
        if self.thinking_started and not self.thinking_stopped:
            events.append(_anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
            self.thinking_stopped = True
        if self.text_started:
            text_index = 1 if self.thinking_started else 0
            events.append(_anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": text_index}))
        return events


def _next_delta(previous: str, current: str) -> str:
    if not current or current == previous:
        return ""
    if current.startswith(previous):
        return current[len(previous) :]
    return current


def _anthropic_sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


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
