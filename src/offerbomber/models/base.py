"""Unified, async model provider abstraction (task book Part B).

Design notes:
- MCP lets a host call OfferBomber tools; it does NOT grant OfferBomber the right
  to call the host model in reverse. Host access is therefore implemented by
  explicit adapters (WorkBuddy cloud task/ACP, DeepSeek Harness headless), never
  by reading private host credentials.
- No OpenAI-compatible endpoint is assumed. Real adapters use the documented
  interfaces only; unavailable environments are reported, not faked.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

log = structlog.get_logger("models")

T = TypeVar("T", bound=BaseModel)


class TaskType(str, Enum):
    RESUME_EXTRACTION = "RESUME_EXTRACTION"
    JOB_ANALYSIS = "JOB_ANALYSIS"
    EXPERIENCE_RERANKING = "EXPERIENCE_RERANKING"
    RESUME_PLANNING = "RESUME_PLANNING"
    RESUME_REWRITING = "RESUME_REWRITING"
    FACT_CONSISTENCY_CHECK = "FACT_CONSISTENCY_CHECK"


class ModelErrorKind(str, Enum):
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    TRANSPORT = "TRANSPORT"
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROVIDER = "PROVIDER"
    UNAVAILABLE = "UNAVAILABLE"


class ModelError(RuntimeError):
    def __init__(self, kind: ModelErrorKind, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.recoverable = recoverable


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 4.0
    jitter: float = 0.1


@dataclass
class HealthStatus:
    available: bool
    provider: str
    detail: str = ""
    real_live_verified: bool = False


class ModelProvider:
    """Async provider contract. Subclasses implement `_generate` only."""

    name = "base"

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.timeout = timeout
        self.retry = retry or RetryPolicy()

    async def generate_structured(
        self,
        task_type: TaskType | str,
        messages: list[dict[str, str]],
        output_schema: type[T],
    ) -> T:
        """Generate a Pydantic-validated structured result with bounded retry."""
        task = TaskType(task_type) if isinstance(task_type, str) else task_type
        last_error: ModelError | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                raw = await asyncio.wait_for(
                    self._generate(task, messages, output_schema),
                    timeout=self.timeout,
                )
                return self._validate(raw, output_schema)
            except asyncio.TimeoutError as exc:
                last_error = ModelError(ModelErrorKind.TIMEOUT, f"{self.name} timed out after {self.timeout}s")
                if not last_error.recoverable:
                    break
                log.warning("model_timeout", provider=self.name, task=task.value, attempt=attempt)
            except asyncio.CancelledError:
                raise
            except ValidationError as exc:
                last_error = ModelError(
                    ModelErrorKind.INVALID_OUTPUT,
                    f"{self.name} produced invalid structured output: {exc}",
                    recoverable=True,
                )
                log.warning("model_invalid_output", provider=self.name, task=task.value, attempt=attempt)
            except ModelError as exc:
                last_error = exc
                if not exc.recoverable:
                    break
                log.warning(
                    "model_error",
                    provider=self.name,
                    task=task.value,
                    kind=exc.kind.value,
                    attempt=attempt,
                )
            if attempt < self.retry.max_attempts:
                await asyncio.sleep(min(self.retry.max_delay, self.retry.base_delay * (2 ** (attempt - 1))))
        assert last_error is not None
        raise last_error

    def _validate(self, raw: Any, schema: type[T]) -> T:
        if isinstance(raw, schema):
            return raw
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ModelError(
                    ModelErrorKind.INVALID_OUTPUT,
                    f"model output is not valid JSON: {exc}",
                ) from exc
        try:
            return schema.model_validate(raw)
        except ValidationError:
            raise

    async def _generate(self, task: TaskType, messages: list[dict[str, str]], schema: type) -> Any:
        raise NotImplementedError

    async def health_check(self) -> HealthStatus:
        return HealthStatus(available=False, provider=self.name, detail="not implemented")


def extract_json_block(text: str) -> str:
    """Return the first {...} or [...] block from a model response, if any."""
    text = text or ""
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ModelError(ModelErrorKind.INVALID_OUTPUT, "no JSON object/array found in model output")
    opens = "{["; closes = "}]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in opens:
            depth += 1
        elif ch in closes:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ModelError(ModelErrorKind.INVALID_OUTPUT, "unterminated JSON block in model output")
