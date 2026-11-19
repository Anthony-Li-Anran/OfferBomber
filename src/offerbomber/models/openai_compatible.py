"""Optional generic OpenAI-compatible provider (task book B5, disabled by default).

This adapter is NOT used unless explicitly configured. It never claims host
model access; it targets a user-supplied endpoint only.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from pydantic import BaseModel

from .base import (
    HealthStatus,
    ModelError,
    ModelErrorKind,
    ModelProvider,
    TaskType,
    extract_json_block,
)


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.base_url = (base_url or os.getenv("OFFERBOMBER_MODEL_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("OFFERBOMBER_MODEL_API_KEY", "")
        self.model = model or os.getenv("OFFERBOMBER_MODEL_NAME", "")

    async def health_check(self) -> HealthStatus:
        missing = [
            name
            for name, value in (
                ("base_url", self.base_url),
                ("api_key", self.api_key),
                ("model", self.model),
            )
            if not value
        ]
        if missing:
            return HealthStatus(False, self.name, f"missing config: {', '.join(missing)}")
        return HealthStatus(
            available=True,
            provider=self.name,
            detail="configured (not verified); user-supplied endpoint",
        )

    async def _generate(self, task: TaskType, messages: list[dict[str, str]], schema: type[BaseModel]) -> Any:
        if not self.base_url or not self.api_key or not self.model:
            raise ModelError(
                ModelErrorKind.UNAVAILABLE,
                "OpenAI-compatible provider not configured",
                recoverable=False,
            )
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Return only one JSON object. No prose."},
                *messages,
                {
                    "role": "user",
                    "content": "Schema: " + json.dumps(schema.model_json_schema(), ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ModelError(ModelErrorKind.TRANSPORT, f"model endpoint error: {exc}") from exc
        if response.status_code == 401:
            raise ModelError(ModelErrorKind.AUTH, "model endpoint rejected key", recoverable=False)
        if response.status_code == 429:
            raise ModelError(ModelErrorKind.RATE_LIMIT, "model endpoint rate limited")
        try:
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ModelError(ModelErrorKind.PROVIDER, f"unexpected model response: {exc}") from exc
        return json.loads(extract_json_block(content))
