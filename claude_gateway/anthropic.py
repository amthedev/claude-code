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

    chunks = re.split(r"(```[\s\S]*?```)", value)
    cleaned = [
        chunk if chunk.startswith("```") else _clean_prose_text(chunk)
        for chunk in chunks
    ]
    result = "".join(cleaned)
    return result.strip() if strip else result


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
    for _ in range(4):
        if current == previous:
            break
        previous = current
        current = _DUPLICATED_WORD_RE.sub(r"\1", current)
        current = _PREFIX_FRAGMENT_RE.sub(r"\3", current)
        current = _WORD_RE.sub(lambda match: _repair_word(match.group(0)), current)
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

    for size in range(len(word) // 2, 1, -1):
        stem = word[:-size]
        suffix = word[-size:]
        if len(stem) < max(4, size + 1):
            continue
        if stem.casefold().endswith(suffix.casefold()):
            return stem

    if len(word) >= 6 and folded[-1:] == folded[-2:-1] and folded[-1:] in {"a", "e", "i", "o", "u"}:
        return word[:-1]

    return word
