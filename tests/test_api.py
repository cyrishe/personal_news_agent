from fastapi.testclient import TestClient
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from personal_news_agent.app import app
from personal_news_agent.config import settings


object.__setattr__(settings, "realname_provider", "mock")


def test_api_health_and_main_routes():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["source_count"] >= 20

        feed = client.get("/api/feed?category=tech&limit=5")
        assert feed.status_code == 200
        assert feed.json()["items"]

        events = client.get("/api/events?category=tech")
        assert events.status_code == 200
        assert events.json()["items"]

        backend = client.get("/api/news/search/backend")
        assert backend.status_code == 200
        assert backend.json()["local_backend"] == "sqlite_fts"

        web = client.get("/web")
        assert web.status_code == 200
        assert "news-console" in web.text
        assert "data-es-status" in web.text
        assert "data-due-urls" in web.text
        assert "深度挖掘" in web.text

        auth = client.get("/auth")
        assert auth.status_code == 200
        assert 'data-auth-mode-target="login"' in auth.text
        assert "手机号实名认证，请用真实姓名对应的手机号" in auth.text

        mobile = client.get("/mobile")
        assert mobile.status_code == 200
        assert "移动端" in mobile.text


def test_auth_register_login_and_realname_status():
    with TestClient(app) as client:
        username = f"api_user_{uuid4().hex[:8]}"
        mobile = _mobile()
        registered = client.post(
            "/api/auth/register",
            json={
                "username": username,
                "password": "123456",
                "confirm_password": "123456",
                "real_name": "张三",
                "mobile": mobile,
            },
        )
        assert registered.status_code == 200
        assert registered.json()["user"]["username"] == username
        assert registered.json()["user"]["mobile"] == mobile[:3] + "****" + mobile[-4:]
        assert registered.json()["session"]["token"]

        login = client.post("/api/auth/login", json={"username": username, "password": "123456"})
        assert login.status_code == 200
        assert login.json()["user"]["username"] == username

        realname = client.get("/api/auth/realname/status")
        assert realname.status_code == 200
        assert realname.json()["provider"] == "mock"

        mismatch = client.post(
            "/api/auth/register",
            json={
                "username": f"bad_{uuid4().hex[:8]}",
                "password": "123456",
                "confirm_password": "654321",
                "real_name": "李四",
                "mobile": _mobile(),
            },
        )
        assert mismatch.status_code == 400


def test_onboarding_generates_profile_prompt_and_model_choice():
    with TestClient(app) as client:
        models = client.get("/api/models")
        assert models.status_code == 200
        assert any(item["key"] == "yuanrong-personal-assistant" for item in models.json()["items"])

        options = client.get("/api/onboarding/options")
        assert options.status_code == 200
        categories = options.json()["categories"]
        assert any(item["key"] == "sports" and item["implemented"] for item in categories)
        assert any(item["key"] == "politics" and item["implemented"] for item in categories)

        username = f"onboarding_{uuid4().hex[:8]}"
        mobile = _mobile()
        registered = client.post(
            "/api/auth/register",
            json={
                "username": username,
                "password": "123456",
                "confirm_password": "123456",
                "real_name": "王五",
                "mobile": mobile,
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
        assert body["model"]["provider_model"] == "qwen3.5-plus"
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


def test_api_chat_report_and_task_flow():
    with TestClient(app) as client:
        task_user_id = f"api_task_user_{uuid4().hex[:8]}"
        due_user_id = f"api_due_user_{uuid4().hex[:8]}"
        turn1 = client.post("/api/chat", json={"conversation_id": "api_conv", "message": "今天汽车圈有什么新闻？"})
        assert turn1.status_code == 200
        assert len(turn1.json()["recommendations"]) >= 2

        turn2 = client.post("/api/chat", json={"conversation_id": "api_conv", "message": "第二条展开说说。"})
        assert turn2.status_code == 200
        assert turn2.json()["focus_object"]["ordinal"] == 2

        report = client.post(
            "/api/reports",
            json={"topic": "新能源汽车价格战", "category_scope": ["auto", "economy"], "time_range": "30d"},
        )
        assert report.status_code == 200
        assert report.json()["timeline"]

        topic_view = client.post(
            "/api/topics/view",
            json={"topic": "新能源汽车价格战", "category_scope": ["auto", "economy"], "max_articles": 8},
        )
        assert topic_view.status_code == 200
        topic_payload = topic_view.json()
        assert topic_payload["event_line"]["items"]
        assert topic_payload["relation_graph"]["nodes"]

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
                "topics": ["张雪机车"],
            },
        )
        assert due_task.status_code == 200
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with client.app.state.services["store"].connect() as conn:
            conn.execute("UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?", (past, due_task.json()["id"]))
        due = client.post("/api/tasks/due/run", json={"user_id": due_user_id, "limit": 3})
        assert due.status_code == 200
        assert due.json()["ran_count"] == 1
