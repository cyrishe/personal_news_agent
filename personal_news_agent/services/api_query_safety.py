from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from personal_news_agent.services.store import NewsStore


DECISIONS = frozenset({"pass", "refuse", "safe_answer"})
RISK_LEVELS = frozenset({"none", "low", "medium", "high"})
SAFETY_CATEGORIES = frozenset(
    {
        "none",
        "discrimination_hate",
        "sexual_content",
        "violence_gore",
        "political_public_affairs",
        "sovereignty_territory",
        "terrorism_extremism",
        "illegal_crime",
        "self_harm",
        "privacy_security",
        "other_safety",
    }
)
REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,80}$")
GENERIC_SAFETY_REFUSAL = "抱歉，这类问题超出了当前服务支持的范围，我无法提供相关内容。"
TRUSTED_SENSITIVE_NEWS_DOMAINS = (
    "news.cn",
    "xinhuanet.com",
    "xinhuanet.com.cn",
    "chinanews.com.cn",
    "gov.cn",
    "fmprc.gov.cn",
    "gwytb.gov.cn",
    "cma.gov.cn",
    "weather.com.cn",
    "people.com.cn",
    "cctv.com",
    "cgtn.com",
)


class ApiQuerySafetyUnavailable(RuntimeError):
    """The API-only safety preflight could not produce a valid decision."""


@dataclass(frozen=True)
class ApiQuerySafetyResult:
    decision: str
    categories: tuple[str, ...]
    risk_level: str
    reason_code: str
    response: str = ""


class ApiQuerySafetyService:
    def __init__(self, store: NewsStore, cc_runtime: Any | None, *, enabled: bool = True) -> None:
        self.store = store
        self.cc_runtime = cc_runtime
        self.enabled = enabled

    async def classify(
        self,
        message: str,
        *,
        user_id: str,
        conversation_id: str,
        history: str = "无",
    ) -> ApiQuerySafetyResult:
        if not self.enabled:
            return ApiQuerySafetyResult("pass", ("none",), "none", "safety_disabled")
        if not self.cc_runtime or not getattr(self.cc_runtime, "configured", False):
            self._log_unavailable(conversation_id, user_id, "cc_runtime_unavailable")
            raise ApiQuerySafetyUnavailable("API query safety service is unavailable")

        last_error: Exception | None = None
        result: ApiQuerySafetyResult | None = None
        for attempt in range(2):
            try:
                runtime_result = await self.cc_runtime.run_api_query_safety(
                    message=str(message or "")[:8_000],
                    history=str(history or "无")[:4_000],
                )
                result = _parse_result(runtime_result.answer)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    continue
        if result is None:
            detail = f"{type(last_error).__name__}: {str(last_error or '')[:300]}"
            self._log_unavailable(conversation_id, user_id, detail)
            raise ApiQuerySafetyUnavailable("API query safety service is unavailable") from last_error

        self.store.log(
            "api_query_safety",
            result.decision,
            target=conversation_id,
            detail={
                "user_id": user_id,
                "categories": list(result.categories),
                "risk_level": result.risk_level,
                "reason_code": result.reason_code,
            },
        )
        return result

    def _log_unavailable(self, conversation_id: str, user_id: str, reason: str) -> None:
        self.store.log(
            "api_query_safety",
            "unavailable",
            target=conversation_id,
            detail={"user_id": user_id, "reason": reason},
        )


def _parse_result(text: str) -> ApiQuerySafetyResult:
    payload = _decode_json_object(text)
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        raise ValueError("invalid safety decision")

    raw_categories = payload.get("categories")
    if not isinstance(raw_categories, list):
        raise ValueError("invalid safety categories")
    categories = tuple(dict.fromkeys(str(item).strip() for item in raw_categories if str(item).strip()))
    if not categories or len(categories) > 4 or any(item not in SAFETY_CATEGORIES for item in categories):
        raise ValueError("invalid safety categories")

    risk_level = str(payload.get("risk_level") or "").strip().lower()
    if risk_level not in RISK_LEVELS:
        raise ValueError("invalid safety risk level")
    reason_code = str(payload.get("reason_code") or "").strip().lower()
    if not REASON_CODE_PATTERN.fullmatch(reason_code):
        raise ValueError("invalid safety reason code")
    response = str(payload.get("response") or "").strip()[:2_000]

    if decision == "pass":
        response = ""
    elif "none" in categories or not response:
        raise ValueError("invalid constrained safety response")
    elif decision == "refuse":
        # Refusal is a terminal safety action, not a fact-checking branch. Keep
        # it deliberately non-specific so model wording cannot imply that an
        # unsafe allegation may be true but merely lacks evidence.
        response = GENERIC_SAFETY_REFUSAL
    return ApiQuerySafetyResult(decision, categories, risk_level, reason_code, response)


def _decode_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value).strip()
    start = value.find("{")
    if start < 0:
        raise ValueError("safety result is not JSON")
    payload, _ = json.JSONDecoder().raw_decode(value[start:])
    if not isinstance(payload, dict):
        raise ValueError("safety result must be an object")
    return payload
