from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")
EXT_ROOT = Path(os.getenv("PERSONAL_NEWS_EXT_ROOT", "/Volumes/ext"))


def _llm_endpoint() -> str | None:
    return os.getenv("PNA_LLM_ENDPOINT") or "https://api.deepseek.com"


def _llm_key() -> str | None:
    return os.getenv("PNA_LLM_KEY") or None


def _aliyun_credentials() -> tuple[str | None, str | None]:
    return os.getenv("AccessKeyID"), os.getenv("AccessKeySecret")


def _aliyun_credentials_configured() -> bool:
    access_key_id, access_key_secret = _aliyun_credentials()
    return bool(access_key_id and access_key_secret)


def _default_phone_challenge_provider() -> str:
    return "aliyun_pnvs" if _aliyun_credentials_configured() else "disabled"


@dataclass(frozen=True)
class Settings:
    app_name: str = "Personal News Agent"
    database_url: str = os.getenv("PERSONAL_NEWS_DB", f"sqlite:///{BASE_DIR / 'personal_news.db'}")
    sources_path: Path = Path(os.getenv("PERSONAL_NEWS_SOURCES", BASE_DIR / "sources.yaml"))
    seed_demo_data: bool = os.getenv("PERSONAL_NEWS_SEED_DEMO", "1") == "1"
    search_backend: str = os.getenv("PERSONAL_NEWS_SEARCH_BACKEND", "sqlite_fts")
    elasticsearch_url: str | None = os.getenv("ELASTICSEARCH_URL")
    elasticsearch_index: str = os.getenv("PERSONAL_NEWS_ES_INDEX", "personal_news_articles")
    elasticsearch_timeout_seconds: float = float(os.getenv("PERSONAL_NEWS_ES_TIMEOUT_SECONDS", "8"))
    crawl_url_backend: str = os.getenv("PERSONAL_NEWS_CRAWL_URL_BACKEND", "mysql")
    crawl_database_url: str | None = (
        os.getenv("PERSONAL_NEWS_CRAWL_DB_URL")
        or os.getenv("PERSONAL_NEWS_MYSQL_URL")
        or os.getenv("PNA_USER_DB_URL")
        or os.getenv("SIMPLE_BI_PLATFORM_DB_URL")
        or os.getenv("PLATFORM_DB_URL")
    )
    crawl_interval_min_minutes: int = int(os.getenv("PERSONAL_NEWS_CRAWL_INTERVAL_MINUTES", "10"))
    crawl_interval_max_minutes: int = int(os.getenv("PERSONAL_NEWS_CRAWL_INTERVAL_MAX_MINUTES", "20"))
    background_crawl_enabled: bool = (
        os.getenv("PERSONAL_NEWS_BACKGROUND_CRAWL", "1") == "1"
        and os.getenv("PNA_WEB_DISABLE_BACKGROUND_CRAWL", "0") != "1"
    )
    background_crawl_interval_seconds: int = int(os.getenv("PERSONAL_NEWS_BACKGROUND_CRAWL_SECONDS", "10"))
    trending_topic_refresh_seconds: int = int(os.getenv("PERSONAL_NEWS_TRENDING_REFRESH_SECONDS", "180"))
    external_search_provider: str = os.getenv("EXTERNAL_SEARCH_PROVIDER", "none")
    bing_search_key: str | None = os.getenv("BING_SEARCH_KEY")
    bing_search_endpoint: str = os.getenv("BING_SEARCH_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search")
    tavily_api_key: str | None = os.getenv("TAVILY_API_KEY")
    tavily_search_endpoint: str = os.getenv("TAVILY_SEARCH_ENDPOINT", "https://api.tavily.com/search")
    tavily_search_depth: str = os.getenv("TAVILY_SEARCH_DEPTH", "basic")
    tavily_trust_env: bool = os.getenv("TAVILY_TRUST_ENV", "0") == "1"
    http_verify_ssl: bool = os.getenv("PNA_HTTP_VERIFY_SSL", "1") == "1"
    everyday_api_timeout_seconds: float = float(os.getenv("PNA_EVERYDAY_API_TIMEOUT_SECONDS", "10"))
    open_meteo_weather_endpoint: str = os.getenv(
        "PNA_OPEN_METEO_WEATHER_ENDPOINT",
        "https://api.open-meteo.com/v1/forecast",
    )
    open_meteo_geocoding_endpoint: str = os.getenv(
        "PNA_OPEN_METEO_GEOCODING_ENDPOINT",
        "https://geocoding-api.open-meteo.com/v1/search",
    )
    amap_web_key: str | None = field(default=os.getenv("PNA_AMAP_WEB_KEY") or None, repr=False)
    amap_web_endpoint: str = os.getenv("PNA_AMAP_WEB_ENDPOINT", "https://restapi.amap.com").rstrip("/")
    juhe_train_key: str | None = field(
        default=os.getenv("PNA_JUHE_TRAIN_KEY") or os.getenv("PNA_JUHE_API_KEY") or None,
        repr=False,
    )
    juhe_flight_key: str | None = field(
        default=os.getenv("PNA_JUHE_FLIGHT_KEY") or os.getenv("PNA_JUHE_API_KEY") or None,
        repr=False,
    )
    juhe_train_endpoint: str = os.getenv(
        "PNA_JUHE_TRAIN_ENDPOINT",
        "https://apis.juhe.cn/fapigw/train/query",
    )
    juhe_flight_endpoint: str = os.getenv(
        "PNA_JUHE_FLIGHT_ENDPOINT",
        "https://v.juhe.cn/flight_dynamic/query",
    )
    llm_endpoint: str | None = field(default_factory=_llm_endpoint)
    llm_key: str | None = field(default_factory=_llm_key, repr=False)
    llm_model: str = os.getenv("PNA_LLM_MODEL", "deepseek-v4-flash")
    llm_default_model: str = os.getenv("PNA_LLM_DEFAULT_MODEL", "yuanrong-personal-assistant")
    llm_timeout_seconds: int = int(os.getenv("PNA_LLM_TIMEOUT_SECONDS", "120"))
    cc_runtime_enabled: bool = os.getenv("PNA_CC_RUNTIME_ENABLED", "1") == "1"
    cc_runtime_effort: str | None = os.getenv("PNA_CC_RUNTIME_EFFORT") or None
    cc_runtime_max_turns: int = int(os.getenv("PNA_CC_RUNTIME_MAX_TURNS", "6"))
    cc_runtime_max_budget_usd: float | None = (
        float(os.environ["PNA_CC_RUNTIME_MAX_BUDGET_USD"])
        if os.getenv("PNA_CC_RUNTIME_MAX_BUDGET_USD")
        else None
    )
    cc_runtime_timeout_seconds: float = float(os.getenv("PNA_CC_RUNTIME_TIMEOUT_SECONDS", "150"))
    cc_runtime_allow_existing_login: bool = os.getenv("PNA_CC_RUNTIME_ALLOW_EXISTING_LOGIN", "0") == "1"
    cc_runtime_builtin_web_search: bool = os.getenv("PNA_CC_RUNTIME_BUILTIN_WEB_SEARCH", "1") == "1"
    cc_runtime_config_dir: Path = Path(
        os.getenv("PNA_CC_RUNTIME_CONFIG_DIR", str(BASE_DIR / "data" / "cc_runtime"))
    ).expanduser()
    stock_agent_db_url: str | None = os.getenv("PNA_USER_DB_URL") or os.getenv("SIMPLE_BI_PLATFORM_DB_URL")
    phone_challenge_provider: str = os.getenv(
        "PNA_PHONE_CHALLENGE_PROVIDER",
        os.getenv("FIN_AGENT_PHONE_CHALLENGE_PROVIDER", _default_phone_challenge_provider()),
    )
    phone_challenge_secret: str | None = os.getenv("PNA_PHONE_CHALLENGE_SECRET") or os.getenv(
        "FIN_AGENT_PHONE_CHALLENGE_SECRET"
    )
    phone_challenge_mock_enabled: bool = (
        os.getenv(
            "PNA_PHONE_CHALLENGE_MOCK_ENABLED",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_MOCK_ENABLED", "0"),
        )
        == "1"
    )
    phone_challenge_mock_code: str = os.getenv(
        "PNA_PHONE_CHALLENGE_MOCK_CODE",
        os.getenv("FIN_AGENT_PHONE_CHALLENGE_MOCK_CODE", "123456"),
    )
    phone_challenge_ttl_seconds: int = int(
        os.getenv(
            "PNA_PHONE_CHALLENGE_TTL_SECONDS",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_TTL_SECONDS", "600"),
        )
    )
    phone_challenge_resend_seconds: int = int(
        os.getenv(
            "PNA_PHONE_CHALLENGE_RESEND_SECONDS",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_RESEND_SECONDS", "60"),
        )
    )
    phone_challenge_request_timeout_seconds: float = float(
        os.getenv(
            "PNA_PHONE_CHALLENGE_REQUEST_TIMEOUT_SECONDS",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_REQUEST_TIMEOUT_SECONDS", "12"),
        )
    )
    phone_challenge_max_attempts: int = int(
        os.getenv(
            "PNA_PHONE_CHALLENGE_MAX_ATTEMPTS",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_MAX_ATTEMPTS", "5"),
        )
    )
    phone_challenge_rate_window_seconds: int = int(
        os.getenv(
            "PNA_PHONE_CHALLENGE_RATE_WINDOW_SECONDS",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_RATE_WINDOW_SECONDS", "3600"),
        )
    )
    phone_challenge_mobile_rate_limit: int = int(
        os.getenv(
            "PNA_PHONE_CHALLENGE_MOBILE_RATE_LIMIT",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_MOBILE_RATE_LIMIT", "5"),
        )
    )
    phone_challenge_ip_rate_limit: int = int(
        os.getenv(
            "PNA_PHONE_CHALLENGE_IP_RATE_LIMIT",
            os.getenv("FIN_AGENT_PHONE_CHALLENGE_IP_RATE_LIMIT", "20"),
        )
    )
    pnvs_sign_name: str = "速通互联验证码"
    pnvs_template_code: str = "100001"
    pnvs_scheme_name: str = "fin-agent-register"
    pnvs_endpoint: str = os.getenv(
        "PNA_PNVS_ENDPOINT",
        os.getenv("FIN_AGENT_PNVS_ENDPOINT", "dypnsapi.aliyuncs.com"),
    )
    realname_provider: str = os.getenv("PNA_REALNAME_PROVIDER", "mock")
    realname_mock_enabled: bool = os.getenv("PNA_REALNAME_MOCK_ENABLED", "1") == "1"
    aliyun_access_key_id: str | None = _aliyun_credentials()[0]
    aliyun_access_key_secret: str | None = _aliyun_credentials()[1]
    aliyun_cloudauth_endpoint: str = os.getenv("ALIYUN_CLOUDAUTH_ENDPOINT", "cloudauth.aliyuncs.com")
    aliyun_region_id: str = os.getenv("ALIYUN_REGION_ID", "cn-beijing")
    tencent_app_id: str | None = os.getenv("TENCENT_APP_ID") or os.getenv("APPID")
    tencent_secret_id: str | None = os.getenv("TENCENT_SECRET_ID") or os.getenv("SECRETID")
    tencent_secret_key: str | None = os.getenv("TENCENT_SECRET_KEY") or os.getenv("SECRETKEY") or os.getenv("SECRET_KEY")
    tencent_faceid_endpoint: str = os.getenv("TENCENT_FACEID_ENDPOINT", "https://faceid.tencentcloudapi.com")
    realname_test_name: str | None = os.getenv("PNA_REALNAME_TEST_NAME") or os.getenv("TEST_NAME")
    realname_test_mobile: str | None = os.getenv("PNA_REALNAME_TEST_MOBILE") or os.getenv("TEST_NUMBER") or os.getenv("TEST_MOBILE")
    wechat_app_id: str | None = os.getenv("WECHAT_APP_ID")
    wechat_app_secret: str | None = os.getenv("WECHAT_APP_SECRET")
    wechat_redirect_uri: str | None = os.getenv("WECHAT_REDIRECT_URI")
    wechat_login_mode: str = os.getenv("WECHAT_LOGIN_MODE", "website")
    ext_root: Path = EXT_ROOT

    @property
    def sqlite_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("MVP storage expects sqlite:/// database URL")
        return Path(self.database_url.removeprefix("sqlite:///"))

    @property
    def effective_cc_runtime_base_url(self) -> str:
        """CC is pinned to DeepSeek's Anthropic-compatible endpoint."""
        return "https://api.deepseek.com/anthropic"

    @property
    def effective_cc_runtime_auth_token(self) -> str | None:
        """CC and ordinary LLM calls always share PNA_LLM_KEY."""
        return self.llm_key

    @property
    def effective_cc_runtime_api_key(self) -> str | None:
        """There is no independent Anthropic key in the pinned provider contract."""
        return None

    @property
    def effective_runtime_model(self) -> str:
        """All model-backed paths use the application's one provider model."""
        return self.llm_model


settings = Settings()
