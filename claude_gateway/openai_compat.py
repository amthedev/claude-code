from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .anthropic import extract_response_text


OPENAI_COMPAT_MODELS = (
    "claude-code-economy",
    "claude-code-pro",
    "claude-code-ultra",
    "claude-code-ui",
    "claude-code-auto",
)


def openai_models_response(models: tuple[str, ...] = OPENAI_COMPAT_MODELS) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "claude-gateway",
            }
            for model in models
        ],
    }


def responses_to_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    outgoing: dict[str, Any] = {
        "model": str(payload.get("model") or "claude-code-pro"),
        "max_tokens": int(payload.get("max_output_tokens") or payload.get("max_tokens") or 4096),
        "stream": bool(payload.get("stream")),
    }

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        outgoing["system"] = instructions

    messages = _responses_input_to_messages(payload.get("input"))
    if not messages:
        messages = [{"role": "user", "content": ""}]
    outgoing["messages"] = messages

    tools = _responses_tools_to_anthropic(payload.get("tools"))
    if tools:
        outgoing["tools"] = tools

    return outgoing


def chat_to_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    outgoing: dict[str, Any] = {
        "model": str(payload.get("model") or "claude-code-pro"),
        "max_tokens": int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 4096),
        "stream": bool(payload.get("stream")),
    }

    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content") or ""
        if role in {"system", "developer"}:
            system_parts.append(_content_to_text(content))
        elif role == "assistant":
            messages.append({"role": "assistant", "content": _content_to_anthropic_blocks(content)})
        elif role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": _content_to_text(content),
                        }
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": _content_to_anthropic_blocks(content)})

    if system_parts:
        outgoing["system"] = "\n\n".join(part for part in system_parts if part)
    outgoing["messages"] = messages or [{"role": "user", "content": ""}]

    tools = _chat_tools_to_anthropic(payload.get("tools"))
    if tools:
        outgoing["tools"] = tools

    return outgoing


def anthropic_to_response(response: dict[str, Any], request: dict[str, Any], model: str) -> dict[str, Any]:
    now = int(time.time())
    output_items = _anthropic_content_to_response_output(response)
    usage = _responses_usage(response.get("usage"))
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": now,
        "status": "completed",
        "completed_at": now,
        "error": None,
        "incomplete_details": None,
        "instructions": request.get("instructions"),
        "max_output_tokens": request.get("max_output_tokens"),
        "model": model,
        "output": output_items,
        "parallel_tool_calls": request.get("parallel_tool_calls", True),
        "previous_response_id": request.get("previous_response_id"),
        "reasoning": request.get("reasoning") or {"effort": None, "summary": None},
        "store": request.get("store", True),
        "temperature": request.get("temperature", 1.0),
        "text": request.get("text") or {"format": {"type": "text"}},
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": request.get("tools") or [],
        "top_p": request.get("top_p", 1.0),
        "truncation": request.get("truncation", "disabled"),
        "usage": usage,
        "user": request.get("user"),
        "metadata": request.get("metadata") or {},
    }


def anthropic_to_chat_completion(
    response: dict[str, Any],
    request: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    text = extract_response_text(response)
    tool_calls = _anthropic_content_to_chat_tool_calls(response)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": None if tool_calls else text,
        "refusal": None,
        "annotations": [],
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "service_tier": request.get("service_tier", "default"),
    }


def response_to_sse(response: dict[str, Any]) -> list[bytes]:
    item = response["output"][0] if response.get("output") else _empty_message_item()
    text = ""
    if item.get("type") == "message":
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = str(part.get("text") or "")
                break

    events = [
        ("response.created", {"type": "response.created", "response": {**response, "status": "in_progress", "output": [], "usage": None}}),
        ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
        (
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ),
    ]
    if text:
        events.append(
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            )
        )
    events.extend(
        [
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                },
            ),
            ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
            ("response.completed", {"type": "response.completed", "response": response}),
        ]
    )
    return [f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8") for event, data in events]


def chat_to_sse(completion: dict[str, Any]) -> list[bytes]:
    choice = completion["choices"][0]
    content = choice.get("message", {}).get("content") or ""
    chunk_id = completion["id"]
    model = completion["model"]
    created = completion["created"]
    chunks = [
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        },
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}],
        },
    ]
    return [f"data: {json.dumps(chunk)}\n\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n\n"]


def _responses_input_to_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]

    messages: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return messages

    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(item.get("call_id") or item.get("id") or ""),
                            "content": _content_to_text(item.get("output") or ""),
                        }
                    ],
                }
            )
            continue

        role = str(item.get("role") or "user")
        if role not in {"user", "assistant"}:
            role = "user"
        messages.append({"role": role, "content": _content_to_anthropic_blocks(item.get("content") or item.get("text") or "")})

    return messages


def _content_to_anthropic_blocks(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": str(part)})
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            blocks.append({"type": "text", "text": str(part.get("text") or "")})
        elif part_type == "tool_result":
            blocks.append(part)
    return blocks or ""


def _content_to_text(content: Any) -> str:
    converted = _content_to_anthropic_blocks(content)
    if isinstance(converted, str):
        return converted
    return "\n".join(str(block.get("text") or block.get("content") or "") for block in converted)


def _responses_tools_to_anthropic(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = str(tool.get("name") or "")
        if not name:
            continue
        tools.append(
            {
                "name": name,
                "description": str(tool.get("description") or ""),
                "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return tools


def _chat_tools_to_anthropic(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name:
            continue
        tools.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return tools


def _anthropic_content_to_response_output(response: dict[str, Any]) -> list[dict[str, Any]]:
    content = response.get("content")
    if not isinstance(content, list):
        return [_message_item([{"type": "output_text", "text": extract_response_text(response), "annotations": []}])]

    output: list[dict[str, Any]] = []
    text_parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append({"type": "output_text", "text": str(block.get("text") or ""), "annotations": []})
        elif block.get("type") == "tool_use":
            if text_parts:
                output.append(_message_item(text_parts))
                text_parts = []
            output.append(
                {
                    "type": "function_call",
                    "id": block.get("id") or f"fc_{uuid.uuid4().hex[:24]}",
                    "call_id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "name": block.get("name") or "",
                    "arguments": json.dumps(block.get("input") or {}),
                    "status": "completed",
                }
            )
    if text_parts or not output:
        output.append(_message_item(text_parts or [{"type": "output_text", "text": "", "annotations": []}]))
    return output


def _anthropic_content_to_chat_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    content = response.get("content")
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    return calls


def _message_item(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "status": "completed",
        "role": "assistant",
        "content": content,
    }


def _empty_message_item() -> dict[str, Any]:
    return _message_item([{"type": "output_text", "text": "", "annotations": []}])


def _responses_usage(usage: Any) -> dict[str, Any]:
    source = usage if isinstance(usage, dict) else {}
    input_tokens = int(source.get("input_tokens") or 0)
    output_tokens = int(source.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }
