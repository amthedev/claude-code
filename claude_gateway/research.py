from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .auth import AuthContext
from .config import Settings
from .routing import extract_prompt_text


WEB_SEARCH_POLICIES = {"auto", "required", "off"}

REQUIRED_PATTERNS = (
    r"\b(pesquise|pesquisar|busque|buscar|procure|googl[ea]|na web|internet|online)\b",
    r"\b(atual|atuais|hoje|agora|recente|recentes|ultimo|ultima|último|última|latest|today|current)\b",
    r"\b(not[ií]cia|pre[cç]o|cotação|cotacao|lei|jurisprud[eê]ncia|vers[aã]o|release|changelog)\b",
    r"\b(clima|tempo agora|esporte|placar|agenda|cronograma|calend[aá]rio|CEO|presidente)\b",
)

OFF_PATTERNS = (
    r"\b(n[aã]o pesquise|sem pesquisar|n[aã]o use a web|sem web|offline|sem internet)\b",
)


@dataclass(frozen=True, slots=True)
class WebSearchDecision:
    policy: str
    enabled: bool
    should_search: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WebSource:
    title: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url}


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    summary: str
    sources: tuple[WebSource, ...]
    searched: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "sources": [source.to_dict() for source in self.sources],
            "searched": self.searched,
        }


class WebSearchError(RuntimeError):
    pass


class WebSearchClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def responses_url(self) -> str:
        return f"{self.settings.openai_base_url.rstrip('/')}/responses"

    async def search(self, query: str, *, required: bool = True) -> WebSearchResult:
        if not self.settings.openai_api_key:
            raise WebSearchError("OPENAI_API_KEY is not configured.")

        body: dict[str, Any] = {
            "model": self.settings.web_search_model,
            "instructions": WEB_SEARCH_PROMPT,
            "input": query[:12000],
            "max_output_tokens": self.settings.web_search_max_output_tokens,
            "tools": [self._web_search_tool()],
            "tool_choice": "required" if required else "auto",
        }

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        timeout_seconds = max(0.05, float(self.settings.web_search_timeout_seconds or 8.0))
        timeout = httpx.Timeout(
            connect=min(5.0, timeout_seconds),
            read=timeout_seconds,
            write=min(10.0, timeout_seconds),
            pool=min(10.0, timeout_seconds),
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.responses_url, headers=headers, json=body)

        if response.status_code >= 400:
            raise WebSearchError(response.text)

        return parse_web_search_response(response.json())

    def _web_search_tool(self) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": "web_search",
            "search_context_size": self.settings.web_search_context_size,
        }
        filters: dict[str, Any] = {}
        if self.settings.web_search_allowed_domains:
            filters["allowed_domains"] = list(self.settings.web_search_allowed_domains[:100])
        if self.settings.web_search_blocked_domains:
            filters["blocked_domains"] = list(self.settings.web_search_blocked_domains[:100])
        if filters:
            tool["filters"] = filters
        return tool


WEB_SEARCH_PROMPT = """You are a concise research pass for a coding/chat gateway.
Use web search only to collect current, source-backed context for the user request.
Return a compact answer in Brazilian Portuguese when the user writes Portuguese.
Favor official docs, primary sources, vendor pages, government pages, and reputable news.
Do not mention hidden routing. Keep citations available through the response annotations."""


def normalize_web_search_policy(value: Any) -> str:
    if isinstance(value, bool):
        return "required" if value else "off"
    policy = str(value or "auto").strip().lower()
    if policy in {"on", "true", "yes", "ligada", "ativo", "required", "force"}:
        return "required"
    if policy in {"off", "false", "no", "desligada", "none", "disable"}:
        return "off"
    return policy if policy in WEB_SEARCH_POLICIES else "auto"


def decide_web_search(payload: dict[str, Any], settings: Settings, auth: AuthContext) -> WebSearchDecision:
    policy = normalize_web_search_policy(payload.get("__gateway_web_search_policy", "auto"))
    prompt_text = extract_prompt_text(payload)
    normalized = _normalize(prompt_text)

    if policy == "off" or _matches_any(normalized, OFF_PATTERNS):
        return WebSearchDecision(policy="off", enabled=settings.enable_web_search, should_search=False, reason="disabled")

    enabled = bool(settings.enable_web_search and (not auth.customer or settings.web_search_for_customers))
    if policy == "required":
        return WebSearchDecision(policy=policy, enabled=enabled, should_search=True, reason="explicit")

    if _matches_any(normalized, REQUIRED_PATTERNS):
        return WebSearchDecision(policy=policy, enabled=enabled, should_search=True, reason="fresh_information")

    return WebSearchDecision(policy=policy, enabled=enabled, should_search=False, reason="stable_request")


def web_search_unavailable_context(decision: WebSearchDecision) -> str:
    return (
        "Internal web research status: web search was needed "
        f"({decision.reason}) but is not available/configured for this request. "
        "If the final answer depends on fresh facts, say you could not verify live sources and avoid guessing."
    )


def web_search_context(result: WebSearchResult) -> str:
    lines = [
        "Internal web research context. Use this silently to improve the answer.",
        "When relying on this current information, cite the sources as Markdown links in the final answer.",
        "",
        "[WEB SUMMARY]",
        result.summary,
    ]
    if result.sources:
        lines.extend(["", "[WEB SOURCES]"])
        for source in result.sources[:8]:
            title = source.title or source.url
            lines.append(f"- {title}: {source.url}")
    return "\n".join(lines)


def parse_web_search_response(response: dict[str, Any]) -> WebSearchResult:
    summary = _extract_output_text(response).strip()
    sources = _extract_sources(response)
    searched = _has_web_search_call(response)
    return WebSearchResult(summary=summary, sources=tuple(sources), searched=searched)


def _extract_output_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in _iter_output_items(response):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_sources(response: dict[str, Any]) -> list[WebSource]:
    sources: list[WebSource] = []
    seen: set[str] = set()

    def add(url: Any, title: Any = "") -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return
        if url in seen:
            return
        seen.add(url)
        sources.append(WebSource(title=str(title or url), url=url))

    for item in _iter_output_items(response):
        for source in _as_list(item.get("sources")):
            if isinstance(source, dict):
                add(source.get("url"), source.get("title"))
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            for annotation in _as_list(part.get("annotations")):
                if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                    add(annotation.get("url"), annotation.get("title"))

    for source in _as_list(response.get("sources")):
        if isinstance(source, dict):
            add(source.get("url"), source.get("title"))

    return sources


def _has_web_search_call(response: dict[str, Any]) -> bool:
    return any(item.get("type") == "web_search_call" for item in _iter_output_items(response))


def _iter_output_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    return [item for item in output if isinstance(item, dict)] if isinstance(output, list) else []


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _normalize(value: str) -> str:
    replacements = str.maketrans("áàãâéêíóôõúç", "aaaaeeiooouc")
    return str(value or "").lower().translate(replacements)
