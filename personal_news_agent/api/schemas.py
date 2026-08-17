from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from personal_news_agent.core.models import TimeRange


class SearchRequest(BaseModel):
    query: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    time_range: str | None = "7d"
    max_results: int = Field(default=20, ge=1, le=100)
    allow_web_search: bool = False


class DeepDiveRequest(BaseModel):
    query: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    rounds: int = Field(default=2, ge=1, le=4)
    breadth: int = Field(default=4, ge=1, le=8)
    allow_web_search: bool = False


class RelatedSearchRequest(BaseModel):
    conversation_id: str | None = None
    user_id: str = "default"
    query: str
    topic: str | None = None
    category_scope: list[str] | None = None
    max_queries: int = Field(default=8, ge=3, le=8)
    allow_web_search: bool = True


class NativeSearchIngestRequest(BaseModel):
    query: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    max_results: int = Field(default=20, ge=1, le=100)
    fetch_articles: int = Field(default=10, ge=0, le=50)
    follow_depth: int = Field(default=0, ge=0, le=1)
    follow_limit_per_article: int = Field(default=2, ge=0, le=5)


class TopicViewRequest(BaseModel):
    topic: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    max_articles: int = Field(default=16, ge=1, le=50)


class TopicSummaryRequest(BaseModel):
    user_id: str = "default"
    topic: str
    category_scope: list[str] | None = None
    source_scope: list[str] | None = None
    max_articles: int = Field(default=12, ge=1, le=30)
    use_llm: bool = True
    output_style: str = "结构化专题摘要"
    save_report: bool = True


class TopicCreateRequest(BaseModel):
    user_id: str = "default"
    conversation_id: str | None = None
    text: str | None = None
    title: str | None = None
    topic_type: str = "user"
    category_scope: list[str] | None = None
    schedule: str = "*/20 * * * *"
    refresh_now: bool = True


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    user_id: str = "default"
    message: str
    topic: str | None = None
    category_scope: list[str] | None = None
    use_llm: bool = True
    allow_web_search: bool = True
    model_key: str = Field(default="yuanrong-personal-assistant", min_length=1, max_length=80)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(default="默认 API Key", min_length=1, max_length=80)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiConversationCreateRequest(BaseModel):
    title: str = Field(default="API 对话", min_length=1, max_length=120)


class ApiConversationMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    topic: str | None = Field(default=None, max_length=200)
    category_scope: list[str] = Field(default_factory=list)
    use_llm: bool = True
    allow_web_search: bool = True
    model_key: str = Field(default="yuanrong-personal-assistant", min_length=1, max_length=80)


class ReportRequest(BaseModel):
    user_id: str = "default"
    topic: str
    category_scope: list[str] = []
    time_range: str = "30d"
    report_type: str = "timeline_analysis"


class ProfileRequest(BaseModel):
    user_id: str = "default"
    interests: list[str] = []
    negative_interests: list[str] = []
    preferred_categories: list[str] = []
    preferred_sources: list[str] = []
    output_style: str = "concise"


class FeedbackRequest(BaseModel):
    user_id: str = "default"
    target_type: str
    target_id: str
    feedback_type: str


class TurnRelationRequest(BaseModel):
    user_id: str = "default"
    relation: str


class TaskRequest(BaseModel):
    user_id: str = "default"
    task_type: str
    schedule: str
    category_scope: list[str] = []
    source_scope: list[str] = []
    topics: list[str] = []
    output_style: str | None = None
    raw_task_description: str | None = None
    parsed_workflow: dict = Field(default_factory=dict)
    delivery_channel: str = "in_app"


class ScheduleCommandRequest(BaseModel):
    user_id: str = "default"
    message: str


class DueTasksRequest(BaseModel):
    user_id: str = "default"
    limit: int = Field(default=10, ge=1, le=50)


class TaskEnabledRequest(BaseModel):
    user_id: str = "default"
    enabled: bool = False


class NotificationReadRequest(BaseModel):
    user_id: str = "default"


class DueCrawlRequest(BaseModel):
    category: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    per_section_limit: int = Field(default=10, ge=1, le=50)
    fetch_articles: int = Field(default=1, ge=0, le=10)
    workers: int = Field(default=2, ge=1, le=4)


class RegisterRequest(BaseModel):
    mobile: str = Field(min_length=11, max_length=20)
    challenge_id: str = Field(min_length=8, max_length=96)
    verification_code: str = Field(pattern=r"^[0-9]{6}$")
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class RegistrationCodeRequest(BaseModel):
    mobile: str = Field(min_length=11, max_length=20)


class LoginRequest(BaseModel):
    mobile: str | None = None
    username: str | None = None
    password: str = Field(min_length=1, max_length=128)


class OnboardingRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    user_id: str
    display_name: str | None = None
    self_description: str = Field(default="", max_length=500)
    age: int | None = None
    gender: str = "不透露"
    zodiac: str = "不透露"
    preferred_categories: list[str] = []
    watch_keywords: list[str] = []
    negative_keywords: list[str] = []
    model_key: str = "yuanrong-personal-assistant"
    output_style: str = "简洁分析型"


def parse_range(value: str | None) -> TimeRange | None:
    if not value:
        return None
    if value.endswith("d") and value[:-1].isdigit():
        return TimeRange(days=int(value[:-1]))
    return TimeRange(days=7)


def mask_mobile(value: str) -> str:
    return value[:3] + "****" + value[-4:] if len(value) == 11 else value
