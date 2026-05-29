from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

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
    "tool call through the tool API. Keep responses concise after each tool result: state what changed, "
    "what failed, and the next concrete action. Do not expose long internal reasoning, planning loops, "
    "or step-by-step thinking unless the user explicitly asks for an explanation."
)
LOCAL_TOOL_AGENT_PROMPT = (
    "Local workspace tool behavior override: the user expects you to use the available file/workspace tools. "
    "When the user asks to read, find, edit, create, patch, run tests, or change files, call the matching tool "
    "first instead of summarizing or saying you cannot find files. Start by listing or reading files when the "
    "exact path is unclear. Use read_file/list_files/apply_patch/write_file/run_tests when those are the tools "
    "available. If the user asks to create a new file and no filename is given, choose a simple filename and "
    "call write_file. Answer in the user's language; if the user writes Portuguese, use Brazilian Portuguese. "
    "Do not repeat the user's request as internal questions. Never write a plain-text tool call "
    "in the chat; emit a real tool call through the tool API. Keep tool-result replies short and action-oriented. "
    "Do not answer with only a summary when an edit was requested."
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

    def _is_openrouter_target(self, target: VPSTarget | None = None) -> bool:
        target = target or self._default_target()
        host = (urlparse(target.base_url).hostname or "").lower()
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")

    def _url(self, path: str, target: VPSTarget | None = None) -> str:
        target = target or self._default_target()
        base = target.base_url.rstrip("/")
        if base.endswith("/v1") and path.startswith("/v1/"):
            return f"{base}{path[3:]}"
        return f"{base}{path}"

    def _headers(self, target: VPSTarget | None = None) -> dict[str, str]:
        target = target or self._default_target()
        headers = {"Content-Type": "application/json"}
        api_key = target.api_key
        if self._is_openrouter_target(target):
            api_key = api_key or self.settings.openrouter_api_key
            headers["HTTP-Referer"] = self.settings.openrouter_site_url
            headers["X-Title"] = self.settings.openrouter_app_name
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
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
        timeout = self._request_timeout(payload)

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
        timeout = self._stream_timeout(payload)
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
                    if response.status_code == 404:
                        async for chunk in _anthropic_stream_error_message(
                            self._target_for_model(model).model_id,
                            "Backend de IA nao encontrado. Confira o pod RunPod ativo e a URL VPS_MODEL_BASE_URL.",
                        ):
                            yield chunk
                        return
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
        disable_tools = bool(tools and self._should_disable_tool_choice(payload))
        if disable_tools:
            tools = []
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
        if disable_tools:
            outgoing["tool_choice"] = "none"
        elif tools:
            outgoing["tools"] = tools
            if payload.get("tool_choice") and not self._is_auto_tool_choice(payload["tool_choice"]):
                outgoing["tool_choice"] = self._tool_choice_to_openai(payload["tool_choice"])
            elif self._should_force_tool_choice(payload):
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
        return self._is_tool_action_request(payload, aggressive=False)

    def _is_tool_action_request(self, payload: dict[str, Any], *, aggressive: bool = False) -> bool:
        if not payload.get("tools"):
            return False
        if self._payload_has_tool_result(payload):
            return True
        text = self._current_user_request_text(payload).lower()
        if not text:
            return False
        if self._looks_like_tool_action_continuation(text):
            return self._payload_has_prior_tool_action_request(payload)
        if self._looks_like_workspace_access_question(text):
            return True
        if self._has_tool_action_terms(text):
            return True
        if self._looks_like_question(text):
            return False
        if self._looks_like_smalltalk(text):
            return False
        return aggressive

    def _has_tool_action_terms(self, text: str) -> bool:
        compact = str(text or "").lower()
        action_terms = (
            "analis",
            "analise",
            "analyze",
            "apag",
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
            "delete",
            "delet",
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
            "remova",
            "remove",
            "remover",
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
            "tool",
            "verificar",
            "write",
            "ferramenta",
            "continuar",
            "continue",
            "inspect",
            "começar",
            "comecar",
            "fassa",
            "faça",
            "github",
            "prossiga",
            "prosseguir",
            "preciso",
            "push",
            "quero",
            "trabalhe",
            "trabalhar",
            "transforma",
            "transforme",
            "transformar",
        )
        return any(term in compact for term in action_terms)

    def _looks_like_tool_action_continuation(self, text: str) -> bool:
        compact = _strip_accents(" ".join(str(text or "").strip().lower().split()))
        if not compact:
            return False
        continuation_terms = (
            "todos",
            "tudo",
            "isso",
            "sim",
            "pode",
            "pode sim",
            "continua",
            "continue",
            "vai",
            "ok",
            "certo",
        )
        return compact in continuation_terms

    def _payload_has_prior_tool_action_request(self, payload: dict[str, Any]) -> bool:
        user_texts: list[str] = []
        for message in payload.get("messages") or []:
            if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
                continue
            content = message.get("content")
            if isinstance(content, list) and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            text = self._content_to_text(content)
            if text:
                user_texts.append(self._current_user_request_text({"messages": [{"role": "user", "content": text}]}))
        for text in user_texts[:-1]:
            lowered = text.lower()
            if self._looks_like_workspace_access_question(lowered) or self._has_tool_action_terms(lowered):
                return True
        return False

    def _should_force_tool_choice(self, payload: dict[str, Any]) -> bool:
        is_action_request = self._is_claude_code_action_request(payload) or self._is_tool_action_request(
            payload,
            aggressive=False,
        )
        if not is_action_request:
            return False
        if not self._payload_has_tool_result(payload):
            return True
        if self._latest_tool_result_error_text(payload):
            return True
        return self._is_file_change_request(payload) and not self._payload_has_successful_mutating_tool_use(payload)

    def _should_force_claude_code_tool_choice(self, payload: dict[str, Any]) -> bool:
        return self._should_force_tool_choice(payload)

    def _is_auto_tool_choice(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() == "auto"
        if isinstance(value, dict):
            return str(value.get("type") or "").strip().lower() == "auto"
        return False

    def _should_disable_tool_choice(self, payload: dict[str, Any]) -> bool:
        if not payload.get("tools"):
            return False
        return not (
            self._is_claude_code_action_request(payload)
            or self._is_tool_action_request(payload, aggressive=False)
        )

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

    def _looks_like_workspace_access_question(self, text: str) -> bool:
        compact = " ".join(str(text or "").strip().lower().split())
        if not compact or "?" not in compact:
            return False
        workspace_terms = (
            "arquivo",
            "arquivos",
            "diretorio",
            "diretório",
            "folder",
            "github",
            "pasta",
            "projeto",
            "repo",
            "repositorio",
            "repositório",
            "workspace",
        )
        access_terms = (
            "abre",
            "acessar",
            "acessa",
            "consegue",
            "encontra",
            "encontrar",
            "ler",
            "le",
            "lê",
            "listar",
            "mexer",
            "modificar",
            "ver",
        )
        return any(term in compact for term in workspace_terms) and any(
            term in compact for term in access_terms
        )

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

    def _payload_has_successful_mutating_tool_use(self, payload: dict[str, Any]) -> bool:
        mutating_tool_ids: set[str] = set()
        successful_results: set[str] = set()
        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, dict):
                content = [content]
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and self._is_mutating_tool_block(block):
                    tool_id = str(block.get("id") or "").strip()
                    if tool_id:
                        mutating_tool_ids.add(tool_id)
                elif block.get("type") == "tool_result" and not self._is_tool_result_error(block):
                    tool_id = str(block.get("tool_use_id") or block.get("id") or "").strip()
                    if tool_id:
                        successful_results.add(tool_id)
        return bool(mutating_tool_ids & successful_results)

    def _is_mutating_tool_block(self, block: dict[str, Any]) -> bool:
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
        return tool_name == "bash" and self._bash_command_can_mutate(str(tool_input.get("command") or ""))

    def _latest_tool_result_error_text(self, payload: dict[str, Any]) -> str:
        for message in reversed(payload.get("messages") or []):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, dict):
                content = [content]
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return self._content_to_text(block) if self._is_tool_result_error(block) else ""
        return ""

    def _is_tool_result_error(self, block: dict[str, Any]) -> bool:
        if bool(block.get("is_error")):
            return True
        text = self._content_to_text(block.get("content"))
        lowered = text.lower()
        return (
            "<tool_use_error>" in lowered
            or "error:" in lowered
            or lowered.startswith("error ")
            or "error writing file" in lowered
            or "failed to write" in lowered
            or "falha ao escrever" in lowered
            or "erro ao escrever" in lowered
        )

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
        if not text:
            return False
        if self._looks_like_change_continuation(text):
            return self._payload_has_prior_file_change_request(payload)
        if self._looks_like_question(text):
            return False
        return self._has_file_change_terms(text)

    def _has_file_change_terms(self, text: str) -> bool:
        compact = str(text or "").lower()
        change_terms = (
            "alter",
            "apag",
            "aplicar patch",
            "build",
            "conserte",
            "corrija",
            "corrigir",
            "create",
            "cria",
            "crie",
            "criar",
            "delete",
            "delet",
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
            "remova",
            "remove",
            "remover",
            "salve",
            "site",
            "transforma",
            "transforme",
            "transformar",
            "write",
        )
        return any(term in compact for term in change_terms)

    def _looks_like_change_continuation(self, text: str) -> bool:
        compact = _strip_accents(" ".join(str(text or "").strip().lower().split()))
        if not compact:
            return False
        continuation_terms = (
            "concordo",
            "pode comecar",
            "pode fazer",
            "pode iniciar",
            "comeca",
            "comece",
            "inicia",
            "inicie",
            "manda bala",
            "vai",
        )
        return any(term in compact for term in continuation_terms)

    def _payload_has_prior_file_change_request(self, payload: dict[str, Any]) -> bool:
        user_texts: list[str] = []
        for message in payload.get("messages") or []:
            if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
                continue
            content = message.get("content")
            if isinstance(content, list) and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            text = self._content_to_text(content)
            if text:
                user_texts.append(self._current_user_request_text({"messages": [{"role": "user", "content": text}]}))
        for text in user_texts[:-1]:
            if self._has_file_change_terms(text):
                return True
        return False

    def _is_change_status_question(self, payload: dict[str, Any]) -> bool:
        text = _strip_accents(" ".join(self._task_request_text(payload).lower().split()))
        if not text:
            return False
        status_terms = (
            "fez as alteracoes",
            "fez a alteracao",
            "alteracoes foram feitas",
            "mudancas foram feitas",
            "mudou os arquivos",
            "mexeu nos arquivos",
            "criou os arquivos",
            "salvou os arquivos",
            "aplicou",
            "foi feito",
            "ta feito",
            "esta feito",
        )
        return any(term in text for term in status_terms)

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

    def _latest_user_message_has_visible_text(self, payload: dict[str, Any]) -> bool:
        for message in reversed(payload.get("messages") or []):
            if not isinstance(message, dict) or str(message.get("role") or "").lower() != "user":
                continue
            content = message.get("content")
            if isinstance(content, list) and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                return False
            return bool(self._content_to_text(content).strip())
        return False

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
            content = message.get("content")
            if isinstance(content, list):
                first_text = next(
                    (
                        part
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                    ),
                    None,
                )
                if first_text is not None:
                    if not first_text["text"].lstrip().startswith("/no_think"):
                        first_text["text"] = f"/no_think\n\n{first_text['text']}"
                else:
                    content.insert(0, {"type": "text", "text": "/no_think"})
            else:
                text_content = str(content or "")
                if not text_content.lstrip().startswith("/no_think"):
                    message["content"] = f"/no_think\n\n{text_content}"
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
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                if parts:
                    messages.append({"role": role, "content": self._openai_user_content(parts)})
                    parts = []
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or block.get("id") or ""),
                        "content": self._content_to_text(block.get("content")),
                    }
                )
            else:
                openai_part = self._content_block_to_openai_user_part(block)
                if openai_part:
                    parts.append(openai_part)
        if parts:
            messages.append({"role": role, "content": self._openai_user_content(parts)})
        return messages

    def _openai_user_content(self, parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if len(parts) == 1 and parts[0].get("type") == "text":
            return str(parts[0].get("text") or "")
        return parts

    def _content_block_to_openai_user_part(self, block: dict[str, Any]) -> dict[str, Any] | None:
        block_type = str(block.get("type") or "")
        if block_type == "text" or isinstance(block.get("text"), str):
            text = self._content_to_text(block)
            return {"type": "text", "text": text} if text else None
        if block_type == "image":
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            media_type = str(source.get("media_type") or source.get("mediaType") or "").strip()
            data = str(source.get("data") or "").strip()
            if data and media_type.startswith("image/"):
                return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
        if block_type == "image_url":
            image_url = block.get("image_url") if isinstance(block.get("image_url"), dict) else {}
            url = str(image_url.get("url") or block.get("url") or "").strip()
            if url:
                return {"type": "image_url", "image_url": {"url": url}}
        text = self._content_to_text(block)
        return {"type": "text", "text": text} if text else None

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
            "text; make an actual tool call. Keep the visible reply compact; do not narrate long thinking "
            "or analysis before using tools.</system-reminder>"
        )
        for message in reversed(copied):
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    content.append({"type": "text", "text": reminder})
                else:
                    text_content = str(content or "")
                    message["content"] = f"{text_content}\n\n{reminder}" if text_content else reminder
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
            "continue. Never answer with generic help text such as 'como posso ajudar' or 'em que posso "
            "ajudar' after reading files; summarize the tool result or continue the requested task. If the "
            "user asked you to create, edit, fix, save, patch, commit, or otherwise change files, do not say "
            "it is done until a mutating tool such as Write, Edit, MultiEdit, apply_patch, write_file, or a "
            "mutating Bash command has succeeded.</system-reminder>"
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
            elif block_type == "image":
                source = block.get("source") if isinstance(block.get("source"), dict) else {}
                media_type = str(source.get("media_type") or source.get("mediaType") or "image").strip()
                parts.append(f"[Imagem anexada: {media_type}]")
            elif block_type == "image_url":
                parts.append("[Imagem anexada]")
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
        timeout = self._request_timeout(payload)

        try:
            response = await self._client.post(
                self._url("/v1/chat/completions", target),
                headers=self._headers(target),
                json=outgoing,
                timeout=timeout,
            )
            if self._should_retry_without_required_tool_choice(response.status_code, response.text, outgoing):
                outgoing = self._without_required_tool_choice(outgoing)
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
        if self._latest_tool_result_needs_worktree(payload):
            fallback_tool = self._fallback_tool_use_for_required_action(payload)
            if fallback_tool and not self._response_has_valid_worktree_tool_use(response):
                response["content"] = [fallback_tool]
                response["stop_reason"] = "tool_use"
                return
        if self._payload_has_tool_result(payload) and not _response_has_tool_use(response):
            response_text = self._response_text(response)
            file_write_tools = self._textual_file_write_tool_uses(response_text, payload)
            if file_write_tools:
                response["content"] = file_write_tools
                response["stop_reason"] = "tool_use"
                return
            if self._is_change_status_question(payload):
                response["content"] = [
                    {
                        "type": "text",
                        "text": self._physical_change_status_text(payload),
                    }
                ]
                response["stop_reason"] = "end_turn"
                return
            if self._looks_like_unneeded_detail_request(response_text) and self._is_tool_action_request(
                payload,
                aggressive=False,
            ):
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool:
                    response["content"] = [fallback_tool]
                    response["stop_reason"] = "tool_use"
                    return
            if self._is_generic_help_response(response_text):
                if self._is_file_change_request(payload):
                    fallback_tool = self._fallback_tool_use_for_required_action(payload)
                    if fallback_tool:
                        response["content"] = [fallback_tool]
                        response["stop_reason"] = "tool_use"
                        return
                if self._latest_user_message_has_visible_text(payload) and self._is_tool_action_request(
                    payload,
                    aggressive=False,
                ):
                    fallback_tool = self._fallback_tool_use_for_required_action(payload)
                    if fallback_tool:
                        response["content"] = [fallback_tool]
                        response["stop_reason"] = "tool_use"
                        return
                response["content"] = [
                    {
                        "type": "text",
                        "text": self._fallback_summary_from_latest_tool_result(payload),
                    }
                ]
                response["stop_reason"] = "end_turn"
                return
            if (
                self._is_tool_action_request(payload, aggressive=False)
                and self._looks_like_non_executing_action_response(response_text)
            ):
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool:
                    response["content"] = [fallback_tool]
                    response["stop_reason"] = "tool_use"
                    return
            if (
                self._is_file_change_request(payload)
                and not self._payload_has_successful_mutating_tool_use(payload)
                and self._looks_like_completed_file_change_response(response_text)
            ):
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool and self._is_mutating_tool_block(fallback_tool):
                    response["content"] = [fallback_tool]
                    response["stop_reason"] = "tool_use"
                    return
                response["content"] = [
                    {
                        "type": "text",
                        "text": (
                            "Ainda nao apliquei nenhuma mudanca fisica nos arquivos. "
                            "Eu so li/inspecionei o projeto ate agora; para alterar de verdade preciso executar "
                            "Write, Edit, apply_patch ou um comando Bash que modifique arquivos."
                        ),
                    }
                ]
                response["stop_reason"] = "end_turn"
                return
        if self._should_force_claude_code_tool_choice(payload) and not _response_has_tool_use(response):
            response_text = self._response_text(response)
            file_write_tools = self._textual_file_write_tool_uses(response_text, payload)
            if file_write_tools:
                response["content"] = file_write_tools
                response["stop_reason"] = "tool_use"
                return
            fallback_tool = self._fallback_tool_use_for_required_action(payload)
            if fallback_tool:
                response["content"] = [fallback_tool]
                response["stop_reason"] = "tool_use"
                return
            raise OpenRouterError(
                "VPS model ignored required Claude Code tool call.",
                status_code=502,
            )

    def _physical_change_status_text(self, payload: dict[str, Any]) -> str:
        if self._payload_has_successful_mutating_tool_use(payload):
            result_text = self._latest_tool_result_text(payload)
            if result_text:
                result_text = self._truncate_text_end(result_text, 1200)
                return (
                    "Sim, houve uma alteracao fisica confirmada por ferramenta de escrita/comando. "
                    "Ultimo resultado:\n\n"
                    f"```text\n{result_text}\n```"
                )
            return "Sim, houve uma alteracao fisica confirmada por ferramenta de escrita/comando."
        return (
            "Nao. Ate agora nao houve alteracao fisica confirmada nos arquivos. "
            "A conversa mostra leitura/inspecao, mas nenhum Write, Edit, apply_patch ou comando Bash mutante "
            "bem-sucedido."
        )

    def _response_text(self, response: dict[str, Any]) -> str:
        parts: list[str] = []
        for block in response.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()

    def _is_generic_help_response(self, text: str) -> bool:
        compact = " ".join(str(text or "").strip().lower().split())
        if not compact:
            return False
        ascii_compact = _strip_accents(compact)
        generic_phrases = (
            "como posso ajudar",
            "como posso te ajudar",
            "em que posso ajudar",
            "em que posso te ajudar",
            "em que posso ajuda-lo",
            "em que posso ajuda-la",
            "posso ajudar",
            "posso ajuda-lo",
            "posso ajuda-la",
            "precisa de mais alguma coisa",
            "what can i help",
            "how can i help",
            "how may i help",
            "please provide the specific task",
            "please provide the specific question",
            "provide the specific task",
            "provide the task or question",
            "forneca mais detalhes",
            "forneça mais detalhes",
            "aspectos especificos",
            "aspectos específicos",
            "detalhes sobre os aspectos",
            "nao consigo acessar ou analisar diretamente seu projeto",
            "não consigo acessar ou analisar diretamente seu projeto",
            "nao tenho acesso aos arquivos locais",
            "não tenho acesso aos arquivos locais",
            "arquivos locais do seu computador",
            "fornecer mais informacoes sobre seu projeto",
            "fornecer mais informações sobre seu projeto",
            "you'd like assistance with",
            "you would like assistance with",
            "use the appropriate tools to help",
            "i do not have access to your local files",
            "i don't have access to your local files",
            "i cannot access your local files",
            "i can't access your local files",
            "i cannot directly access your project",
            "i can't directly access your project",
        )
        if any(phrase in ascii_compact for phrase in generic_phrases):
            return True
        return compact in {
            "entendi.",
            "entendido.",
            "claro.",
            "ok.",
            "certo.",
        }

    def _looks_like_unneeded_detail_request(self, text: str) -> bool:
        compact = _strip_accents(" ".join(str(text or "").strip().lower().split()))
        if not compact:
            return False
        markers = (
            "forneca mais detalhes",
            "aspectos especificos",
            "detalhes sobre os aspectos",
            "nao consigo acessar ou analisar diretamente seu projeto",
            "nao tenho acesso aos arquivos locais",
            "arquivos locais do seu computador",
            "fornecer mais informacoes sobre seu projeto",
            "please provide more details",
            "please provide the specific task",
            "provide the specific task",
            "provide the task or question",
            "do not have access to your local files",
            "don't have access to your local files",
            "cannot access your local files",
            "can't access your local files",
            "cannot directly access your project",
            "can't directly access your project",
        )
        return any(marker in compact for marker in markers)

    def _looks_like_non_executing_action_response(self, text: str) -> bool:
        compact = _strip_accents(" ".join(str(text or "").strip().lower().split()))
        if not compact:
            return False
        promise_markers = (
            "estou pronto",
            "pronto para ajudar",
            "i will follow",
            "i will get started",
            "i will start",
            "i'll follow",
            "i'll get started",
            "i'll start",
            "i'll use the appropriate tools",
            "ill follow",
            "ill get started",
            "ill start",
            "ill use the appropriate tools",
            "let's get started",
            "lets get started",
            "understood! i'll",
            "understood, i'll",
            "understood! i will",
            "understood, i will",
            "vou ajudar",
            "vou auxiliar",
            "vou comecar",
            "vou continuar",
            "vou prosseguir",
            "vou responder",
            "vou seguir",
            "vou usar",
            "irei ajudar",
            "irei seguir",
            "posso seguir",
        )
        if any(marker in compact for marker in promise_markers):
            return True
        starts = (
            "entendi, vou ",
            "entendido, vou ",
            "entendi! vou ",
            "entendido! vou ",
            "understood, i ",
            "understood! i ",
        )
        return compact.startswith(starts)

    def _looks_like_completed_file_change_response(self, text: str) -> bool:
        compact = _strip_accents(" ".join(str(text or "").strip().lower().split()))
        if not compact:
            return False
        completion_markers = (
            "alterei",
            "apliquei",
            "atualizei",
            "corrigi",
            "criei",
            "editei",
            "fiz",
            "implementei",
            "modifiquei",
            "salvei",
            "subi",
            "foi criado",
            "foi alterado",
            "foi atualizado",
            "esta pronto",
            "tudo certo",
            "done",
            "created",
            "updated",
            "changed",
            "fixed",
            "implemented",
            "saved",
        )
        future_markers = (
            "vou ",
            "irei ",
            "posso ",
            "preciso ",
            "devo ",
            "seria ",
            "recomendo ",
        )
        return any(marker in compact for marker in completion_markers) and not any(
            marker in compact for marker in future_markers
        )

    def _latest_tool_result_text(self, payload: dict[str, Any]) -> str:
        for message in reversed(payload.get("messages") or []):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, dict):
                content = [content]
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return self._content_to_text(block.get("content")).strip()
        return ""

    def _fallback_summary_from_latest_tool_result(self, payload: dict[str, Any]) -> str:
        result_text = self._latest_tool_result_text(payload)
        if not result_text:
            return "Usei a ferramenta, mas ela não retornou conteúdo útil para resumir."
        result_text = self._truncate_text_end(result_text, 1800)
        return (
            "Usei a ferramenta e obtive este resultado. Vou seguir a partir dele se você mandar a próxima ordem:\n\n"
            f"```text\n{result_text}\n```"
        )

    def _fallback_tool_use_for_required_action(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        tool_names = self._available_tool_names(payload)
        if not tool_names:
            return None
        previous_fallbacks = self._previous_gateway_tool_fallbacks(payload)

        def first_available(*names: str) -> str | None:
            wanted = {name.lower() for name in names}
            for tool_name in tool_names:
                if tool_name.lower() in wanted:
                    return tool_name
            return None

        if self._latest_tool_result_needs_worktree(payload):
            if tool_name := first_available("EnterWorktree"):
                return {
                    "type": "tool_use",
                    "id": "call_gateway_worktree_0",
                    "name": tool_name,
                    "input": {"name": self._fallback_worktree_name(payload)},
                }
        requested_path = self._requested_file_path(payload)
        if requested_path:
            if tool_name := first_available("Read", "read_file"):
                candidate = {
                    "type": "tool_use",
                    "id": "call_gateway_read_0",
                    "name": tool_name,
                    "input": self._read_tool_input(tool_name, requested_path),
                }
                if not self._has_previous_fallback(previous_fallbacks, candidate):
                    return candidate
        if tool_name := first_available("LS", "list_files"):
            candidate = {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {"path": "."},
            }
            if not self._has_previous_fallback(previous_fallbacks, candidate):
                return candidate
        if tool_name := first_available("Glob"):
            candidate = {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {"pattern": "**/*"},
            }
            if not self._has_previous_fallback(previous_fallbacks, candidate):
                return candidate
        if tool_name := first_available("Bash", "run_command"):
            command_key = "command" if tool_name.lower() == "bash" else "cmd"
            candidate = {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": {
                    command_key: "pwd && find . -maxdepth 2 -type f | head -80",
                    "description": "Inspect workspace files before editing",
                },
            }
            if not self._has_previous_fallback(previous_fallbacks, candidate):
                return candidate
        if tool_name := first_available("read_file", "Read"):
            candidate = {
                "type": "tool_use",
                "id": "call_gateway_inspect_0",
                "name": tool_name,
                "input": self._read_tool_input(tool_name, "README.md"),
            }
            if not self._has_previous_fallback(previous_fallbacks, candidate):
                return candidate
        return None

    def _textual_file_write_tool_uses(self, text: str, payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not payload or not self._is_file_change_request(payload):
            return []
        tool_name = self._preferred_write_tool_name(payload)
        if not tool_name:
            return []
        files = _file_contents_from_text(text)
        if not files:
            return []
        blocks: list[dict[str, Any]] = []
        for index, (path, content) in enumerate(files):
            tool_input = (
                {"path": path, "content": content}
                if tool_name.lower() == "write_file"
                else {"file_path": path, "content": content}
            )
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"call_gateway_write_{index}",
                    "name": tool_name,
                    "input": tool_input,
                }
            )
        return blocks

    def _preferred_write_tool_name(self, payload: dict[str, Any]) -> str:
        for wanted in ("Write", "write_file"):
            for tool_name in self._available_tool_names(payload):
                if tool_name.lower() == wanted.lower():
                    return tool_name
        return ""

    def _previous_gateway_tool_fallbacks(self, payload: dict[str, Any]) -> set[tuple[str, str]]:
        fallbacks: set[tuple[str, str]] = set()
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
                block_id = str(block.get("id") or "")
                if not block_id.startswith("call_gateway_"):
                    continue
                name = str(block.get("name") or "")
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                fallbacks.add((name.lower(), json.dumps(tool_input, sort_keys=True, ensure_ascii=True)))
        return fallbacks

    def _has_previous_fallback(self, previous: set[tuple[str, str]], candidate: dict[str, Any]) -> bool:
        tool_input = candidate.get("input") if isinstance(candidate.get("input"), dict) else {}
        key = (
            str(candidate.get("name") or "").lower(),
            json.dumps(tool_input, sort_keys=True, ensure_ascii=True),
        )
        return key in previous

    def _read_tool_input(self, tool_name: str, path: str) -> dict[str, str]:
        if tool_name.strip().lower() == "read_file":
            return {"path": path}
        return {"file_path": path}

    def _requested_file_path(self, payload: dict[str, Any]) -> str:
        text = self._task_request_text(payload)
        if not text:
            return ""
        patterns = (
            r"(/Users/[^\s`'\"<>]+)",
            r"((?:\.{1,2}/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9_+-]+)",
            r"(/[A-Za-z0-9._~/-]+\.[A-Za-z0-9_+-]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).rstrip(".,;:)")
        return ""

    def _latest_tool_result_needs_worktree(self, payload: dict[str, Any]) -> bool:
        error_text = self._latest_tool_result_error_text(payload).lower()
        return "enterworktree" in error_text or "hasn't isolated its changes" in error_text

    def _fallback_worktree_name(self, payload: dict[str, Any]) -> str:
        text = self._task_request_text(payload).lower()
        if "calculadora" in text or "calculator" in text:
            return "calculadora-worktree"
        if "site" in text:
            return "site-worktree"
        return "claude-code-worktree"

    def _response_has_valid_worktree_tool_use(self, response: dict[str, Any]) -> bool:
        for block in response.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if str(block.get("name") or "").strip().lower() != "enterworktree":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                return False
            return any(str(tool_input.get(key) or "").strip() for key in ("name", "path"))
        return False

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
        timeout = self._stream_timeout(payload)
        attempts = [outgoing]
        if outgoing.get("tool_choice") == "required":
            attempts.append(self._without_required_tool_choice(outgoing))
        try:
            for attempt_index, attempt in enumerate(attempts):
                async with self._client.stream(
                    "POST",
                    self._url("/v1/chat/completions", target),
                    headers=self._headers(target),
                    json=attempt,
                    timeout=timeout,
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        body_text = body.decode("utf-8", "replace")
                        if (
                            attempt_index == 0
                            and len(attempts) > 1
                            and self._should_retry_without_required_tool_choice(
                                response.status_code,
                                body_text,
                                attempt,
                            )
                        ):
                            continue
                        if response.status_code == 404:
                            async for chunk in _anthropic_stream_error_message(
                                target.model_id,
                                "Backend de IA nao encontrado. Confira o pod RunPod ativo e a URL VPS_MODEL_BASE_URL.",
                            ):
                                yield chunk
                            return
                        raise OpenRouterError(body_text, response.status_code)
                    async for chunk in self._openai_sse_to_anthropic(
                        response.aiter_bytes(),
                        model=model,
                        require_tool_call=self._should_force_tool_choice(payload),
                        payload=payload,
                    ):
                        yield chunk
                    return
        except OpenRouterError:
            raise
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"VPS model stream failed: {exc}", status_code=502) from exc

    def _should_retry_without_required_tool_choice(
        self,
        status_code: int,
        body_text: str,
        outgoing: dict[str, Any],
    ) -> bool:
        if int(status_code or 0) != 400 or outgoing.get("tool_choice") != "required":
            return False
        text = str(body_text or "").lower()
        if not text:
            return True
        retry_terms = (
            "tool_choice",
            "tool choice",
            "required",
            "auto tool",
            "invalid request",
            "bad request",
            "extra inputs are not permitted",
            "not supported",
            "unsupported",
        )
        return any(term in text for term in retry_terms)

    def _without_required_tool_choice(self, outgoing: dict[str, Any]) -> dict[str, Any]:
        retried = deepcopy(outgoing)
        if retried.get("tool_choice") == "required":
            retried.pop("tool_choice", None)
        return retried

    def _request_timeout(self, payload: dict[str, Any]) -> httpx.Timeout:
        seconds = self._timeout_seconds_for_payload(payload)
        return httpx.Timeout(seconds)

    def _stream_timeout(self, payload: dict[str, Any]) -> httpx.Timeout:
        seconds = self._timeout_seconds_for_payload(payload)
        return httpx.Timeout(
            connect=min(10.0, seconds),
            read=seconds,
            write=min(30.0, seconds),
            pool=min(30.0, seconds),
        )

    def _timeout_seconds_for_payload(self, payload: dict[str, Any]) -> float:
        default_timeout = max(1.0, float(self.settings.vps_model_timeout_seconds or 55.0))
        if not (self._is_claude_code_client(payload) or payload.get("tools") or payload.get("tool_choice")):
            return default_timeout
        code_timeout = float(getattr(self.settings, "vps_code_timeout_seconds", 8.0) or 8.0)
        return max(1.0, min(default_timeout, code_timeout))

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
        post_tool_text_buffer = "" if payload and self._payload_has_tool_result(payload) else None
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
                elif text_delta and post_tool_text_buffer is not None and not tool_state.has_tool:
                    post_tool_text_buffer += text_delta
                elif text_delta:
                    for outgoing in state.feed(text_delta):
                        yield outgoing
                if tool_calls:
                    textual_tool_buffer = "" if textual_tool_buffer is not None else None
                    post_tool_text_buffer = "" if post_tool_text_buffer is not None else None
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
            elif text_delta and post_tool_text_buffer is not None and not tool_state.has_tool:
                post_tool_text_buffer += text_delta
            elif text_delta:
                for outgoing in state.feed(text_delta):
                    yield outgoing
            if tool_calls:
                textual_tool_buffer = "" if textual_tool_buffer is not None else None
                post_tool_text_buffer = "" if post_tool_text_buffer is not None else None
                for outgoing in tool_state.feed(tool_calls):
                    yield outgoing

        if textual_tool_buffer and not tool_state.has_tool:
            textual_tool_calls = _textual_tool_calls_from_text(textual_tool_buffer)
            if textual_tool_calls:
                for outgoing in tool_state.feed(self._validated_textual_tool_calls(textual_tool_calls, payload)):
                    yield outgoing
            elif payload and (file_write_tools := self._textual_file_write_tool_uses(textual_tool_buffer, payload)):
                for index, tool_use in enumerate(file_write_tools):
                    for outgoing in tool_state.feed([_openai_tool_call_from_anthropic_tool_use(tool_use, index)]):
                        yield outgoing
            elif require_tool_call and payload:
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool:
                    for outgoing in tool_state.feed([_openai_tool_call_from_anthropic_tool_use(fallback_tool, 0)]):
                        yield outgoing
            else:
                for outgoing in state.feed(textual_tool_buffer):
                    yield outgoing

        if post_tool_text_buffer and not tool_state.has_tool:
            textual_tool_calls = _textual_tool_calls_from_text(post_tool_text_buffer)
            if textual_tool_calls:
                for outgoing in tool_state.feed(self._validated_textual_tool_calls(textual_tool_calls, payload)):
                    yield outgoing
            elif payload and (file_write_tools := self._textual_file_write_tool_uses(post_tool_text_buffer, payload)):
                for index, tool_use in enumerate(file_write_tools):
                    for outgoing in tool_state.feed([_openai_tool_call_from_anthropic_tool_use(tool_use, index)]):
                        yield outgoing
            elif payload and self._is_change_status_question(payload):
                post_tool_text_buffer = self._physical_change_status_text(payload)
            elif (
                payload
                and self._looks_like_unneeded_detail_request(post_tool_text_buffer)
                and self._is_tool_action_request(payload, aggressive=False)
            ):
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool:
                    for outgoing in tool_state.feed([_openai_tool_call_from_anthropic_tool_use(fallback_tool, 0)]):
                        yield outgoing
            elif self._is_generic_help_response(post_tool_text_buffer) and payload:
                post_tool_text_buffer = self._fallback_summary_from_latest_tool_result(payload)
            elif (
                payload
                and self._is_tool_action_request(payload, aggressive=False)
                and self._looks_like_non_executing_action_response(post_tool_text_buffer)
            ):
                fallback_tool = self._fallback_tool_use_for_required_action(payload)
                if fallback_tool:
                    for outgoing in tool_state.feed([_openai_tool_call_from_anthropic_tool_use(fallback_tool, 0)]):
                        yield outgoing
            elif (
                payload
                and self._is_file_change_request(payload)
                and not self._payload_has_successful_mutating_tool_use(payload)
                and self._looks_like_completed_file_change_response(post_tool_text_buffer)
            ):
                post_tool_text_buffer = (
                    "Ainda nao apliquei nenhuma mudanca fisica nos arquivos. "
                    "Eu so li/inspecionei o projeto ate agora; para alterar de verdade preciso executar "
                    "Write, Edit, apply_patch ou um comando Bash que modifique arquivos."
                )
            if not tool_state.has_tool:
                for outgoing in state.feed(post_tool_text_buffer):
                    yield outgoing

        for outgoing in state.finish():
            yield outgoing
        for outgoing in tool_state.finish():
            yield outgoing
        if tool_state.has_tool:
            stop_reason = "tool_use"
        elif state.is_empty:
            stop_reason = "end_turn"
            if payload and self._payload_has_tool_result(payload):
                fallback_text = self._fallback_summary_from_latest_tool_result(payload)
            else:
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

    def _validated_textual_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        payload: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not payload or not self._latest_tool_result_needs_worktree(payload):
            return tool_calls
        fallback_tool = self._fallback_tool_use_for_required_action(payload)
        if not fallback_tool:
            return tool_calls
        fallback_call = _openai_tool_call_from_anthropic_tool_use(fallback_tool, 0)
        validated: list[dict[str, Any]] = []
        for index, tool_call in enumerate(tool_calls):
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            name = str((function or {}).get("name") or "")
            arguments = _json_object_from_string((function or {}).get("arguments")) if isinstance(function, dict) else {}
            if name.strip().lower() == "enterworktree" and not any(
                str(arguments.get(key) or "").strip() for key in ("name", "path")
            ):
                replacement = deepcopy(fallback_call)
                replacement["index"] = index
                validated.append(replacement)
            else:
                validated.append(tool_call)
        return validated

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


