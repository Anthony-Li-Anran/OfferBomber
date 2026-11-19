"""WorkBuddy host-model adapter (task book B3).

Real interfaces (verified from https://open.workbuddy.cn/docs/openapi on 2026-11-19):
- POST https://www.workbuddy.cn/openapi/v2/tasks  {prompt, name} -> {task_id, status, link, token}
- GET  https://www.workbuddy.cn/openapi/v2/tasks/{task_id} -> status/link/token
- ACP channel: GET {link} (SSE, Authorization: Bearer {token}) + POST {link}
  JSON-RPC 2.0 methods initialize / session/load / session/prompt.

There is NO OpenAI-compatible Chat Completions endpoint. This adapter does not
read WorkBuddy internal account credentials: the OAuth access token must be
supplied explicitly by the caller (env OFFERBOMBER_WORKBUDDY_TOKEN), and the
ACP task token comes from the API response.

Live call is only claimed when a real token was used and a task completed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
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

TASKS_URL = "https://www.workbuddy.cn/openapi/v2/tasks"
LOCAL_ASSISTANT_URL = "https://www.workbuddy.cn/openapi/v2/localassistant"
TERMINAL_STATUSES = {"completed", "failed", "archived", "deleted"}


class WorkBuddyProvider(ModelProvider):
    """Submits structured tasks through WorkBuddy cloud tasks + ACP.

    Async lifecycle: create -> poll status -> open SSE -> initialize ->
    session/load -> session/prompt -> await terminal response -> parse JSON.
    """

    name = "workbuddy"

    def __init__(
        self,
        *,
        access_token: str | None = None,
        base_url: str = TASKS_URL,
        poll_interval: float = 2.0,
        status_timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.access_token = access_token or os.getenv("OFFERBOMBER_WORKBUDDY_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.status_timeout = status_timeout or self.timeout
        self.live_calls = 0

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            raise ModelError(
                ModelErrorKind.AUTH,
                "WorkBuddy access token missing; set OFFERBOMBER_WORKBUDDY_TOKEN",
                recoverable=False,
            )
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    async def health_check(self) -> HealthStatus:
        if not self.access_token:
            return HealthStatus(
                available=False,
                provider=self.name,
                detail="OFFERBOMBER_WORKBUDDY_TOKEN not configured",
            )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}?page=1&size=1", headers=self._headers())
            if response.status_code == 401:
                return HealthStatus(False, self.name, "token rejected (401)")
            response.raise_for_status()
            return HealthStatus(
                available=True,
                provider=self.name,
                detail="token accepted; cloud task API reachable",
                real_live_verified=False,  # auth check is not a model task
            )
        except httpx.HTTPError as exc:
            return HealthStatus(False, self.name, f"transport error: {exc}")

    async def _create_task(self, prompt: str, name: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=min(20.0, self.timeout)) as client:
            response = await client.post(
                self.base_url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"prompt": prompt, "name": name},
            )
        if response.status_code == 401:
            raise ModelError(ModelErrorKind.AUTH, "WorkBuddy token rejected (401)", recoverable=False)
        if response.status_code == 429:
            raise ModelError(ModelErrorKind.RATE_LIMIT, "WorkBuddy task API rate limited")
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError(ModelErrorKind.TRANSPORT, f"WorkBuddy create task failed: {exc}") from exc
        if not data.get("task_id"):
            raise ModelError(ModelErrorKind.PROVIDER, f"WorkBuddy create task missing task_id: {data}")
        return data

    async def _wait_for_task(self, task_id: str) -> dict[str, Any]:
        """Poll GET /tasks/{id} until terminal or timeout; return latest payload."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.status_timeout
        latest: dict[str, Any] = {}
        while loop.time() < deadline:
            async with httpx.AsyncClient(timeout=min(20.0, self.timeout)) as client:
                try:
                    response = await client.get(f"{self.base_url}/{task_id}", headers=self._headers())
                    response.raise_for_status()
                    latest = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise ModelError(ModelErrorKind.TRANSPORT, f"WorkBuddy poll failed: {exc}") from exc
            if str(latest.get("status", "")).lower() in TERMINAL_STATUSES:
                return latest
            await asyncio.sleep(self.poll_interval)
        raise ModelError(ModelErrorKind.TIMEOUT, f"WorkBuddy task {task_id} did not finish in {self.status_timeout}s")

    async def _acp_prompt(
        self,
        *,
        link: str,
        token: str,
        session_id: str,
        prompt: str,
    ) -> str:
        """Minimal ACP client: SSE receive channel + JSON-RPC POST channel."""
        connection_id = ""
        response_text = ""
        rpc_id = 0

        def next_id() -> int:
            nonlocal rpc_id
            rpc_id += 1
            return rpc_id

        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", link, headers=headers) as stream:
                for header, value in stream.headers.items():
                    if header.lower() == "acp-connection-id":
                        connection_id = value
                if not connection_id:
                    raise ModelError(ModelErrorKind.PROVIDER, "ACP SSE missing Acp-Connection-Id")

                async def send(method: str, params: dict[str, Any]) -> None:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": next_id(),
                        "method": method,
                        "params": params,
                    }
                    post_headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Acp-Connection-Id": connection_id,
                    }
                    post = await client.post(link, headers=post_headers, json=payload)
                    post.raise_for_status()

                await send(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                    },
                )
                await send("session/load", {"sessionId": session_id, "cwd": "/workspace", "mcpServers": []})
                prompt_id = next_id()
                await send(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                )

                async for line in stream.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        message = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if message.get("id") == prompt_id and "result" in message:
                        result = message["result"] or {}
                        stop_reason = result.get("stopReason")
                        if stop_reason in {"cancelled", "refusal"}:
                            raise ModelError(
                                ModelErrorKind.PROVIDER,
                                f"ACP stopped with {stop_reason}",
                                recoverable=False,
                            )
                        text_parts = []
                        for block in result.get("content", []) or []:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                        response_text = "\n".join(text_parts)
                        break
        if not response_text:
            raise ModelError(ModelErrorKind.PROVIDER, "ACP returned no text content")
        return response_text

    async def _generate(self, task: TaskType, messages: list[dict[str, str]], schema: type) -> Any:
        prompt = "\n\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
        )
        prompt += (
            "\n\nReturn ONLY one JSON object matching this Pydantic schema: "
            f"{schema.model_json_schema()}"
        )
        created = await self._create_task(prompt=prompt, name=f"offerbomber-{task.value}")
        task_id = str(created["task_id"])
        final = await self._wait_for_task(task_id)
        if str(final.get("status", "")).lower() == "failed":
            raise ModelError(ModelErrorKind.PROVIDER, f"WorkBuddy task {task_id} failed")
        link = final.get("link") or created.get("link")
        token = final.get("token") or created.get("token")
        if not link or not token:
            raise ModelError(ModelErrorKind.PROVIDER, "WorkBuddy task has no ACP link/token")
        text = await self._acp_prompt(link=link, token=token, session_id=task_id, prompt=prompt)
        self.live_calls += 1
        return json.loads(extract_json_block(text))
