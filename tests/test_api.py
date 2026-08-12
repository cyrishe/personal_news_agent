from fastapi.testclient import TestClient
from datetime import datetime, timedelta, timezone
from io import BytesIO
import time
from uuid import uuid4
import zipfile

from personal_news_agent.app import app
from personal_news_agent.api.schemas import ChatRequest, RelatedSearchRequest
from personal_news_agent.config import settings
from personal_news_agent.config import Settings
from personal_news_agent.services.phone_verification import PhoneVerificationService


object.__setattr__(settings, "realname_provider", "mock")


def test_chat_request_defaults_to_cc_with_web_search():
    payload = ChatRequest(message="今天有什么重要新闻")

    assert payload.use_llm is True
    assert payload.allow_web_search is True

    related = RelatedSearchRequest(query="Ame")
    assert related.allow_web_search is True


def test_api_health_and_main_routes():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["frontend_revision"] == "20260812-topic-rail-1"
        assert health.json()["source_count"] >= 20

        feed = client.get("/api/feed?category=tech&limit=5")
        assert feed.status_code == 200
        assert feed.json()["items"]

        events = client.get("/api/events?category=tech")
        assert events.status_code == 200
        assert events.json()["items"]

        recommended = client.get("/api/topics/recommended?user_id=default&limit=4&use_llm=false")
        assert recommended.status_code == 200
        assert recommended.json()["window_hours"] == 24
        assert recommended.json()["refresh_window_hours"] == 6
        assert recommended.json()["generation_source"] == "fallback"

        invalid_window = client.get("/api/topics/recommended?window_hours=3&refresh_window_hours=6&use_llm=false")
        assert invalid_window.status_code == 400

        backend = client.get("/api/news/search/backend")
        assert backend.status_code == 200
        assert backend.json()["local_backend"] == "sqlite_fts"
        assert isinstance(backend.json()["external_search_available"], bool)

        web = client.get("/web")
        assert web.status_code == 200
        assert "news-console" in web.text
        assert "data-es-status" in web.text
        assert "data-due-urls" in web.text
        assert "深度挖掘" in web.text

        auth = client.get("/auth")
        assert auth.status_code == 200
        assert auth.headers["cache-control"] == "no-store, max-age=0"
        assert auth.headers["x-pna-frontend-revision"] == "20260812-topic-rail-1"
        assert 'data-auth-mode-target="login"' in auth.text
        assert "短信验证码仅用于确认你持有该手机号" in auth.text

        mobile = client.get("/mobile")
        assert mobile.status_code == 200
        assert 'id="mobileTemplate"' in mobile.text
        assert "联网回答" in mobile.text


def test_auth_register_login_and_realname_status():
    with TestClient(app) as client:
        _enable_mock_phone_auth(client)
        auth_config = client.get("/api/auth/config")
        assert auth_config.status_code == 200
        assert auth_config.json()["available"] is True
        assert auth_config.json()["method"] == "sms_otp"
        mobile = _mobile()
        challenge = client.post("/api/auth/registration-code", json={"mobile": mobile})
        assert challenge.status_code == 200
        assert challenge.json()["debug_code"] == "123456"
        registered = client.post(
            "/api/auth/register",
            json={
                "mobile": mobile,
                "challenge_id": challenge.json()["challenge_id"],
                "verification_code": "123456",
                "password": "12345678",
                "confirm_password": "12345678",
            },
        )
        assert registered.status_code == 200
        assert registered.json()["user"]["username"] == mobile
        assert registered.json()["user"]["mobile"] == mobile[:3] + "****" + mobile[-4:]
        assert registered.json()["session"]["token"]

        login = client.post("/api/auth/login", json={"mobile": mobile, "password": "12345678"})
        assert login.status_code == 200
        assert login.json()["user"]["username"] == mobile

        realname = client.get("/api/auth/realname/status")
        assert realname.status_code == 200
        assert realname.json()["provider"] == "mock"

        mismatch_mobile = _mobile()
        mismatch_challenge = client.post("/api/auth/registration-code", json={"mobile": mismatch_mobile}).json()
        mismatch = client.post("/api/auth/register", json={
            "mobile": mismatch_mobile,
            "challenge_id": mismatch_challenge["challenge_id"],
            "verification_code": "123456",
            "password": "12345678",
            "confirm_password": "87654321",
        })
        assert mismatch.status_code == 400
        assert mismatch.json()["detail"]["code"] == "password_mismatch"