def _strip_accents(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", str(text or ""))
        if not unicodedata.combining(char)
    )


_FILE_LABEL_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:`{1,3}|\*\*)?([A-Za-z0-9_.\-/]+\.(?:css|html|js|jsx|json|md|py|sh|toml|ts|tsx|txt|yaml|yml))(?:`{1,3}|\*\*)?\s*:?\s*$"
)
_FENCED_CODE_RE = re.compile(r"\A\s*```[A-Za-z0-9_-]*\s*\n(?P<code>.*?)\n```\s*\Z", re.DOTALL)


def _file_contents_from_text(text: str) -> list[tuple[str, str]]:
    raw = str(text or "")
    if not raw.strip():
        return []
    matches = list(_FILE_LABEL_RE.finditer(raw))
    if not matches:
        return []

    files: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        path = match.group(1).strip().strip("`*")
        if not path or path.startswith(("/", "~")) or ".." in Path(path).parts:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        content = raw[start:end].strip()
        fenced = _FENCED_CODE_RE.match(content)
        if fenced:
            content = fenced.group("code")
        else:
            fenced_blocks = list(re.finditer(r"```[A-Za-z0-9_-]*\s*\n(.*?)\n```", content, re.DOTALL))
            if fenced_blocks:
                content = fenced_blocks[0].group(1)
        content = content.strip("\n")
        if not content or path in seen:
            continue
        if not _looks_like_file_body(path, content):
            continue
        seen.add(path)
        files.append((path, content + ("\n" if not content.endswith("\n") else "")))
    return files[:5]


