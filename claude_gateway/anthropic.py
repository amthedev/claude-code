from __future__ import annotations

import time
import uuid
from copy import deepcopy
from typing import Any


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
    return "\n".join(text).strip()


def build_text_message(model: str, text: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
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
    return copied
