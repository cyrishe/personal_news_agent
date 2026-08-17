from __future__ import annotations

from typing import Any

from claude_code_backend import LocalAgentService

from personal_news_agent.config import Settings
from personal_news_agent.everyday import EverydayCapabilityService
from personal_news_agent.services.auth import AuthService
from personal_news_agent.services.api_keys import ApiKeyService
from personal_news_agent.services.chat import NewsChatService
from personal_news_agent.services.cc_runtime import CCRuntimeOrchestrator
from personal_news_agent.services.crawl import CrawlScheduler
from personal_news_agent.services.content_moderation import TextModerationPlusService
from personal_news_agent.services.conversation_audit import ConversationAuditLogger
from personal_news_agent.services.deep_dive import DeepDiveService
from personal_news_agent.services.events import EventDiscoveryService
from personal_news_agent.services.factcheck import FactCheckService
from personal_news_agent.services.llm import LLMClient
from personal_news_agent.services.model_config import public_model_options
from personal_news_agent.services.native_ingestion import NativeSearchIngestionService
from personal_news_agent.services.onboarding import OnboardingService
from personal_news_agent.services.personalization import PersonalizationService
from personal_news_agent.services.reports import ReportGenerationService
from personal_news_agent.services.search import UnifiedSearchService, external_provider_from_settings
from personal_news_agent.services.search_index import ArticleSearchIndex, ElasticsearchArticleIndex
from personal_news_agent.services.source_registry import SourceRegistryService
from personal_news_agent.services.store import NewsStore
from personal_news_agent.services.tasks import ScheduledTaskService
from personal_news_agent.services.topic_agent import TopicAgentService
from personal_news_agent.services.topic_extraction import TopicExtractionService
from personal_news_agent.services.topic_summary import TopicSummaryService
from personal_news_agent.services.topic_views import TopicViewService
from personal_news_agent.services.trending_topics import TrendingTopicService
from personal_news_agent.services.url_store import CrawlUrlStore, MySQLCrawlUrlStore
from personal_news_agent.skills.registry import build_default_registry


def build_services(settings: Settings) -> dict[str, Any]:
    registry = SourceRegistryService(settings.sources_path)
    store = NewsStore(settings.sqlite_path)
    search_index = ElasticsearchArticleIndex.from_settings(settings) or ArticleSearchIndex()
    url_store = MySQLCrawlUrlStore.from_settings(settings) or CrawlUrlStore()
    search_service = UnifiedSearchService(store, registry, external_provider_from_settings(settings), search_index)
    events = EventDiscoveryService(store)
    native_ingestion = NativeSearchIngestionService(registry, store, url_store, search_index)
    topic_views = TopicViewService(store, search_service)
    deep_dive = DeepDiveService(search_service)
    local_agent = LocalAgentService.from_app_settings(settings)
    llm_client = LLMClient(settings)
    everyday_capabilities = EverydayCapabilityService.from_settings(settings)
    cc_runtime = CCRuntimeOrchestrator(
        store,
        search_service,
        settings,
        everyday_capabilities=everyday_capabilities,
    )
    reports = ReportGenerationService(store, search_service, local_agent=local_agent)
    factcheck = FactCheckService(
        store,
        search_service,
        local_agent=local_agent,
        llm_client=llm_client,
        cc_runtime=cc_runtime,
    )
    topic_summary = TopicSummaryService(store, search_service)
    trending_topics = TrendingTopicService(
        store,
        llm=llm_client,
        cc_runtime=cc_runtime,
        cache_minutes=max(1, (settings.trending_topic_refresh_seconds + 59) // 60),
    )
    tasks = ScheduledTaskService(
        store,
        reports,
        search_service=search_service,
        native_ingestion=native_ingestion,
        llm_client=llm_client,
        cc_runtime=cc_runtime,
    )
    topic_agent = TopicAgentService(store, tasks, topic_views=topic_views, native_ingestion=native_ingestion)
    content_moderation = None
    if settings.content_moderation_enabled:
        content_moderation = TextModerationPlusService(
            access_key_id=settings.aliyun_access_key_id,
            access_key_secret=settings.aliyun_access_key_secret,
            endpoint=settings.content_moderation_endpoint,
            query_service=settings.content_moderation_query_service,
            fail_open=settings.content_moderation_fail_open,
        )
    conversation_audit = ConversationAuditLogger(
        settings.conversation_audit_log_dir,
        enabled=settings.conversation_audit_log_enabled,
        retention_days=settings.conversation_audit_log_retention_days,
    )
    topic_extraction = TopicExtractionService(store)

    skill_registry = build_default_registry()
    services: dict[str, Any] = {

        "registry": registry,
        "store": store,
        "url_store": url_store,
        "search_index": search_index,
        "search": search_service,
        "native_ingestion": native_ingestion,
        "topic_views": topic_views,
        "deep_dive": deep_dive,
        "events": events,
        "auth": AuthService(store, settings),
        "api_keys": ApiKeyService(
            store,
            rate_limit_per_minute=settings.api_key_rate_limit_per_minute,
        ),
        "onboarding": OnboardingService(store, settings),
        "feed": PersonalizationService(store, registry),
        "model_options": public_model_options,
        "reports": reports,
        "factcheck": factcheck,
        "topic_summary": topic_summary,
        "trending_topics": trending_topics,
        "tasks": tasks,
        "topic_agent": topic_agent,
        "topic_extraction": topic_extraction,
        "content_moderation": content_moderation,
        "conversation_audit": conversation_audit,
        "local_agent": local_agent,
        "cc_runtime": cc_runtime,
        "everyday_capabilities": everyday_capabilities,
        "skill_registry": skill_registry,
        "crawl": CrawlScheduler(registry, store, url_store, search_index),
    }
    chat = NewsChatService(
        store,
        search_service,
        native_ingestion=native_ingestion,
        deep_dive=deep_dive,
        topic_views=topic_views,
        topic_agent=topic_agent,
        scheduled_tasks=tasks,
        content_moderation=content_moderation,
        local_agent=local_agent,
        cc_runtime=cc_runtime,
        skill_registry=skill_registry,
        services=services,
    )
    services["chat"] = chat
    return services
