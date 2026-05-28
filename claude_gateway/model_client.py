from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .anthropic import clean_model_text, split_thinking_text
from .config import Settings
from .openrouter import OpenRouterClient, OpenRouterError


OPENAI_CHAT_CONTEXT_TOKENS = 32_768
OPENAI_CHAT_CONTEXT_MARGIN_TOKENS = 512
OPENAI_CHAT_INPUT_BUDGET_TOKENS = 18_000
OPENAI_CHAT_MIN_TRIMMED_CHARS = 1_200
CLAUDE_CODE_AGENT_PROMPT = (
    "Claude Code agent behavior override for this local model: when the user asks you to inspect, analyze, "
    "list, read, fix, test, or start work in the project, immediately use the available tools instead of "
    "asking for permission or asking if you should begin. Ask a question only if a required detail is truly "
    "missing and no reasonable first action exists. Do not stop after saying what you will do; take the next "
    "tool action, then summarize what you found. For project analysis, inspect the repository root first, "
    "ignore dependency/cache folders such as .venv, .git, node_modules, __pycache__, and site-packages unless "
    "the user specifically asks about them, then read likely manifest or entry files before answering. Never "
    "end with permission questions like 'posso comecar?' or 'deseja que eu leia?'. If a tool fails or the "
    "workspace is incomplete, explain what you can infer and what failed, without asking for permission to "
    "continue. For code creation or edits, call Write, Edit, MultiEdit, or Bash as needed; do not provide "
    "file contents in the chat as a substitute for editing files. Tool-call JSON must be complete and include "
    "all required fields such as file_path and content. Use enough tokens to finish the requested task."
)
CLAUDE_CODE_SYSTEM_REMINDER_RE = re.compile(r"(?is)<system-reminder>.*?</system-reminder>")
CLAUDE_CODE_SESSION_RE = re.compile(r"(?is)<session>.*?</session>")


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
            connect=10.0,
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
        tools = self._tools_to_openai(payload.get("tools"))
        tools = self._compact_tools_for_openai_chat_context(messages, tools)
        messages = self._trim_messages_for_openai_chat_context(messages, tools)
        requested_max_tokens = int(payload.get("max_tokens") or 4096)
        outgoing: dict[str, Any] = {
            "model": target.model_id,
            "messages": messages,
            "max_tokens": self._fit_max_tokens_for_openai_chat(
                messages,
                tools,
                requested_max_tokens=requested_max_tokens,
            ),
            "stream": stream,
        }
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "stop"):
            if key in payload:
                outgoing[key] = payload[key]
        if tools:
            outgoing["tools"] = tools
            if payload.get("tool_choice"):
                outgoing["tool_choice"] = self._tool_choice_to_openai(payload["tool_choice"])
            elif self._is_claude_code_action_request(payload):
                outgoing["tool_choice"] = "required"
        return outgoing

    def _should_disable_qwen_thinking(self, payload: dict[str, Any], target: VPSTarget) -> bool:
        model_id = target.model_id.lower()
        if "qwen3" not in model_id and "qwen/qwen3" not in model_id:
            return False
        if str(payload.get("__gateway_reasoning") or "").strip().lower() == "none":
            return True
        if self._is_claude_code_client(payload):
            return True
        # Claude Code/tool-heavy requests can otherwise stream only hidden thinking blocks.
        return bool(payload.get("tools") or payload.get("tool_choice"))

    def _is_claude_code_client(self, payload: dict[str, Any]) -> bool:
        return str(payload.get("__gateway_client") or "").strip().lower() == "claude-code"

    def _is_claude_code_action_request(self, payload: dict[str, Any]) -> bool:
        if not self._is_claude_code_client(payload):
            return False
        if self._payload_has_tool_result(payload):
            return False
        if not payload.get("tools"):
            return False
        text = self._current_user_request_text(payload).lower()
        if not text:
            return False
        action_terms = (
            "analis",
            "analise",
            "analyze",
            "estrutura",
            "project",
            "projeto",
            "arquivo",
            "arquivos",
            "alterar",
            "altere",
            "aplicar patch",
            "build",
            "commit",
            "comando",
            "corrija",
            "corrigir",
            "create",
            "crie",
            "criar",
            "debug",
            "edite",
            "editar",
            "execute",
            "executar",
            "file",
            "files",
            "fix",
            "faca",
            "implemente",
            "implement",
            "listar",
            "list",
            "ler",
            "leia",
            "make",
            "mexa",
            "mexer",
            "modifique",
            "read",
            "rode",
            "rodar",
            "salve",
            "save",
            "test",
            "teste",
            "tests",
            "terminal",
            "verificar",
            "write",
            "inspect",
            "começar",
            "comecar",
            "fassa",
            "faça",
        )
        if any(term in text for term in action_terms):
            return True
        return not self._looks_like_question(text)

    def _looks_like_question(self, text: str) -> bool:
        compact = " ".join(str(text or "").strip().lower().split())
        if not compact:
            return False
        if "?" in compact:
            return True
        question_prefixes = (
            "como ",
            "how ",
            "o que ",
            "what ",
            "onde ",
            "where ",
            "por que ",
            "porque ",
            "why ",
            "qual ",
            "quais ",
            "which ",
            "quem ",
            "who ",
            "quando ",
            "when ",
            "quanto ",
            "quantos ",
            "quantas ",
            "can you explain ",
            "voce pode explicar ",
            "você pode explicar ",
        )
        return compact.startswith(question_prefixes)

    def _payload_has_tool_result(self, payload: dict[str, Any]) -> bool:
        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                return True
        return False

    def _last_user_text(self, payload: dict[str, Any]) -> str:
        for message in reversed(payload.get("messages") or []):
            if isinstance(message, dict) and str(message.get("role") or "").lower() == "user":
                return self._content_to_text(message.get("content"))
        return ""

    def _current_user_request_text(self, payload: dict[str, Any]) -> str:
        text = self._last_user_text(payload)
        if not text:
            return ""
        text = CLAUDE_CODE_SYSTEM_REMINDER_RE.sub("\n", text)
        text = CLAUDE_CODE_SESSION_RE.sub("\n", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        current: list[str] = []
        for line in lines:
            if current and self._looks_like_embedded_context(line):
                break
            current.append(line)
            if len(current) >= 3:
                break
        return "\n".join(current).strip()

    def _looks_like_embedded_context(self, line: str) -> bool:
        lowered = line.lower()
        prefixes = (
            "from __future__",
            "import ",
            "class ",
            "def ",
            "const ",
            "let ",
            "var ",
            "function ",
            "```",
            "traceback ",
            "allanm",
        )
        return lowered.startswith(prefixes)

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
        if self._is_claude_code_action_request(payload):
            system_text = f"{system_text}\n\n{CLAUDE_CODE_AGENT_PROMPT}" if system_text else CLAUDE_CODE_AGENT_PROMPT
        if system_text:
            messages.append({"role": "system", "content": system_text})

        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            if role not in {"user", "assistant", "system"}:
                role = "user"
            messages.append({"role": role, "content": self._content_to_text(message.get("content"))})

        if self._is_claude_code_action_request(payload):
            messages = self._messages_with_claude_code_agent_nudge(messages)

        return messages or [{"role": "user", "content": ""}]

    def _messages_with_claude_code_agent_nudge(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = deepcopy(messages)
        reminder = (
            "<system-reminder>Execute the user's project request now. If tools are available, use them before "
            "answering. Never ask permission to begin or continue; never end with 'Deseja que eu continue?', "
            "'posso comecar?', or similar. Ignore .venv, .git, node_modules, __pycache__, and site-packages "
            "unless explicitly requested. For project analysis, inspect root files and likely manifests/source "
            "files, then give the answer. If the user asks to create, edit, modify, run, or test anything, "
            "call the appropriate tool such as Bash, Write, Edit, or MultiEdit; do not answer with instructions "
            "for the user to do it manually.</system-reminder>"
        )
        for message in reversed(copied):
            if message.get("role") == "user":
                content = str(message.get("content") or "")
                message["content"] = f"{content}\n\n{reminder}" if content else reminder
                return copied
        copied.append({"role": "user", "content": reminder})
        return copied

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

    def _compact_tools_for_openai_chat_context(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not tools:
            return tools
        if self._estimate_openai_chat_input_tokens(messages, tools) <= OPENAI_CHAT_INPUT_BUDGET_TOKENS:
            return tools

        compacted = deepcopy(tools)
        for tool in compacted:
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            function["description"] = self._truncate_text_end(str(function.get("description") or ""), 600)
            function["parameters"] = self._compact_json_schema(function.get("parameters"))

        if self._estimate_openai_chat_input_tokens(messages, compacted) <= OPENAI_CHAT_INPUT_BUDGET_TOKENS:
            return compacted

        for tool in compacted:
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            function["description"] = self._truncate_text_end(str(function.get("description") or ""), 180)
            function["parameters"] = {"type": "object", "additionalProperties": True}
        return compacted

    def _compact_json_schema(self, schema: Any, *, depth: int = 0) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {"type": "object", "additionalProperties": True}

        compacted: dict[str, Any] = {}
        schema_type = schema.get("type")
        if isinstance(schema_type, str):
            compacted["type"] = schema_type
        elif isinstance(schema_type, list):
            compacted["type"] = schema_type[:3]
        else:
            compacted["type"] = "object"

        description = schema.get("description")
        if isinstance(description, str) and description:
            compacted["description"] = self._truncate_text_end(description, 180 if depth else 300)

        enum = schema.get("enum")
        if isinstance(enum, list) and len(enum) <= 20:
            compacted["enum"] = enum

        required = schema.get("required")
        if isinstance(required, list):
            compacted["required"] = required[:30]

        properties = schema.get("properties")
        if isinstance(properties, dict) and depth < 2:
            compacted["properties"] = {
                str(name): self._compact_json_schema(value, depth=depth + 1)
                for name, value in properties.items()
                if isinstance(value, dict)
            }
        elif compacted.get("type") == "object":
            compacted["additionalProperties"] = True

        items = schema.get("items")
        if isinstance(items, dict) and depth < 2:
            compacted["items"] = self._compact_json_schema(items, depth=depth + 1)

        return compacted

    def _fit_max_tokens_for_openai_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        requested_max_tokens: int,
    ) -> int:
        estimated_input_tokens = self._estimate_openai_chat_input_tokens(messages, tools)
        available = OPENAI_CHAT_CONTEXT_TOKENS - estimated_input_tokens - OPENAI_CHAT_CONTEXT_MARGIN_TOKENS
        return max(1, min(max(1, requested_max_tokens), available))

    def _trim_messages_for_openai_chat_context(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        trimmed = deepcopy(messages) or [{"role": "user", "content": ""}]
        while self._estimate_openai_chat_input_tokens(trimmed, tools) > OPENAI_CHAT_INPUT_BUDGET_TOKENS:
            removable_index = next(
                (idx for idx, message in enumerate(trimmed[:-1]) if message.get("role") != "system"),
                None,
            )
            if removable_index is not None:
                trimmed.pop(removable_index)
                continue

            longest_index = max(range(len(trimmed)), key=lambda idx: len(str(trimmed[idx].get("content") or "")))
            content = str(trimmed[longest_index].get("content") or "")
            if len(content) <= OPENAI_CHAT_MIN_TRIMMED_CHARS:
                break
            target_chars = max(OPENAI_CHAT_MIN_TRIMMED_CHARS, int(len(content) * 0.65))
            trimmed[longest_index]["content"] = self._truncate_text_middle(content, target_chars)
        return trimmed

    def _estimate_openai_chat_input_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
        char_estimate = len(serialized) / 3.2
        word_estimate = len(serialized.split()) * 1.35
        return max(1, int(max(char_estimate, word_estimate)) + (6 * len(messages)) + 32)

    def _truncate_text_middle(self, text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        marker = "\n\n[... previous content omitted to fit the model context window ...]\n\n"
        available = max(0, max_chars - len(marker))
        head = available // 2
        tail = available - head
        return f"{text[:head]}{marker}{text[-tail:]}"

    def _truncate_text_end(self, text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        marker = " [... omitted]"
        return text[: max(0, max_chars - len(marker))].rstrip() + marker

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
            connect=10.0,
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
        tool_state = _OpenAIToolCallStreamState(state)
        stop_reason = "end_turn"

        buffer = ""
        async for chunk in chunks:
            buffer += chunk.decode("utf-8", "replace")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                text_delta, tool_calls, finish_reason = self._openai_stream_delta(event)
                if finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                elif finish_reason in {"stop", "length", "content_filter"} and stop_reason != "tool_use":
                    stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
                if text_delta:
                    for outgoing in state.feed(text_delta):
                        yield outgoing
                if tool_calls:
                    for outgoing in tool_state.feed(tool_calls):
                        yield outgoing

        if buffer:
            text_delta, tool_calls, finish_reason = self._openai_stream_delta(buffer)
            if finish_reason == "tool_calls":
                stop_reason = "tool_use"
            elif finish_reason in {"stop", "length", "content_filter"} and stop_reason != "tool_use":
                stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
            if text_delta:
                for outgoing in state.feed(text_delta):
                    yield outgoing
            if tool_calls:
                for outgoing in tool_state.feed(tool_calls):
                    yield outgoing

        for outgoing in state.finish():
            yield outgoing
        for outgoing in tool_state.finish():
            yield outgoing
        if tool_state.has_tool:
            stop_reason = "tool_use"
        elif state.is_empty:
            stop_reason = "end_turn"
            fallback_text = "Nao consegui gerar uma chamada de ferramenta valida. Tente pedir novamente de forma mais direta."
            yield _anthropic_sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            )
            yield _anthropic_sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": fallback_text}},
            )
            yield _anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        elif stop_reason == "tool_use":
            stop_reason = "end_turn"
        yield (
            "event: message_delta\n"
            "data: "
            f"{json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}"
            "\n\n"
        ).encode("utf-8")
        yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    def _openai_text_delta(self, event: str) -> str:
        text_delta, _, _ = self._openai_stream_delta(event)
        return text_delta

    def _openai_stream_delta(self, event: str) -> tuple[str, list[dict[str, Any]], str | None]:
        data_lines = [
            line.removeprefix("data:").strip()
            for line in event.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            return "", [], None
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            return "", [], None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "", [], None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", [], None
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice, dict) else {}
        if not isinstance(delta, dict):
            return "", [], None
        content = delta.get("content")
        tool_calls = delta.get("tool_calls")
        return (
            content if isinstance(content, str) else "",
            tool_calls if isinstance(tool_calls, list) else [],
            str(choice.get("finish_reason")) if choice.get("finish_reason") else None,
        )


class _QwenThinkingStreamState:
    def __init__(self) -> None:
        self.raw_text = ""
        self.thinking_text = ""
        self.visible_text = ""
        self.thinking_started = False
        self.thinking_stopped = False
        self.text_started = False
        self.text_stopped = False

    @property
    def is_empty(self) -> bool:
        return not self.thinking_started and not self.text_started

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
        if self.text_started and not self.text_stopped:
            text_index = 1 if self.thinking_started else 0
            events.append(_anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": text_index}))
            self.text_stopped = True
        return events

    def close_before_tool(self) -> list[bytes]:
        return self.finish()

    def next_block_index(self) -> int:
        index = 0
        if self.thinking_started:
            index += 1
        if self.text_started:
            index += 1
        return index


class _OpenAIToolCallStreamState:
    def __init__(self, text_state: _QwenThinkingStreamState) -> None:
        self.text_state = text_state
        self.base_index: int | None = None
        self.calls: dict[int, dict[str, Any]] = {}
        self.started_order: list[int] = []

    @property
    def has_tool(self) -> bool:
        return any(bool(call.get("started")) for call in self.calls.values())

    def feed(self, tool_calls: list[dict[str, Any]]) -> list[bytes]:
        events: list[bytes] = []
        if tool_calls and self.base_index is None:
            events.extend(self.text_state.close_before_tool())
            self.base_index = self.text_state.next_block_index()

        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            index = self._tool_index(tool_call)
            call = self.calls.setdefault(
                index,
                {
                    "id": str(tool_call.get("id") or f"call_{index}"),
                    "name": "",
                    "arguments": "",
                    "emitted_arguments": 0,
                    "started": False,
                },
            )
            if tool_call.get("id"):
                call["id"] = str(tool_call["id"])

            function = tool_call.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    call["name"] = str(function["name"])
                if isinstance(function.get("arguments"), str):
                    call["arguments"] += function["arguments"]

            if not call["started"] and call["name"]:
                events.append(
                    _anthropic_sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": self._block_index(index),
                            "content_block": {
                                "type": "tool_use",
                                "id": call["id"],
                                "name": call["name"],
                                "input": {},
                            },
                        },
                    )
                )
                call["started"] = True
                self.started_order.append(index)

            if call["started"]:
                arguments = str(call["arguments"])
                emitted = int(call["emitted_arguments"])
                if len(arguments) > emitted:
                    delta = arguments[emitted:]
                    events.append(
                        _anthropic_sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": self._block_index(index),
                                "delta": {"type": "input_json_delta", "partial_json": delta},
                            },
                        )
                    )
                    call["emitted_arguments"] = len(arguments)

        return events

    def finish(self) -> list[bytes]:
        events: list[bytes] = []
        for index in self.started_order:
            events.append(
                _anthropic_sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._block_index(index)},
                )
            )
        return events

    def _tool_index(self, tool_call: dict[str, Any]) -> int:
        try:
            return int(tool_call.get("index") or 0)
        except (TypeError, ValueError):
            return 0

    def _block_index(self, tool_index: int) -> int:
        return int(self.base_index or 0) + tool_index


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
