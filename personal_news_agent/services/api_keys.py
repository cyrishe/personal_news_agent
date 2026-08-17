from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from personal_news_agent.services.store import NewsStore


class ApiKeyError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ApiPrincipal:
    user_id: str
    api_key_id: str
    name: str


class ApiKeyService:
    def __init__(self, store: NewsStore, *, rate_limit_per_minute: int = 30):
        self.store = store
        self.rate_limit_per_minute = max(1, int(rate_limit_per_minute))
        self._usage: dict[str, deque[float]] = defaultdict(deque)
        self._usage_lock = threading.Lock()

    def create(self, user_id: str, name: str, expires_in_days: int | None = None) -> dict[str, Any]:
        cleaned_name = " ".join(str(name or "").split()).strip() or "默认 API Key"
        raw_key = f"pna_live_{secrets.token_urlsafe(32)}"
        prefix = raw_key[:17]
        expires_at = None
        if expires_in_days is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=max(1, int(expires_in_days)))
            ).isoformat()
        item = self.store.create_api_key(
            user_id=user_id,
            name=cleaned_name[:80],
            key_prefix=prefix,
            secret_hash=_hash_api_key(raw_key),
            expires_at=expires_at,
        )
        return {**item, "api_key": raw_key}

    def list(self, user_id: str) -> list[dict[str, Any]]:
        return self.store.list_api_keys(user_id)

    def revoke(self, user_id: str, api_key_id: str) -> dict[str, Any] | None:
        return self.store.revoke_api_key(user_id, api_key_id)

    def authenticate(self, raw_key: str) -> ApiPrincipal:
        candidate = str(raw_key or "").strip()
        if not candidate.startswith("pna_live_") or len(candidate) < 32:
            raise ApiKeyError("invalid_api_key", "API Key 无效。")
        item = self.store.get_api_key_by_hash(_hash_api_key(candidate))
        if not item or item.get("revoked_at"):
            raise ApiKeyError("invalid_api_key", "API Key 无效或已撤销。")
        expires_at = item.get("expires_at")
        if expires_at and _parse_time(expires_at) <= datetime.now(timezone.utc):
            raise ApiKeyError("expired_api_key", "API Key 已过期。")
        self._check_rate_limit(item["id"])
        self.store.mark_api_key_used(item["id"])
        return ApiPrincipal(user_id=item["user_id"], api_key_id=item["id"], name=item["name"])

    def _check_rate_limit(self, api_key_id: str) -> None:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._usage_lock:
            usage = self._usage[api_key_id]
            while usage and usage[0] <= cutoff:
                usage.popleft()
            if len(usage) >= self.rate_limit_per_minute:
                raise ApiKeyError(
                    "api_rate_limit_exceeded",
                    "API 调用过于频繁，请稍后重试。",
                    status_code=429,
                )
            usage.append(now)


def _hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