def test_registration_code_provider_timeout_returns_promptly_and_keeps_api_healthy():
    with TestClient(app) as client:
        auth = client.app.state.services["auth"]
        original_request = auth.request_registration_code
        original_timeout = settings.phone_challenge_request_timeout_seconds

        def slow_request(mobile: str, remote_addr: str = "") -> dict:
            time.sleep(0.4)
            return {"challenge_id": "late-result"}

        auth.request_registration_code = slow_request
        object.__setattr__(settings, "phone_challenge_request_timeout_seconds", 0.1)
        started_at = time.monotonic()
        try:
            response = client.post(
                "/api/auth/registration-code",
                json={"mobile": "13800138000"},
                headers={"X-Real-IP": "203.0.113.8"},
            )
        finally:
            auth.request_registration_code = original_request
            object.__setattr__(settings, "phone_challenge_request_timeout_seconds", original_timeout)

        assert time.monotonic() - started_at < 1.0
        assert response.status_code == 504
        assert response.json()["detail"]["code"] == "phone_code_send_timeout"
        assert client.get("/api/health").status_code == 200


def test_onboarding_generates_profile_prompt_and_model_choice():
    with TestClient(app) as client:
        models = client.get("/api/models")
        assert models.status_code == 200
        model_payload = models.json()
        assert model_payload["default_model"] == "yuanrong-personal-assistant"
        assert {item["key"] for item in model_payload["items"]} == {
            "yuanrong-personal-assistant",
            "qwen3.6",
            "deepseek-v4-flash",
        }
        assert {item["provider_model"] for item in model_payload["items"]} == {"deepseek-v4-flash"}
        assert all(item["logical_only"] is True for item in model_payload["items"])

        options = client.get("/api/onboarding/options")
        assert options.status_code == 200
        categories = options.json()["categories"]
        assert any(item["key"] == "sports" and item["implemented"] for item in categories)
        assert any(item["key"] == "politics" and item["implemented"] for item in categories)

        _enable_mock_phone_auth(client)
        mobile = _mobile()
        challenge = client.post("/api/auth/registration-code", json={"mobile": mobile}).json()
        registered = client.post(
            "/api/auth/register",
            json={
                "mobile": mobile,
                "challenge_id": challenge["challenge_id"],
                "verification_code": "123456",
                "password": "12345678",
                "confirm_password": "12345678",
            },
        )
        assert registered.status_code == 200
        user_id = registered.json()["user"]["id"]

        completed = client.post(
            "/api/onboarding/complete",
            json={
                "user_id": user_id,
                "display_name": "小明",
                "self_description": "互联网从业者，关注 AI 产品、游戏和体育商业。",
                "age": 28,
                "gender": "男",
                "zodiac": "天秤座",
                "preferred_categories": ["sports", "entertainment", "politics"],
                "watch_keywords": ["NBA", "OpenAI"],
                "negative_keywords": ["短线荐股"],
                "model_key": "yuanrong-personal-assistant",
                "output_style": "休闲",
            },
        )
        assert completed.status_code == 200
        body = completed.json()
        assert body["model"]["provider_model"] == "deepseek-v4-flash"
        assert body["model"]["has_fixed_system_prompt"] is True
        assert any(item["key"] == "assistant_prompt_saved" for item in body["preparation"])
        assert "元融个人助理大模型" in body["assistant_prompt"]
        assert "天秤座" in body["assistant_prompt"]
        assert "NBA" in body["assistant_prompt"]
        assert "互联网从业者" in body["assistant_prompt"]

        with client.app.state.services["store"].connect() as conn:
            user = conn.execute("SELECT assistant_prompt FROM pna_users WHERE id = ?", (user_id,)).fetchone()
            profile = conn.execute("SELECT self_description, age, gender, zodiac, model_key, output_style, onboarding_completed FROM pna_user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        assert user is not None and "休闲" in user["assistant_prompt"]
        assert profile["self_description"] == "互联网从业者，关注 AI 产品、游戏和体育商业。"
        assert profile["age"] == 28
        assert profile["gender"] == "男"
        assert profile["zodiac"] == "天秤座"
        assert profile["model_key"] == "yuanrong-personal-assistant"
        assert profile["output_style"] == "休闲"
        assert profile["onboarding_completed"] == 1

        loaded = client.get(f"/api/profile?user_id={user_id}")
        assert loaded.status_code == 200
        assert loaded.json()["profile"]["self_description"].startswith("互联网从业者")
        assert loaded.json()["user"]["assistant_prompt"]


def _mobile() -> str:
    return "13" + str(uuid4().int % 1_000_000_000).zfill(9)


def _enable_mock_phone_auth(client: TestClient) -> None:
    store = client.app.state.services["store"]
    mock_settings = Settings(
        database_url=settings.database_url,
        phone_challenge_provider="mock",
        phone_challenge_secret="api-test-phone-challenge-secret-that-is-long-enough",
        phone_challenge_mock_enabled=True,
        phone_challenge_mock_code="123456",
        phone_challenge_resend_seconds=10,
    )
    client.app.state.services["auth"].phone_verification = PhoneVerificationService(store, mock_settings)


def test_api_chat_report_and_task_flow():
    with TestClient(app) as client:
        task_user_id = f"api_task_user_{uuid4().hex[:8]}"
        due_user_id = f"api_due_user_{uuid4().hex[:8]}"
        turn1 = client.post(
            "/api/chat",
            json={
                "conversation_id": "api_conv",
                "message": "今天汽车圈有什么新闻？",
                "use_llm": False,
                "allow_web_search": False,
            },
        )
        assert turn1.status_code == 200
        assert len(turn1.json()["recommendations"]) >= 2

        turn2 = client.post(
            "/api/chat",
            json={
                "conversation_id": "api_conv",
                "message": "第二条展开说说。",
                "use_llm": False,
                "allow_web_search": False,
            },
        )
        assert turn2.status_code == 200
        assert turn2.json()["focus_object"]["ordinal"] == 2

        history = client.get("/api/chat/conversations/api_conv?user_id=default")
        assert history.status_code == 200
        assert len(history.json()["turns"]) >= 2
        assert history.json()["turns"][-1]["response"]["conversation_id"] == "api_conv"

        conversations = client.get("/api/chat/conversations?user_id=default")
        assert conversations.status_code == 200
        api_conversation = next(item for item in conversations.json()["items"] if item["conversation_id"] == "api_conv")
        assert api_conversation["first_message"] == "今天汽车圈有什么新闻？"
        assert api_conversation["turn_count"] >= 2

        normal_topic_user = f"normal_topic_{uuid4().hex[:8]}"
        normal_topic = client.post(
            "/api/chat",
            json={
                "conversation_id": f"normal_topic_conv_{uuid4().hex[:8]}",
                "user_id": normal_topic_user,
                "message": "量子计算产业最近消息",
            },
        )
        assert normal_topic.status_code == 200
        detected_topics = client.get(f"/api/topics?user_id={normal_topic_user}")
        assert detected_topics.status_code == 200
        user_topics = [item for item in detected_topics.json()["items"] if item["topic_type"] == "user"]
        assert user_topics == []

        report = client.post(
            "/api/reports",
            json={"topic": "新能源汽车价格战", "category_scope": ["auto", "economy"], "time_range": "30d"},
        )
        assert report.status_code == 200
        assert report.json()["timeline"]
        report_id = report.json()["report_id"]
        pdf = client.get(f"/api/reports/{report_id}/download?format=pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF-")
        assert len(pdf.content) > 20_000
        assert b"STSong-Light" not in pdf.content
        pdf_wrong_user = client.get(f"/api/reports/{report_id}/download?format=pdf&user_id=someone_else")
        assert pdf_wrong_user.status_code == 200
        assert pdf_wrong_user.headers["content-type"] == "application/pdf"
        assert pdf_wrong_user.content.startswith(b"%PDF-")
        docx = client.get(f"/api/reports/{report_id}/download?format=docx")
        assert docx.status_code == 200
        assert docx.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
        with zipfile.ZipFile(BytesIO(docx.content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "专题报告：新能源汽车价格战" in document_xml
        assert "一句话结论" in document_xml
        assert "证据来源列表" in document_xml
        assert "已截断" not in document_xml

        topic_view = client.post(
            "/api/topics/view",
            json={"topic": "新能源汽车价格战", "category_scope": ["auto", "economy"], "max_articles": 8},
        )
        assert topic_view.status_code == 200
        topic_payload = topic_view.json()
        assert topic_payload["event_line"]["items"]
        assert topic_payload["relation_graph"]["nodes"]

        topic_summary = client.post(
            "/api/topics/summary",
            json={"topic": "新能源汽车价格战", "category_scope": ["auto", "economy"], "max_articles": 8, "use_llm": False},
        )
        assert topic_summary.status_code == 200
        summary_payload = topic_summary.json()
        assert summary_payload["report_id"].startswith("rpt_")
        assert summary_payload["summary"]["sections"]
        assert summary_payload["summary"]["graph"]["nodes"]
        assert "## 时间线" in summary_payload["markdown"]

        task = client.post(
            "/api/tasks",
            json={
                "user_id": task_user_id,
                "task_type": "daily_digest",
                "schedule": "0 21 * * *",
                "category_scope": ["tech", "game", "auto"],
                "topics": ["AI", "任天堂", "新能源汽车"],
                "delivery_channel": "browser",
            },
        )
        assert task.status_code == 200
        assert task.json()["next_run_at"]
        listed = client.get(f"/api/tasks?user_id={task_user_id}")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == task.json()["id"]

        run = client.post(f"/api/tasks/{task.json()['id']}/run")
        assert run.status_code == 200
        assert run.json()["status"] == "ok"
        assert run.json()["notification"]["target_id"] == run.json()["report_id"]

        notifications = client.get(f"/api/notifications?user_id={task_user_id}")
        assert notifications.status_code == 200
        notification_id = notifications.json()["items"][0]["id"]
        read = client.post(f"/api/notifications/{notification_id}/read", json={"user_id": task_user_id})
        assert read.status_code == 200
        assert read.json()["item"]["read_at"]

        due_task = client.post(
            "/api/tasks",
            json={
                "user_id": due_user_id,
                "task_type": "topic_tracking",
                "schedule": "*/20 * * * *",
                "category_scope": ["sports"],
                "topics": ["机车赛事"],
            },
        )
        assert due_task.status_code == 200
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with client.app.state.services["store"].connect() as conn:
            conn.execute("UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?", (past, due_task.json()["id"]))
        due = client.post("/api/tasks/due/run", json={"user_id": due_user_id, "limit": 3})
        assert due.status_code == 200
        assert due.json()["ran_count"] == 1

        schedule_user_id = f"api_schedule_user_{uuid4().hex[:8]}"
        task_service = client.app.state.services["tasks"]
        original_llm = task_service.llm_client
        original_cc_runtime = task_service.cc_runtime
        task_service.llm_client = type("FakeLLM", (), {"configured": False})()
        task_service.cc_runtime = None
        try:
            schedule_api = client.post(
                "/api/tasks/schedule",
                json={
                    "user_id": schedule_user_id,
                    "message": "/schedule 帮我定时每天早晨9点收集关于AI Agent的新闻，并总结成一个专题发给我",
                },
            )
            assert schedule_api.status_code == 200
            assert schedule_api.json()["api_params"]["parsed_workflow"]["report_style"]["sections"]

            schedule_chat = client.post(
                "/api/chat",
                json={
                    "conversation_id": "api_schedule_conv",
                    "user_id": schedule_user_id,
                    "message": "/schedule 帮我定时每天早晨9点收集关于AI Agent的新闻，并总结成一个专题发给我",
                },
            )
        finally:
            task_service.llm_client = original_llm
            task_service.cc_runtime = original_cc_runtime
        assert schedule_chat.status_code == 200
        assert schedule_chat.json()["context_relation"] == "scheduled_push_created"
        conversations = client.get(f"/api/conversations?user_id={schedule_user_id}")
        assert conversations.status_code == 200
        scheduled_push = [item for item in conversations.json()["items"] if item["kind"] == "scheduled_push"]
        assert scheduled_push


def test_topic_agent_creates_user_topic_and_chat_tracking():
    with TestClient(app) as client:
        user_id = f"topic_user_{uuid4().hex[:8]}"
        listed = client.get(f"/api/topics?user_id={user_id}&limit=20")
        assert listed.status_code == 200
        assert any(item["topic_type"] == "system" and "大型体育赛事" in item["title"] for item in listed.json()["items"])

        created = client.post(
            "/api/topics",
            json={
                "user_id": user_id,
                "text": "我想做一个关于阿根廷队世界杯表现的主题，并帮我持续更新",
                "refresh_now": False,
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["topic"]["title"] == "阿根廷队世界杯表现"
        assert body["topic"]["topic_type"] == "user"
        assert "sports" in body["topic"]["category_scope"]
        assert body["task"]["task_type"] == "topic_tracking"

        topics = client.get(f"/api/topics?user_id={user_id}&limit=20")
        assert any(item["title"] == "阿根廷队世界杯表现" for item in topics.json()["items"])

        topic_agent = client.app.state.services["topic_agent"]
        original_ingestion = topic_agent.native_ingestion
        topic_agent.native_ingestion = None
        try:
            pending = client.post(
                "/api/chat",
                json={
                    "conversation_id": "topic_agent_conv",
                    "user_id": user_id,
                    "message": "创建一个新的长期专题任务",
                },
            )
            assert pending.status_code == 200
            assert pending.json()["context_relation"] == "topic_create_pending"
            chat = client.post(
                "/api/chat",
                json={
                    "conversation_id": "topic_agent_conv",
                    "user_id": user_id,
                    "message": "阿根廷队世界杯表现",
                },
            )
        finally:
            topic_agent.native_ingestion = original_ingestion
        assert chat.status_code == 200
        payload = chat.json()
        assert payload["context_relation"] == "topic_agent_created"
        assert "已创建主题「阿根廷队世界杯表现」" in payload["answer"]
        assert payload["focus_object"]["text"] == "阿根廷队世界杯表现"
