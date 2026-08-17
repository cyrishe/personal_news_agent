from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from personal_news_agent.app import create_app
from personal_news_agent.config import Settings
from personal_news_agent.services.conversation_audit import ConversationAuditLogger


@pytest.fixture
def api_app(tmp_path):
    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'api.db'}",
            seed_demo_data=False,
            background_crawl_enabled=False,
            crawl_database_url=None,
            elasticsearch_url=None,
            external_search_provider="none",
            llm_key=None,
            content_moderation_enabled=False,
            conversation_audit_log_dir=tmp_path / "conversation-audit",
            cc_runtime_enabled=False,
            phone_challenge_provider="disabled",
        )
    )


def _session_for_new_user(api_app) -> tuple[str, str]:
    store = api_app.state.services["store"]
    store.init()
    suffix = uuid4().hex
    user = store.create_user(
        display_name=f"API tester {suffix[:8]}",
        email=f"api-{suffix}@example.test",
        password_hash=None,
    )
    session_token = f"session_{uuid4().hex}"
    store.create_session(
        user["id"],
        session_token,
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    return user["id"], session_token


def _create_key(client: TestClient, session_token: str, name: str = "integration") -> dict:
    response = client.post(
        "/api/v1/api-keys",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"name": name, "expires_in_days": 30},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_api_key_conversation_contract_and_tenant_isolation(api_app):
    audit = api_app.state.services["conversation_audit"]
    with TestClient(api_app) as client:
        user_id, session_token = _session_for_new_user(api_app)
        created_key = _create_key(client, session_token)
        raw_key = created_key["api_key"]
        assert raw_key.startswith("pna_live_")
        assert created_key["user_id"] == user_id

        listed = client.get(
            "/api/v1/api-keys",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == created_key["id"]
        assert "api_key" not in listed.json()["items"][0]

        conversation = client.post(
            "/api/v1/conversations",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"title": "API contract test"},
        )
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["conversation"]["id"]

        message = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Authorization": f"Bearer {raw_key}", "X-Request-ID": "contract-test-1"},
            json={
                "message": "科技新闻",
                "use_llm": True,
                "allow_web_search": False,
            },
        )
        assert message.status_code == 200, message.text
        assert message.headers["x-request-id"] == "contract-test-1"
        assert message.json()["conversation_id"] == conversation_id
        assert message.json()["turn_id"]
        assert message.json()["conversation_mode"] == "general"
        assert message.json()["message"]["role"] == "assistant"
        assert message.json()["response"]["context_relation"] == "general_conversation_fallback"

        history = client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert history.status_code == 200
        assert history.json()["turns"][-1]["user_message"] == "科技新闻"

        _, other_session = _session_for_new_user(api_app)
        other_key = _create_key(client, other_session, "other")["api_key"]
        forbidden_as_not_found = client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {other_key}"},
        )
        assert forbidden_as_not_found.status_code == 404

        revoked = client.delete(
            f"/api/v1/api-keys/{created_key['id']}",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        assert revoked.status_code == 200
        rejected = client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert rejected.status_code == 401

    lines = [
        json.loads(line)
        for path in audit.directory.glob("conversation-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [line["event"] for line in lines] == ["request", "response"]
    assert all(line["request_id"] == "contract-test-1" for line in lines)
    assert raw_key not in json.dumps(lines, ensure_ascii=False)


def test_api_key_creation_requires_login_session(api_app):
    with TestClient(api_app) as client:
        missing = client.post("/api/v1/api-keys", json={"name": "no session"})
        assert missing.status_code == 401

        fake_api_key = "pna_live_" + "x" * 43
        invalid = client.post(
            "/api/v1/api-keys",
            headers={"Authorization": f"Bearer {fake_api_key}"},
            json={"name": "wrong credential type"},
        )
        assert invalid.status_code == 401


def test_conversation_audit_redacts_sensitive_fields(tmp_path):
    logger = ConversationAuditLogger(tmp_path, enabled=True, retention_days=7)
    logger.record(
        "request",
        request_id="req-redaction",
        user_id="usr_test",
        conversation_id="conv_test",
        channel="test",
        data={
            "message": "原始问题仍需保留",
            "authorization": "Bearer secret",
            "nested": {"api_key": "pna_live_secret", "answer": "原始回答"},
        },
    )
    path = next(tmp_path.glob("conversation-*.jsonl"))
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["data"]["message"] == "原始问题仍需保留"
    assert row["data"]["authorization"] == "<redacted>"
    assert row["data"]["nested"]["api_key"] == "<redacted>"
    assert row["data"]["nested"]["answer"] == "原始回答"
