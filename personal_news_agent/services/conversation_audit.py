from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_SENSITIVE_KEY_PARTS = ("authorization", "api_key", "password", "secret", "token")


class ConversationAuditLogger:
    """Append-only JSONL audit trail for raw conversation traffic.

    Business-facing conversation state remains in SQLite. This log records the
    API boundary and streamed execution events so operational diagnosis does
    not require overloading the business tables with transport details.
    """

    def __init__(self, directory: Path, *, enabled: bool = True, retention_days: int = 30):
        self.directory = Path(directory)
        self.enabled = enabled
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        self._last_cleanup_date: str | None = None

    def record(
        self,
        event: str,
        *,
        request_id: str,
        user_id: str,
        conversation_id: str | None,
        channel: str,
        data: dict[str, Any] | None = None,
        api_key_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        row = {
            "timestamp": now.isoformat(),
            "event": str(event),
            "request_id": str(request_id),
            "user_id": str(user_id),
            "conversation_id": conversation_id,
            "channel": str(channel),
            "api_key_id": api_key_id,
            "data": _redact(data or {}),
        }
        encoded = json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            path = self.directory / f"conversation-{now.date().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
            try:
                os.chmod(path, 0o640)
            except OSError:
                pass
            self._cleanup(now)

    def _cleanup(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self._last_cleanup_date == today:
            return
        cutoff = (now - timedelta(days=self.retention_days)).date().isoformat()
        for path in self.directory.glob("conversation-*.jsonl"):
            date_part = path.stem.removeprefix("conversation-")
            if date_part < cutoff:
                try:
                    path.unlink()
                except OSError:
                    continue
        self._last_cleanup_date = today


def _redact(value: Any, key: str = "") -> Any:
    normalized_key = str(key).lower()
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value
