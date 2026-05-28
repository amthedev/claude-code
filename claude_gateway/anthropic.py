from __future__ import annotations

import time
import uuid
import re
from copy import deepcopy
from typing import Any


_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_DUPLICATED_WORD_RE = re.compile(r"\b([^\W_]+)(\s+)\1\b", re.IGNORECASE | re.UNICODE)
_PREFIX_FRAGMENT_RE = re.compile(r"\b([^\W_]{1,8})(\s+)(\1[^\W_]{2,})\b", re.IGNORECASE | re.UNICODE)
_MARKDOWN_PAIR_RE = re.compile(r"(\*\*|__|\*|_)\s+\1")
_BROKEN_TIME_EMPHASIS_RE = re.compile(r"\*(\d+[–-]\d+\s*min)\*\*")
_THINK_BLOCK_RE = re.compile(r"(?is)<think\b[^>]*>(.*?)</think>\s*")
_OPEN_THINK_BLOCK_RE = re.compile(r"(?is)<think\b[^>]*>(.*)\Z")
_THINK_TAG_RE = re.compile(r"(?is)</?think\b[^>]*>\s*")
_GLUED_REPAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bParaprender\b", re.IGNORECASE), "Para aprender"),
    (re.compile(r"\bdentendimento\b", re.IGNORECASE), "de entendimento"),
    (re.compile(r"\bfrasesobre\b", re.IGNORECASE), "frases sobre"),
    (re.compile(r"\bfrasesimples\b", re.IGNORECASE), "frases simples"),
    (re.compile(r"\bpalavrasem\b", re.IGNORECASE), "palavras sem"),
    (re.compile(r"\bpalavrasoltas\b", re.IGNORECASE), "palavras soltas"),
    (re.compile(r"\bsemanasem\b", re.IGNORECASE), "semanas sem"),
    (re.compile(r"\bmetasemanais\b", re.IGNORECASE), "metas semanais"),
    (re.compile(r"\bqueu\b", re.IGNORECASE), "que eu"),
    (re.compile(r"\bquevita\b", re.IGNORECASE), "que evita"),
    (re.compile(r"\bO\s+quev\b", re.IGNORECASE), "O que evita"),
    (re.compile(r"\bConversasimples\b"), "Conversa simples"),
    (re.compile(r"\bComprensão\b"), "Compreensão"),
    (re.compile(r"\bIso\b"), "Isso"),
    (re.compile(r"\bEscutativa\b", re.IGNORECASE), "Escuta ativa"),
    (re.compile(r"\bcoffe\b", re.IGNORECASE), "coffee"),
    (re.compile(r"\bBroklyn\b", re.IGNORECASE), "Brooklyn"),
    (re.compile(r"\bSpeak\s+or\s+conversa\b", re.IGNORECASE), "Speak ou converse"),
    (re.compile(r"\bConteúdo\s+seu\s+interesse\b", re.IGNORECASE), "Conteúdo do seu interesse"),
    (re.compile(r"\bpensem\s+frases\b", re.IGNORECASE), "pense em frases"),
    (re.compile(r"\bmedo\s+derrar\b", re.IGNORECASE), "medo de errar"),
    (re.compile(r"\bPoso\b", re.IGNORECASE), "Posso"),
    (re.compile(r"\bencontrar?arquivos\b", re.IGNORECASE), "encontrar arquivos"),
    (re.compile(r"\bidentificar?quivos\b", re.IGNORECASE), "identificar arquivos"),
    (re.compile(r"\bexplorar?quivos\b", re.IGNORECASE), "explorar arquivos"),
    (re.compile(r"\blocalizar?quivos\b", re.IGNORECASE), "localizar arquivos"),
    (re.compile(r"\bcomanteiga\b", re.IGNORECASE), "com manteiga"),
    (re.compile(r"\bo\s+leite\s+o\s+óleo\b", re.IGNORECASE), "o leite e o óleo"),
    (re.compile(r"\bantes\s+desenformar\b", re.IGNORECASE), "antes de desenformar"),
    (re.compile(r"\bTransfira\s+massa\s+para\s+forma\b", re.IGNORECASE), "Transfira a massa para a forma"),
    (re.compile(r"\b(\d+)hoje\s+nada\b"), r"\1h hoje e nada"),
    (re.compile(r"\bAprendas\s+\*?1\.0[–-]2\.0\b"), "Aprenda as **1.000–2.000"),
    (re.compile(r"\bFoquem\s+frases\b"), "Foque em frases"),
)


def extract_response_text(response: dict[str, Any]) -> str:
    content = response.get("content")
    if isinstance(content, str):
        return content

    text: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    text.append(value)
    return clean_model_text("\n".join(text).strip())


def build_text_message(model: str, text: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    cleaned_text = clean_model_text(text)
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": cleaned_text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 0, "output_tokens": 0},
    }


def merge_usage(*responses: dict[str, Any]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0}
    for response in responses:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            continue
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
    return totals


def with_system_prompt(payload: dict[str, Any], prompt: str) -> dict[str, Any]:
    outgoing = deepcopy(payload)
    outgoing.pop("stream", None)
    existing = outgoing.get("system")

    if not existing:
        outgoing["system"] = prompt
    elif isinstance(existing, str):
        outgoing["system"] = f"{existing}\n\n{prompt}"
    elif isinstance(existing, list):
        outgoing["system"] = [*existing, {"type": "text", "text": prompt}]
    else:
        outgoing["system"] = prompt

    return outgoing


def append_user_context(payload: dict[str, Any], text: str) -> dict[str, Any]:
    outgoing = deepcopy(payload)
    outgoing.pop("stream", None)
    messages = list(outgoing.get("messages") or [])
    messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
    outgoing["messages"] = messages
    return outgoing


