from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable

import httpx


class EverydayProviderError(RuntimeError):
    pass


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class EverydayToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class JsonHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        verify_ssl: bool = True,
        max_response_bytes: int = 2_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    async def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            verify=self.verify_ssl,
            trust_env=False,
            transport=self.transport,
        ) as client:
            response = await client.get(url, params=params)
            if response.is_error:
                raise EverydayProviderError(f"provider HTTP {response.status_code}")
            if len(response.content) > self.max_response_bytes:
                raise EverydayProviderError("provider response exceeded size limit")
            try:
                payload = response.json()
            except ValueError as exc:
                raise EverydayProviderError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise EverydayProviderError("provider returned a non-object response")
        return payload


def tool_success(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "marker": "UNTRUSTED_LIVE_SERVICE_DATA",
        "tool": tool_name,
        "data": data,
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": {"ok": True, "tool": tool_name, "data": data},
        "isError": False,
    }


def tool_error(tool_name: str, exc: Exception) -> dict[str, Any]:
    error_type = type(exc).__name__
    message = str(exc).strip()[:500] or error_type
    return {
        "content": [
            {"type": "text", "text": f"live service failed: {error_type}: {message}"}
        ],
        "structuredContent": {
            "ok": False,
            "tool": tool_name,
            "error_type": error_type,
            "message": message,
        },
        "isError": True,
    }


def required_text(args: dict[str, Any], name: str, *, maximum: int = 160) -> str:
    value = " ".join(str(args.get(name) or "").strip().split())
    if not value:
        raise ValueError(f"{name} is required")
    return value[:maximum]


def optional_text(args: dict[str, Any], name: str, *, maximum: int = 80) -> str:
    return " ".join(str(args.get(name) or "").strip().split())[:maximum]


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def redact_secret(value: Any, secret: str) -> str:
    text = str(value or "")[:500]
    return text.replace(secret, "[redacted]") if secret else text
