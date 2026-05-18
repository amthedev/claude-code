from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class OpenAIHelperError(RuntimeError):
    pass


class OpenAIHelperClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def responses_url(self) -> str:
        return f"{self.settings.openai_base_url.rstrip('/')}/responses"

    async def generate_text(
        self,
        *,
        instructions: str,
        input_text: str,
        max_output_tokens: int | None = None,
    ) -> str:
        if not self.settings.openai_api_key:
            raise OpenAIHelperError("OPENAI_API_KEY is not configured.")

        body: dict[str, Any] = {
            "model": self.settings.openai_helper_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max_output_tokens or self.settings.openai_helper_max_output_tokens,
        }
        reasoning_effort = self.settings.openai_helper_reasoning_effort.strip().lower()
        if reasoning_effort and reasoning_effort not in {"default", "none"}:
            body["reasoning"] = {"effort": reasoning_effort}

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(self.responses_url, headers=headers, json=body)

        if response.status_code >= 400:
            raise OpenAIHelperError(response.text)

        return _extract_output_text(response.json())


def _extract_output_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)

    return "\n".join(chunks).strip()
