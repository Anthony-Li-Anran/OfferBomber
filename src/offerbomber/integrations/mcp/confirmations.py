"""Cross-process confirmation token store.

WorkBuddy / DeepSeek Harness may launch a fresh MCP server process for each
tool call. An in-memory token cannot survive that: prepare_generation runs in
one process and generate_resume in another. Persist pending actions to a small
JSON file so the human-in-the-loop handshake works with stateless hosts.

Tokens are single-use and expire after a short TTL. File writes are atomic
(temp file + os.replace) so concurrent calls cannot corrupt the store.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 15 * 60


class ConfirmationStore:
    def __init__(self, path: Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.path = path
        self.ttl = ttl_seconds

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def create(self, preview: dict[str, Any]) -> str:
        token = uuid.uuid4().hex
        data = self._read()
        data[token] = {"preview": preview, "created_at": time.time()}
        self._write(data)
        return token

    def peek(self, token: str) -> dict[str, Any] | None:
        """Return a still-valid pending preview without consuming it."""
        data = self._read()
        entry = data.get(token)
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("created_at", 0)) > self.ttl:
            data.pop(token, None)
            self._write(data)
            return None
        preview = entry.get("preview")
        return preview if isinstance(preview, dict) else None

    def consume(self, token: str) -> dict[str, Any] | None:
        """Atomically claim and remove a pending token (single use)."""
        data = self._read()
        entry = data.get(token)
        if not isinstance(entry, dict):
            return None
        if time.time() - float(entry.get("created_at", 0)) > self.ttl:
            data.pop(token, None)
            self._write(data)
            return None
        preview = entry.get("preview")
        data.pop(token, None)
        self._write(data)
        return preview if isinstance(preview, dict) else None
