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

from .anthropic import split_thinking_text
from .config import Settings
from .openrouter import OpenRouterClient, OpenRouterError


OPENAI_CHAT_CONTEXT_TOKENS = 24_576
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
    "end with permission questions like 'posso comecar?' or 'deseja que eu leia?'. Answer in the user's "
    "language; if the user writes Portuguese, use Brazilian Portuguese. Do not repeat or rephrase the user's "
    "request back to yourself. If a tool fails or the workspace is incomplete, fix the tool arguments and "
    "retry when a reasonable retry exists, then explain what you can infer and what failed without asking "
    "for permission to continue. If Read fails because line_offset, line_count, offset, or limit is "
    "unsupported, immediately call Read again with only the required file path argument. If a file path is "
    "not found and the user did not give an exact path, call LS/Glob/Grep to discover files instead of asking "
    "for the path. If the user asks to create a new file such as .txt, .html, .js, or .py and the filename is "
    "not specified, choose a simple sensible filename in the current workspace and call Write. For code "
    "creation or edits, call Write, Edit, MultiEdit, or Bash as needed; do not provide file contents in the "
    "chat as a substitute for editing files. Tool-call JSON must be complete and include all required fields "
    "such as file_path and content. Never write a plain-text tool call in the chat; emit a real "
    "tool call through the tool API. Use enough tokens to finish the requested task."
)
LOCAL_TOOL_AGENT_PROMPT = (
    "Local workspace tool behavior override: the user expects you to use the available file/workspace tools. "
    "When the user asks to read, find, edit, create, patch, run tests, or change files, call the matching tool "
    "first instead of summarizing or saying you cannot find files. Start by listing or reading files when the "
    "exact path is unclear. Use read_file/list_files/apply_patch/write_file/run_tests when those are the tools "
    "available. If the user asks to create a new file and no filename is given, choose a simple filename and "
    "call write_file. Answer in the user's language; if the user writes Portuguese, use Brazilian Portuguese. "
    "Do not repeat the user's request as internal questions. Never write a plain-text tool call "
    "in the chat; emit a real tool call through the tool API. Do not answer with only a summary when an edit "
    "was requested."
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
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        requested = str(model or "").strip().lower()
        fast_names = {
            self.settings.cheap_code_agent.lower(),
            self.settings.fast_agent.lower(),
            self.settings.vps_fast_model_id.lower(),
            self.settings.vps_model_id.lower(),
        }
        has_fast_target = bool(self.settings.vps_fast_model_base_url or self.settings.vps_fast_model_id)
        if has_fast_target and (requested in fast_names or "flash" in requested or "economy" in requested):
            return self._fast_target()
        if not self.settings.vps_strong_model_id:
            return self._default_target()
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
        outgoing.pop("thinking", None)
        return outgoing

    async def complete_messages(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        target = self._target_for_model(model)
        if self._api_format(target) == "openai-chat":
            return await self._complete_openai_chat(payload, model)

        outgoing = self._payload_for_model(payload, model)
        outgoing["stream"] = False
        timeout = httpx.Timeout(self.settings.vps_model_timeout_seconds)

        try:
            response = await self._client.post(
                self._url("/v1/messages", target),
                headers=self._headers(target),
                json=outgoing,
                timeout=timeout,
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
            async with self._client.stream(
                    "POST",
                    self._url("/v1/messages", target),
                    headers=self._headers(target),
                    json=outgoing,
                    timeout=timeout,
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
        disable_hidden_thinking = self._should_disable_hidden_thinking(payload)
        if disable_hidden_thinking:
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
        if disable_hidden_thinking and self._supports_chat_template_thinking_toggle(target):
            outgoing["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            outgoing["tools"] = tools
            if self._should_force_tool_choice(payload):
                outgoing["tool_choice"] = "required"
            elif payload.get("tool_choice"):
                outgoing["tool_choice"] = self._tool_choice_to_openai(payload["tool_choice"])
        return outgoing

    def _should_disable_hidden_thinking(self, payload: dict[str, Any]) -> bool:
        if not self.settings.vps_disable_qwen_thinking:
            return False
        if str(payload.get("__gateway_reasoning") or "").strip().lower() == "none":
            return True
        if self._is_claude_code_client(payload):
            return True
        # Tool-heavy/cowork requests can otherwise stream only hidden thinking blocks.
        return bool(payload.get("tools") or payload.get("tool_choice"))

    def _supports_chat_template_thinking_toggle(self, target: VPSTarget) -> bool:
        model_id = target.model_id.lower()
        return "qwen3" in model_id or "qwen/qwen3" in model_id

    def _is_claude_code_client(self, payload: dict[str, Any]) -> bool:
        return str(payload.get("__gateway_client") or "").strip().lower() == "claude-code"

    def _is_claude_code_action_request(self, payload: dict[str, Any]) -> bool:
        if not self._is_claude_code_client(payload):
            return False
        return self._is_tool_action_request(payload, aggressive=True)

    def _is_tool_action_request(self, payload: dict[str, Any], *, aggressive: bool = False) -> bool:
        if not payload.get("tools"):
            return False
        if self._payload_has_tool_result(payload):
            return True
        text = self._current_user_request_text(payload).lower()
        if not text:
            return False
        if self._looks_like_question(text):
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
            "cria",
            "crie",
            "criar",
            "debug",
            "edite",
            "editar",
            "envie",
            "enviar",
            "execute",
            "executar",
            "file",
            "files",
            "fix",
            "faca",
            "faz",
            "fazer",
            "implemente",
            "implement",
            "listar",
            "list",
            "ler",
            "leia",
            "make",
            "mande",
            "mexa",
            "mexer",
            "modifique",
            "monte",
            "read",
            "rode",
            "rodar",
            "salve",
            "save",
            "site",
            "suba",
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
            "github",
            "preciso",
            "push",
            "quero",
        )
        if any(term in text for term in action_terms):
            return True
        if self._looks_like_smalltalk(text):
            return False
        return aggressive

    def _should_force_tool_choice(self, payload: dict[str, Any]) -> bool:
        is_action_request = self._is_claude_code_action_request(payload) or self._is_tool_action_request(
            payload,
            aggressive=False,
        )
        if not is_action_request:
            return False
        if not self._payload_has_tool_result(payload):
            return True
        return self._is_file_change_request(payload) and not self._payload_has_mutating_tool_use(payload)

    def _should_force_claude_code_tool_choice(self, payload: dict[str, Any]) -> bool:
        return self._should_force_tool_choice(payload)

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

    def _looks_like_smalltalk(self, text: str) -> bool:
        compact = " ".join(str(text or "").strip().lower().split())
        compact = compact.strip(" .,!;:")
        if not compact:
            return False
        exact = {
            "eae",
            "iae",
            "iai",
            "oi",
            "ola",
            "olá",
            "e ai",
            "e aí",
            "bom dia",
            "boa tarde",
            "boa noite",
            "beleza",
            "valeu",
            "obrigado",
            "obrigada",
        }
        if compact in exact:
            return True
        words = compact.split()
        greeting_prefixes = ("oi ", "ola ", "olá ", "bom dia ", "boa tarde ", "boa noite ")
        return len(words) <= 4 and compact.startswith(greeting_prefixes)

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

    def _payload_has_mutating_tool_use(self, payload: dict[str, Any]) -> bool:
        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, dict):
                content = [content]
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_name = str(block.get("name") or "").strip().lower()
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if tool_name in {
                    "write",
                    "edit",
                    "multiedit",
                    "notebookedit",
                    "apply_patch",
                    "write_file",
                    "delete_file",
                    "replace_file",
                    "create_file",
                }:
                    return True
                if tool_name == "bash" and self._bash_command_can_mutate(str(tool_input.get("command") or "")):
                    return True
        return False

    def _bash_command_can_mutate(self, command: str) -> bool:
        compact = " ".join(str(command or "").lower().split())
        if not compact:
            return False
        readonly_prefixes = (
            "pwd",
            "ls",
            "find",
            "rg",
            "grep",
            "cat",
            "sed -n",
            "git status",
            "git diff",
            "git log",
            "git show",
            "npm test",
            "pytest",
            "python -m pytest",
            "python3 -m pytest",
        )
        if compact.startswith(readonly_prefixes) and not re.search(r"\b(>|tee|touch|mkdir|mv|cp|rm|git add|git commit|git push)\b", compact):
            return False
        mutating_terms = (
            ">",
            "tee ",
            "touch ",
            "mkdir ",
            "mv ",
            "cp ",
            "rm ",
            "apply_patch",
            "npm install",
            "npm run build",
            "git add",
            "git commit",
            "git push",
        )
        return any(term in compact for term in mutating_terms)

    def _is_file_change_request(self, payload: dict[str, Any]) -> bool:
        text = self._task_request_text(payload).lower()
        if not text or self._looks_like_question(text):
            return False
        change_terms = (
            "alter",
            "aplicar patch",
            "build",
            "conserte",
            "corrija",
            "corrigir",
            "create",
            "cria",
            "crie",
            "criar",
            "edite",
            "editar",
            "faca",
            "fassa",
            "faça",
            "faz",
            "fazer",
            "fix",
            "implemente",
            "implement",
            "modifique",
            "monte",
            "patch",
            "salve",
            "site",
            "write",
        )
        return any(term in text for term in change_terms)

    def _task_request_text(self, payload: dict[str, Any]) -> str:
        for message in reversed(payload.get("messages") or []):
            if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
                continue
            content = message.get("content")
            if isinstance(content, list) and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            text = self._content_to_text(content)
            if not text:
                continue
            synthetic = {"messages": [{"role": "user", "content": text}]}
            return self._current_user_request_text(synthetic)
        return self._current_user_request_text(payload)

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
        if self._is_claude_code_action_request(payload) or self._is_tool_action_request(payload, aggressive=False):
            action_prompt = CLAUDE_CODE_AGENT_PROMPT if self._is_claude_code_client(payload) else LOCAL_TOOL_AGENT_PROMPT
            system_text = f"{system_text}\n\n{action_prompt}" if system_text else action_prompt
        if system_text:
            messages.append({"role": "system", "content": system_text})

        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            messages.extend(self._message_to_openai_chat_messages(message))

        if self._is_claude_code_action_request(payload) or self._is_tool_action_request(payload, aggressive=False):
            if self._payload_has_tool_result(payload):
                messages = self._messages_with_post_tool_nudge(messages)
            else:
                messages = self._messages_with_claude_code_agent_nudge(messages)

        return messages or [{"role": "user", "content": ""}]

    def _message_to_openai_chat_messages(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        role = str(message.get("role") or "user")
        if role not in {"user", "assistant", "system", "tool"}:
            role = "user"

        if role == "tool":
            return [
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "content": self._content_to_text(message.get("content")),
                }
            ]

        content = message.get("content")
        if not isinstance(content, list):
            return [{"role": role, "content": self._content_to_text(content)}]

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "tool_use":
                    name = str(block.get("name") or "")
                    tool_calls.append(
                        {
                            "id": str(block.get("id") or f"toolu_{index}"),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    _normalize_claude_code_tool_input(
                                        name,
                                        block.get("input") if isinstance(block.get("input"), dict) else {},
                                    )
                                ),
                            },
                        }
                    )
                else:
                    text = self._content_to_text(block)
                    if text:
                        text_parts.append(text)
            outgoing: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                outgoing["tool_calls"] = tool_calls
            return [outgoing]

        messages: list[dict[str, Any]] = []
        text_parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                if text_parts:
                    messages.append({"role": role, "content": "\n".join(text_parts)})
                    text_parts = []
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or block.get("id") or ""),
                        "content": self._content_to_text(block.get("content")),
                    }
                )
            else:
                text = self._content_to_text(block)
                if text:
                    text_parts.append(text)
        if text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
        return messages

    def _messages_with_claude_code_agent_nudge(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = deepcopy(messages)
        reminder = (
            "<system-reminder>Execute the user's project request now. If tools are available, use them before "
            "answering. Never ask permission to begin or continue; never end with 'Deseja que eu continue?', "
            "'posso comecar?', or similar. Reply in the same language as the user, using Brazilian Portuguese "
            "for Portuguese requests. Do not ask yourself repeated questions in the response. Ignore .venv, "
            ".git, node_modules, __pycache__, and site-packages "
            "unless explicitly requested. For project analysis, inspect root files and likely manifests/source "
            "files, then give the answer. If the user asks to create, edit, modify, run, or test anything, "
            "call the appropriate tool such as Bash, Write, Edit, MultiEdit, read_file, write_file, apply_patch, "
            "or run_tests; do not answer with instructions for the user to do it manually. If a file is not "
            "found and no exact path was provided, list or search the workspace instead of asking for the path. "
            "If the user asks for a new .txt/.html/.js/.py without a filename, choose a simple filename and "
            "write it. If a Read call failed because line_offset, line_count, offset, or limit was rejected, "
            "retry Read immediately with only the file path argument. Never print a plain-text tool call "
            "text; make an actual tool call.</system-reminder>"
        )
        for message in reversed(copied):
            if message.get("role") == "user":
                content = str(message.get("content") or "")
                message["content"] = f"{content}\n\n{reminder}" if content else reminder
                return copied
        copied.append({"role": "user", "content": reminder})
        return copied

    def _messages_with_post_tool_nudge(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = deepcopy(messages)
        reminder = (
            "<system-reminder>Use the latest tool result exactly once. If it already contains enough "
            "information to answer or finish the edit, give the final answer now in the user's language. "
            "Only call another tool when a concrete missing file, command, or edit is required. Do not repeat "
            "the same tool call, do not print a plain-text tool call, and do not ask permission to "
            "continue.</system-reminder>"
        )
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
                    "Previous tool call: "
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
        available = self._openai_chat_context_tokens() - estimated_input_tokens - OPENAI_CHAT_CONTEXT_MARGIN_TOKENS
        return max(1, min(max(1, requested_max_tokens), available))

    def _openai_chat_context_tokens(self) -> int:
        configured = int(getattr(self.settings, "vps_openai_chat_context_tokens", 0) or 0)
        return max(1, configured or OPENAI_CHAT_CONTEXT_TOKENS)

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
            response = await self._client.post(
                self._url("/v1/chat/completions", target),
                headers=self._headers(target),
                json=outgoing,
                timeout=timeout,
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
        response_payload = self._anthropic_from_openai_chat(data, model=model)
        self._ensure_required_tool_call(payload, response_payload)
        return response_payload

    def _ensure_required_tool_call(self, payload: dict[str, Any], response: dict[str, Any]) -> None:
        if self._should_force_claude_code_tool_choice(payload) and not _response_has_tool_use(response):
            fallback_tool = self._fallback_tool_use_for_required_action(payload)
            if fallback_tool:
                response["content"] = [fallback_tool]
                response["stop_reason"] = "tool_use"
                return
            raise OpenRouterError(
                "VPS model ignored required Claude Code tool call.",
                status_code=502,
            )

    def _fallback_tool_use_for_required_action(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        tool_names = self._available_tool_names(payload)
        if not tool_names:
            return None

        def first_available(*names: str) -> str | None:
            wanted = {name.lower() for name in names}
            for tool_name in tool_names:
                if tool_name.lower() in wanted:
                    return tool_name
            return None

        if tool_name := first_available("LS", "list_files"):
            return {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {"path": "."},
            }
        if tool_name := first_available("Glob"):
            return {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {"pattern": "**/*"},
            }
        if tool_name := first_available("Bash", "run_command"):
            command_key = "command" if tool_name.lower() == "bash" else "cmd"
            return {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {
                    command_key: "pwd && find . -maxdepth 2 -type f | head -80",
                    "description": "Inspect workspace files before editing",
                },
            }
        if tool_name := first_available("read_file", "Read"):
            return {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {"file_path": "README.md"},
            }
        return None

    def _available_tool_names(self, payload: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for tool in payload.get("tools") or []:
            if isinstance(tool, dict) and tool.get("name"):
                names.append(str(tool["name"]))
        return names

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
            textual_tool_calls = _textual_tool_calls_from_text(visible_text)
            if textual_tool_calls:
                for tool_call in textual_tool_calls:
                    function = tool_call["function"]
                    content.append(
                        {
                            "type": "tool_use",
                            "id": str(tool_call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "input": _normalize_claude_code_tool_input(
                                str(function.get("name") or ""),
                                _json_object_from_string(function.get("arguments")),
                            ),
                        }
                    )
            elif visible_text:
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
                        "input": _normalize_claude_code_tool_input(
                            str(function.get("name") or ""),
                            self._json_object(function.get("arguments")),
                        ),
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
        return _json_object_from_string(value)

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
            async with self._client.stream(
                    "POST",
                    self._url("/v1/chat/completions", target),
                    headers=self._headers(target),
                    json=outgoing,
                    timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise OpenRouterError(body.decode("utf-8", "replace"), response.status_code)
                async for chunk in self._openai_sse_to_anthropic(
                    response.aiter_bytes(),
                    model=model,
                    require_tool_call=self._should_force_tool_choice(payload),
                    payload=payload,
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
        require_tool_call: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> AsyncIterator[bytes]:
        target = self._target_for_model(model)
        yield b'event: message_start\ndata: {"type":"message_start","message":{"model":"'
        yield target.model_id.encode("utf-8")
        yield b'","role":"assistant","content":[]}}\n\n'
        state = _QwenThinkingStreamState()
        tool_state = _OpenAIToolCallStreamState(state)
        textual_tool_buffer = "" if require_tool_call else None
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
                if text_delta and textual_tool_buffer is not None and not tool_state.has_tool:
                    textual_tool_buffer += text_delta
                elif text_delta:
                    for outgoing in state.feed(text_delta):
                        yield outgoing
                if tool_calls:
                    textual_tool_buffer = "" if textual_tool_buffer is not None else None
                    for outgoing in tool_state.feed(tool_calls):
                        yield outgoing

        if buffer:
            text_delta, tool_calls, finish_reason = self._openai_stream_delta(buffer)
            if finish_reason == "tool_calls":
                stop_reason = "tool_use"
            elif finish_reason in {"stop", "length", "content_filter"} and stop_reason != "tool_use":
                stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
            if text_delta and textual_tool_buffer is not None and not tool_state.has_tool:
                textual_tool_buffer += text_delta
            elif text_delta:
                for outgoing in state.feed(text_delta):
                    yield outgoing
            if tool_calls:
                textual_tool_buffer = "" if textual_tool_buffer is not None else None
                for outgoing in tool_state.feed(tool_calls):
                    yield outgoing

        if textual_tool_buffer and not tool_state.has_tool:
            textual_tool_calls = _textual_tool_calls_from_text(textual_tool_buffer)
            if textual_tool_calls:
                for outgoing in tool_state.feed(textual_tool_calls):
                    yield outgoing
            elif require_tool_call and payload:
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool:
                    for outgoing in tool_state.feed([_openai_tool_call_from_anthropic_tool_use(fallback_tool, 0)]):
                        yield outgoing
            else:
                for outgoing in state.feed(textual_tool_buffer):
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

        return events

    def finish(self) -> list[bytes]:
        events: list[bytes] = []
        for index in self.started_order:
            call = self.calls.get(index) or {}
            normalized_arguments = _normalize_claude_code_tool_input(
                str(call.get("name") or ""),
                _json_object_from_string(call.get("arguments")),
            )
            events.append(
                _anthropic_sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._block_index(index),
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(normalized_arguments),
                        },
                    },
                )
            )
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


def _response_has_tool_use(response: dict[str, Any]) -> bool:
    content = response.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in content
    )


def _textual_tool_calls_from_text(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    lowered = str(text or "").lower()
    search_from = 0
    while True:
        marker = lowered.find("tool use", search_from)
        if marker < 0:
            break
        colon = lowered.find(":", marker)
        if colon < 0:
            break
        object_start = text.find("{", colon)
        if object_start < 0:
            break
        object_end = _balanced_json_object_end(text, object_start)
        if object_end < 0:
            break
        parsed = _json_object_from_string(text[object_start : object_end + 1])
        call = _tool_call_from_textual_payload(parsed, len(calls))
        if call:
            calls.append(call)
        search_from = object_end + 1
    return calls


def _balanced_json_object_end(text: str, start: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _tool_call_from_textual_payload(payload: dict[str, Any], index: int) -> dict[str, Any] | None:
    name = str(payload.get("name") or payload.get("tool") or payload.get("tool_name") or "").strip()
    if not name:
        return None
    arguments = payload.get("input")
    if not isinstance(arguments, dict):
        arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return {
        "index": index,
        "id": str(payload.get("id") or f"call_text_{index}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _openai_tool_call_from_anthropic_tool_use(block: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "id": str(block.get("id") or f"call_gateway_{index}"),
        "type": "function",
        "function": {
            "name": str(block.get("name") or ""),
            "arguments": json.dumps(block.get("input") if isinstance(block.get("input"), dict) else {}),
        },
    }


def _json_object_from_string(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    candidates = [value]
    if '\\"' in value:
        candidates.append(value.replace('\\"', '"'))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _normalize_claude_code_tool_input(tool_name: str, value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value) if isinstance(value, dict) else {}
    name = tool_name.strip().lower()

    if name in {"read", "write", "edit", "multiedit"}:
        if "file_path" not in normalized and "path" in normalized:
            normalized["file_path"] = normalized["path"]
        normalized.pop("path", None)

    if name == "notebookedit":
        if "notebook_path" not in normalized:
            if "path" in normalized:
                normalized["notebook_path"] = normalized["path"]
            elif "file_path" in normalized:
                normalized["notebook_path"] = normalized["file_path"]
        normalized.pop("path", None)
        normalized.pop("file_path", None)

    if name == "bash":
        if "command" not in normalized:
            if "cmd" in normalized:
                normalized["command"] = normalized["cmd"]
            elif "script" in normalized:
                normalized["command"] = normalized["script"]
        normalized.pop("cmd", None)
        normalized.pop("script", None)

    if name == "grep":
        if "glob" not in normalized:
            for alias in ("file_pattern", "filePattern", "include"):
                if alias in normalized:
                    normalized["glob"] = normalized[alias]
                    break
        if "path" not in normalized:
            for alias in ("dir", "directory", "root"):
                if alias in normalized:
                    normalized["path"] = normalized[alias]
                    break
        if "pattern" not in normalized:
            for alias in ("query", "search", "searchTerm", "regex"):
                if alias in normalized:
                    normalized["pattern"] = normalized[alias]
                    break
        for invalid in (
            "files",
            "file",
            "paths",
            "file_pattern",
            "filePattern",
            "include",
            "dir",
            "directory",
            "root",
            "query",
            "search",
            "searchTerm",
            "regex",
        ):
            normalized.pop(invalid, None)

    if name == "glob":
        if "pattern" not in normalized:
            for alias in ("glob", "file_pattern", "filePattern", "include"):
                if alias in normalized:
                    normalized["pattern"] = normalized[alias]
                    break
        if "path" not in normalized:
            for alias in ("dir", "directory", "root"):
                if alias in normalized:
                    normalized["path"] = normalized[alias]
                    break
        for invalid in ("files", "paths", "glob", "file_pattern", "filePattern", "include", "dir", "directory", "root"):
            normalized.pop(invalid, None)

    if name == "ls":
        if "path" not in normalized:
            for alias in ("dir", "directory", "root"):
                if alias in normalized:
                    normalized["path"] = normalized[alias]
                    break
        for invalid in ("files", "paths", "dir", "directory", "root"):
            normalized.pop(invalid, None)

    return normalized


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

    async def aclose(self) -> None:
        for client in (self.primary, self.fallback):
            close = getattr(client, "aclose", None)
            if close:
                await close()

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
