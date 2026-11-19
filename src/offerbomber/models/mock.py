"""Deterministic Mock Provider for tests and offline demos.

A Mock result is NEVER a real model call. `real_live_verified` is always False.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from pydantic import BaseModel

from .base import (
    HealthStatus,
    ModelError,
    ModelErrorKind,
    ModelProvider,
    TaskType,
)


class MockProvider(ModelProvider):
    """Returns deterministic, schema-valid data derived from the prompt.

    Fault injection is explicit via constructor flags so tests can cover invalid
    JSON, timeouts, and provider errors without a network.
    """

    name = "mock"

    def __init__(
        self,
        *,
        responses: dict[str, Any] | None = None,
        invalid_json: bool = False,
        raise_kind: ModelErrorKind | None = None,
        delay: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.responses = responses or {}
        self.invalid_json = invalid_json
        self.raise_kind = raise_kind
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def _generate(self, task: TaskType, messages: list[dict[str, str]], schema: type) -> Any:
        self.calls.append({"task": task.value, "messages": list(messages)})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_kind is not None:
            recoverable = self.raise_kind not in {
                ModelErrorKind.AUTH,
                ModelErrorKind.UNAVAILABLE,
            }
            raise ModelError(
                self.raise_kind,
                f"mock injected {self.raise_kind.value}",
                recoverable=recoverable,
            )
        if self.invalid_json:
            return "{not valid json"
        if task.value in self.responses:
            return self.responses[task.value]
        return self._synthesise(task, messages, schema)

    def _synthesise(self, task: TaskType, messages: list[dict[str, str]], schema: type[BaseModel]) -> Any:
        prompt = " ".join(m.get("content", "") for m in messages)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        data: dict[str, Any] = {}

        for name, field in schema.model_fields.items():
            annotation = str(field.annotation)
            default = field.default
            if "list[" in annotation:
                value: Any = []
            elif "dict" in annotation:
                value = {}
            elif "bool" in annotation:
                value = False
            elif "int" in annotation or "float" in annotation:
                value = 0
            elif "str" in annotation:
                # A stable token lets tests distinguish prompts deterministically.
                value = f"mock-{task.value.lower()}-{digest}"
            elif default is not None and default is not ...:
                value = default
            else:
                value = None
            data[name] = value
        return data

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            available=True,
            provider=self.name,
            detail="deterministic mock; not a real model call",
            real_live_verified=False,
        )
