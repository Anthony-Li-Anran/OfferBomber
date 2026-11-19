"""DeepSeek Harness host-model adapter (task book B4).

Verified interface from the official repo (deepseek-ai/deepseek-harness,
docs/cli-help.md, commit 2e39e70, 2026-11-19):
- `dsh --profile headless "task"` — answer one task, result on stdout.
- `dsh --profile headless --json "task"` — newline-delimited run events on stdout.
- `dsh --profile headless --session-id <id> "continue"` — resume a Session.
- `dsh --profile acp` — ACP stdio automation server.

This adapter drives the documented headless CLI via a subprocess (no invented
REST endpoint). The `dsh` binary must be installed by the operator; if it is
absent the provider reports UNAVAILABLE rather than pretending success.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from typing import Any

from .base import (
    HealthStatus,
    ModelError,
    ModelErrorKind,
    ModelProvider,
    TaskType,
    extract_json_block,
)


class DeepSeekHarnessProvider(ModelProvider):
    name = "deepseek_harness"

    def __init__(
        self,
        *,
        binary: str | None = None,
        profile: str = "headless",
        default_model: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.binary = binary or os.getenv("OFFERBOMBER_DSH_BIN", "dsh")
        self.profile = profile
        self.default_model = default_model or os.getenv("OFFERBOMBER_DSH_MODEL")
        self.live_calls = 0

    async def health_check(self) -> HealthStatus:
        path = shutil.which(self.binary)
        if path is None:
            return HealthStatus(
                available=False,
                provider=self.name,
                detail=f"'{self.binary}' not found on PATH; install deepseek-harness",
            )
        try:
            process = await asyncio.create_subprocess_exec(
                path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            if process.returncode != 0:
                return HealthStatus(False, self.name, f"dsh --version failed: {stderr.decode(errors='replace')[:200]}")
            return HealthStatus(
                available=True,
                provider=self.name,
                detail=f"dsh found at {path} ({stdout.decode(errors='replace').strip()})",
                real_live_verified=False,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            return HealthStatus(False, self.name, f"health check failed: {exc}")

    async def _run_headless(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        json_events: bool = True,
    ) -> tuple[str, str]:
        path = shutil.which(self.binary)
        if path is None:
            raise ModelError(
                ModelErrorKind.UNAVAILABLE,
                f"'{self.binary}' not found on PATH",
                recoverable=False,
            )
        args = [path, f"--profile={self.profile}"]
        if json_events:
            args.append("--json")
        if session_id:
            args.extend(["--session-id", session_id])
        args.append(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise ModelError(ModelErrorKind.TIMEOUT, f"dsh headless timed out after {self.timeout}s") from exc
        except OSError as exc:
            raise ModelError(ModelErrorKind.TRANSPORT, f"cannot launch dsh: {exc}") from exc

        err = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ModelError(
                ModelErrorKind.PROVIDER,
                f"dsh headless exit {process.returncode}: {err[:400]}",
                recoverable="rate limit" not in err.lower(),
            )
        return stdout.decode("utf-8", errors="replace"), err

    @staticmethod
    def _text_from_events(stdout: str) -> str:
        """Collect final assistant text from dsh --json newline-delimited events."""
        chunks: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                chunks.append(line)  # non-json fallback: raw final message
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in {"error", "failed"}:
                raise ModelError(ModelErrorKind.PROVIDER, f"dsh event: {event}")
            text = event.get("text") or event.get("content")
            if isinstance(text, str):
                chunks.append(text)
            elif event.get("role") == "assistant" and isinstance(event.get("message"), str):
                chunks.append(event["message"])
        return "\n".join(chunks).strip()

    async def _generate(self, task: TaskType, messages: list[dict[str, str]], schema: type) -> Any:
        prompt = "\n\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)
        prompt += (
            "\n\nReturn ONLY one JSON object matching this schema: "
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        stdout, _stderr = await self._run_headless(prompt)
        text = self._text_from_events(stdout) or stdout
        self.live_calls += 1
        return json.loads(extract_json_block(text))
