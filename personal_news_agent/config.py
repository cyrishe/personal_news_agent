from __future__ import annotations

import os
from dataclasses import dataclass
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
    external_search_provider: str = os.getenv("EXTERNAL_SEARCH_PROVIDER", "none")
    bing_search_key: str | None = os.getenv("BING_SEARCH_KEY")
    bing_search_endpoint: str = os.getenv("BING_SEARCH_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search")
    http_verify_ssl: bool = os.getenv("PNA_HTTP_VERIFY_SSL", "0") == "1"
    llm_endpoint: str | None = os.getenv("PNA_LLM_ENDPOINT") or os.getenv("LLM_ENDPOINT")
    llm_key: str | None = os.getenv("PNA_LLM_KEY") or os.getenv("LLM_KEY")
    llm_default_model: str = os.getenv("PNA_LLM_DEFAULT_MODEL", "yuanrong-personal-assistant")
    llm_timeout_seconds: int = int(os.getenv("PNA_LLM_TIMEOUT_SECONDS") or os.getenv("LLM_CLIENT_TIMEOUT_SECONDS", "120"))
    stock_agent_db_url: str | None = os.getenv("PNA_USER_DB_URL") or os.getenv("SIMPLE_BI_PLATFORM_DB_URL")
    realname_provider: str = os.getenv("PNA_REALNAME_PROVIDER", "mock")
    realname_mock_enabled: bool = os.getenv("PNA_REALNAME_MOCK_ENABLED", "1") == "1"
    aliyun_access_key_id: str | None = (
        os.getenv("ALIYUN_ACCESS_KEY_ID")
        or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
        or os.getenv("AccessKeyID")
    )
    aliyun_access_key_secret: str | None = (
        os.getenv("ALIYUN_ACCESS_KEY_SECRET")
        or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        or os.getenv("AccessKeySecret")
    )
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


settings = Settings()