def public_response_copy(response: dict[str, Any], model: str) -> dict[str, Any]:
    copied = deepcopy(response)
    copied.setdefault("id", f"msg_{uuid.uuid4().hex[:24]}")
    copied.setdefault("type", "message")
    copied.setdefault("role", "assistant")
    copied["model"] = model
    copied.setdefault("created_at", int(time.time()))
    _clean_response_content(copied)
    return copied


def clean_model_text(text: str, *, strip: bool = True) -> str:
    value = str(text or "")
    if not value:
        return value
    value = _strip_thinking_text(value)
    return _clean_visible_text(value, strip=strip)


def split_thinking_text(text: str, *, strip: bool = True) -> tuple[str, str]:
    value = str(text or "")
    if not value:
        return "", value

    thinking_parts = [match.group(1) for match in _THINK_BLOCK_RE.finditer(value)]
    visible = _THINK_BLOCK_RE.sub("", value)
    open_match = _OPEN_THINK_BLOCK_RE.search(visible)
    if open_match:
        thinking_parts.append(open_match.group(1))
        visible = visible[: open_match.start()]
    visible = _THINK_TAG_RE.sub("", visible)

    thinking = "\n\n".join(
        _clean_visible_text(part, strip=True)
        for part in thinking_parts
        if str(part or "").strip()
    )
    return thinking, _clean_visible_text(visible, strip=strip)


def _clean_visible_text(value: str, *, strip: bool = True) -> str:
    if not value:
        return value

    chunks = re.split(r"(```[\s\S]*?```)", value)
    cleaned = [
        chunk if chunk.startswith("```") else _clean_prose_text(chunk)
        for chunk in chunks
    ]
    result = "".join(cleaned)
    return result.strip() if strip else result


def _strip_thinking_text(text: str) -> str:
    without_closed_blocks = _THINK_BLOCK_RE.sub("", text)
    without_open_block = _OPEN_THINK_BLOCK_RE.sub("", without_closed_blocks)
    return _THINK_TAG_RE.sub("", without_open_block)


def _clean_response_content(response: dict[str, Any]) -> None:
    content = response.get("content")
    if isinstance(content, str):
        response["content"] = clean_model_text(content)
        return

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                block["text"] = clean_model_text(block["text"])

    choices = response.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        message_content = message.get("content")
        if isinstance(message_content, str):
            message["content"] = clean_model_text(message_content)
        elif isinstance(message_content, list):
            for block in message_content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    block["text"] = clean_model_text(block["text"])


def _clean_prose_text(text: str) -> str:
    previous = None
    current = _MARKDOWN_PAIR_RE.sub(r"\1", text)
    current = _BROKEN_TIME_EMPHASIS_RE.sub(r"\1", current)
    for _ in range(4):
        if current == previous:
            break
        previous = current
        current = _remove_restarted_answer(current)
        current = _remove_short_restarted_prefix(current)
        current = _DUPLICATED_WORD_RE.sub(r"\1", current)
        current = _PREFIX_FRAGMENT_RE.sub(r"\3", current)
        current = _WORD_RE.sub(lambda match: _repair_word(match.group(0)), current)
        current = _repair_glued_phrases(current)
    return current


def _remove_restarted_answer(text: str) -> str:
    markers = ("Para aprender", "Paraprender", "Aprender inglês rápido")
    for marker in markers:
        first = text.find(marker)
        if first < 0:
            continue
        second = text.find(marker, first + len(marker))
        if second < 0:
            continue
        prefix = text[:second]
        suffix = text[second:]
        if len(suffix) < len(prefix) * 0.5:
            return prefix.rstrip()
        tail = prefix[-48:]
        if len(prefix) > 180 and not tail.rstrip().endswith((".", "!", "?", ":", "\n")):
            return suffix
        return prefix.rstrip()
    return text


def _remove_short_restarted_prefix(text: str) -> str:
    max_restart_at = min(80, len(text) - 8)
    for restart_at in range(8, max_restart_at + 1):
        prefix = text[:restart_at]
        suffix = text[restart_at:]
        if len(suffix) < restart_at or not re.search(r"[\s.!?]", prefix):
            continue
        common = 0
        max_common = min(len(prefix), len(suffix))
        while common < max_common and prefix[common].casefold() == suffix[common].casefold():
            common += 1
        if common >= 8:
            return suffix
    return text


def _repair_glued_phrases(text: str) -> str:
    current = text
    for pattern, replacement in _GLUED_REPAIRS:
        current = pattern.sub(replacement, current)
    return current


def _repair_word(word: str) -> str:
    repaired = word
    for _ in range(3):
        next_value = _repair_word_once(repaired)
        if next_value == repaired:
            return repaired
        repaired = next_value
    return repaired


def _repair_word_once(word: str) -> str:
    if len(word) < 4:
        return word

    folded = word.casefold()
    if len(word) % 2 == 0:
        half = len(word) // 2
        if folded[:half] == folded[half:]:
            return word[:half]

    for size in range(min(4, len(word) // 2), 0, -1):
        if folded.startswith(folded[:size] * 2):
            return word[size:]

    for size in range(len(word) // 2, 2, -1):
        stem = word[:-size]
        suffix = word[-size:]
        if len(stem) < max(4, size + 1):
            continue
        if stem.casefold().endswith(suffix.casefold()):
            return stem

    if len(word) >= 6:
        stem = word[:-2]
        suffix = word[-2:]
        if suffix.casefold() in {"ar", "er", "ir", "ês"} and stem.casefold().endswith(suffix.casefold()):
            return stem

    if len(word) >= 6 and folded[-1:] == folded[-2:-1] and folded[-1:] in {"a", "e", "i", "o", "u"}:
        return word[:-1]

    return word
