from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from personal_news_agent.app import app
from personal_news_agent.services.content_moderation import (
    ContentModerationError,
    TextModerationPlusService,
)


class StubAliyunModeration(TextModerationPlusService):
    def __init__(self, payload: dict):
        super().__init__(access_key_id="id", access_key_secret="secret")
        self.payload = payload
        self.captured: dict | None = None

    def _call_text_moderation_plus(
        self,
        text: str,
        service: str,
        *,
        account_id: str | None = None,
        data_id: str | None = None,
    ) -> dict:
        self.captured = {
            "text": text,
            "service": service,
            "account_id": account_id,
            "data_id": data_id,
        }
        return self.payload


class UnavailableModeration:
    configured = True
    query_service = "llm_query_moderation"

    def __init__(self, *, fail_open: bool):
        self.fail_open = fail_open

    def check_query_text(self, *_args, **_kwargs):
        raise ContentModerationError(
            "commodity is not activated",
            provider_code="ServiceNotActivated",
            request_id="provider-request-id",
        )


def test_aliyun_query_moderation_parses_safe_and_risky_results():
    safe = StubAliyunModeration(
        {
            "Code": 200,
            "Message": "OK",
            "RequestId": "safe-request",
            "Data": {"RiskLevel": "none", "Result": [{"Label": "nonLabel"}]},
        }
    )
    safe_result = safe.check_query_text("科技新闻", account_id="usr_1", data_id="conv_1")
    assert safe_result.allowed is True
    assert safe.captured == {
        "text": "科技新闻",
        "service": "llm_query_moderation",
        "account_id": "usr_1",
        "data_id": "conv_1",
    }

    risky = StubAliyunModeration(
        {
            "Code": 200,
            "Message": "OK",
            "RequestId": "risk-request",
            "Data": {"RiskLevel": "high", "Result": [{"Label": "violence", "Description": "风险内容"}]},
        }
    )
    risky_result = risky.check_query_text("风险内容")
    assert risky_result.allowed is False
    assert risky_result.label == "violence"


def test_aliyun_provider_failure_is_not_a_policy_block():
    unavailable = StubAliyunModeration(
        {
            "Code": 500,
            "Message": "you haven't activated the commodity",
            "RequestId": "not-activated-request",
            "Data": {},
        }
    )
    try:
        unavailable.check_query_text("普通问题")
    except ContentModerationError as exc:
        assert "activated" in str(exc)
        assert exc.request_id == "not-activated-request"
    else:
        raise AssertionError("provider failure must raise ContentModerationError")


def test_chat_moderation_fail_open_and_fail_closed_are_distinct():
    chat = app.state.services["chat"]
    original = chat.content_moderation
    user_id = f"moderation_{uuid4().hex}"
    try:
        with TestClient(app) as client:
            chat.content_moderation = UnavailableModeration(fail_open=True)
            continued = client.post(
                "/api/chat",
                json={
                    "user_id": user_id,
                    "message": "科技新闻",
                    "use_llm": False,
                    "allow_web_search": False,
                },
            )
            assert continued.status_code == 200
            assert continued.json()["context_relation"] != "query_moderation_blocked"
            assert continued.json()["context_relation"] != "query_moderation_unavailable"

            chat.content_moderation = UnavailableModeration(fail_open=False)
            stopped = client.post(
                "/api/chat",
                json={
                    "user_id": user_id,
                    "message": "科技新闻",
                    "use_llm": False,
                    "allow_web_search": False,
                },
            )
            assert stopped.status_code == 200
            assert stopped.json()["context_relation"] == "query_moderation_unavailable"

        with app.state.services["store"].connect() as conn:
            rows = conn.execute(
                """
                SELECT status, detail_json FROM operation_logs
                WHERE operation = 'query_content_moderation' AND target LIKE 'conv_%'
                ORDER BY created_at DESC LIMIT 2
                """
            ).fetchall()
        assert rows
        details = [json.loads(row["detail_json"]) for row in rows]
        assert all(detail["provider_code"] == "ServiceNotActivated" for detail in details)
    finally:
        chat.content_moderation = original