def _looks_like_file_body(path: str, content: str) -> bool:
    suffix = Path(path).suffix.lower()
    stripped = content.lstrip()
    if suffix == ".html":
        return "<" in content and ">" in content
    if suffix == ".css":
        return "{" in content and "}" in content
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return any(marker in content for marker in ("function ", "const ", "let ", "import ", "export ", "=>", "document."))
    if suffix == ".py":
        return any(marker in content for marker in ("def ", "import ", "print(", "if __name__", "class "))
    if suffix in {".json"}:
        return stripped.startswith(("{", "["))
    if suffix in {".yml", ".yaml", ".toml", ".sh"}:
        return len(content.splitlines()) >= 1
    return len(content.splitlines()) >= 2 or len(content) >= 20


def _textual_tool_calls_from_text(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    bare_call = _json_tool_call_from_text(text)
    if bare_call:
        return [bare_call]

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


def _json_tool_call_from_text(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates: list[str] = []
    for match in reversed(list(re.finditer(r"\{", raw))):
        object_end = _balanced_json_object_end(raw, match.start())
        if object_end >= 0:
            candidates.append(raw[match.start() : object_end + 1])
    for candidate in candidates:
        candidate = candidate.strip()
        call = _tool_call_from_textual_payload(_json_object_from_string(candidate), 0)
        if call:
            return call
    return None


def _normalize_tool_name(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    compact = name.lower().replace("_", "").replace("-", "")
    aliases = {
        "enterworktre": "EnterWorktree",
        "enterworktree": "EnterWorktree",
    }
    return aliases.get(compact, name)


def _tool_call_from_textual_payload(payload: dict[str, Any], index: int) -> dict[str, Any] | None:
    name = _normalize_tool_name(payload.get("name") or payload.get("tool") or payload.get("tool_name") or "")
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


async def _anthropic_stream_error_message(model: str, text: str) -> AsyncIterator[bytes]:
    yield _anthropic_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_backend_error",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _anthropic_sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    yield _anthropic_sse(
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
    )
    yield _anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _anthropic_sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 0},
        },
    )
    yield _anthropic_sse("message_stop", {"type": "message_stop"})


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
