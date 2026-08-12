from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "personal_news_agent" / "static"


@pytest.mark.parametrize("name", ["landing.html", "auth.html", "index.html", "home.html", "mobile.html"])
def test_frontend_assets_and_navigation_do_not_escape_reverse_proxy_prefix(name: str):
    source = (STATIC_DIR / name).read_text(encoding="utf-8")
    assert 'href="/' not in source
    assert 'src="/' not in source


def test_landing_page_exposes_product_story_and_company_contact():
    landing = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "styles.css?v=20260812-landing-10" in landing
    assert "landing.js?v=20260812-landing-10" in landing
    assert "PERSONAL NEWS AGENT · ALWAYS LEARNING" in landing
    assert "我们持续追踪" in landing
    assert "主动回来找你" in landing
    assert "每个判断有证据" in landing
    assert "它持续扫描" not in landing
    assert "PRODUCT DEMO" in landing
    assert "signal-ribbon" in landing
    assert "trust-proof-grid" in landing
    assert "热点自动追踪" in landing
    assert "事件深度分析" in landing
    assert "事件真伪核查" in landing
    assert "关联事件图谱" in landing
    assert "learning-loop" in landing
    assert "金证优智 · KingdomAI" in landing
    assert "深圳市南山区科技园高新南五道9号" in landing
    assert 'href="mailto:service@kingdomai.com"' in landing
    assert 'href="auth"' in landing
    assert 'href="web"' in landing
    assert "body.landing-shell" in styles
    assert '"PingFang SC"' in styles
    assert ".landing-footer" in styles
    script = (STATIC_DIR / "landing.js").read_text(encoding="utf-8")
    assert "data-preview-key" in landing
    assert "aria-selected" in script
    assert "scenarios" in script
    assert "data-preview-prompt" in landing
    assert "data-preview-step-title" in landing
    assert "tracking-viz" in script
    assert "timeline-viz" in script
    assert "factcheck-viz" in script
    assert "data-graph-node" in script
    assert "演示数据" in script


def test_shared_client_preserves_pna_prefix_for_api_requests():
    source = (STATIC_DIR / "shared.js").read_text(encoding="utf-8")
    assert 'const prefix = "/pna"' in source
    assert "fetch(appUrl(path)" in source
    assert "timeoutMs: 15000" in source
    assert "controller.abort()" in source
    assert "短信服务响应超时" in source
    assert "button.disabled = true;\n      if (submit) submit.disabled = true;" not in source
    assert "你仍可点击发送重试" in source
    home = (STATIC_DIR / "home.html").read_text(encoding="utf-8")
    auth = (STATIC_DIR / "auth.html").read_text(encoding="utf-8")
    auth_script = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
    mobile_script = (STATIC_DIR / "mobile.js").read_text(encoding="utf-8")
    assert "home.js?v=20260812-chat-workspace-1" in home
    assert "shared.js?v=20260810-phone-controls-2" in auth
    assert "ensureRegistrationChallenge(form, status)" in auth_script
    assert "ensureRegistrationChallenge(form, status)" in mobile_script
    assert "function ensureRegistrationChallenge" in source


def test_server_templates_keep_nginx_and_web_port_aligned():
    runtime_env = (ROOT / "deploy" / "env.ext.server.example").read_text(encoding="utf-8")
    nginx = (ROOT / "deploy" / "nginx" / "personal-news-location.conf.example").read_text(encoding="utf-8")
    assert 'PNA_WEB_PORT="22053"' in runtime_env
    assert "location /pna/" in nginx
    assert "proxy_pass http://127.0.0.1:22053/;" in nginx
    assert "X-Forwarded-Prefix /pna" in nginx


def test_html_routes_disable_cache_and_expose_frontend_revision():
    routes = (ROOT / "personal_news_agent" / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'FRONTEND_REVISION = "20260812-chat-workspace-1"' in routes
    assert '"Cache-Control": "no-store, max-age=0"' in routes
    assert '"frontend_revision": FRONTEND_REVISION' in routes


def test_console_theme_has_dark_drawers_readable_content_and_responsive_rails():
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    home = (STATIC_DIR / "home.html").read_text(encoding="utf-8")
    assert "styles.css?v=20260812-chat-workspace-1" in home
    assert ".console-shell .agent-drawer" in styles
    assert "background: rgba(10, 28, 45, 0.96)" in styles
    assert ".console-shell .assistant-markdown h3" in styles
    assert ".console-shell .chat-event strong" in styles
    assert "@media (max-width: 1380px)" in styles
    assert "position: sticky" in styles


def test_topic_rail_is_compact_and_exposes_signal_context():
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    web = (STATIC_DIR / "web.js").read_text(encoding="utf-8")

    assert "grid-template-columns: 270px minmax(0, 1fr) 304px" in styles
    assert "grid-template-columns: 248px minmax(0, 1fr)" in styles
    assert "align-content: start" in styles
    assert "grid-auto-rows: max-content" in styles
    assert "display: -webkit-box" in styles
    assert "rail-tooltip" not in styles
    assert "compactRecommendationReason(item)" in web
    assert "长期关注" in web
    assert "点击进入专题对话与持续追踪" in web
    assert "<em>${escapeHtml(detail)}</em>" in web


def test_chat_console_uses_harness_trace_compact_controls_and_latest_message_layout():
    home = (STATIC_DIR / "home.html").read_text(encoding="utf-8")
    shared = (STATIC_DIR / "shared.js").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    web = (STATIC_DIR / "web.js").read_text(encoding="utf-8")

    assert home.count("legacy-agent-drawer") == 2
    assert ".legacy-agent-drawer" in styles
    assert "display: none !important" in styles
    assert ".messages.chat-stream > .chat-turn:first-child" in styles
    assert "margin-top: auto;" in styles
    assert "grid-template-columns: auto 168px" in styles
    assert ".console-shell .turn-actions button" in styles
    turn_actions = shared.split("function turnActionsHtml", 1)[1].split("document.addEventListener", 1)[0]
    assert "mark-related" not in turn_actions
    assert "mark-unrelated" not in turn_actions
    assert "重新编辑" in turn_actions
    assert 'data-action="changes"' in home
    assert 'data-action="deep-dive-chat"' in home
    assert 'data-action="make-task"' not in home
    assert "conversation-command-bar" in home
    assert "topic-card-skeleton" in home
    assert 'aria-busy="true"' in home
    assert 'target.setAttribute("aria-busy", "false")' in web
    assert 'target.setAttribute("aria-label", "关注专题")' in web
    assert 'target.dataset.loaded = "true"' in web
    assert 'target.dataset.recommendationsLoading = "true"' in web
    assert "const TOPIC_REFRESH_INTERVAL_MS = 180_000" in web
    assert "TOPIC_RECOMMENDATION_RETENTION_MS = 30 * 60_000" in web
    assert "stabilizeRecommendedTopics" in web
    assert "单源待确认" in web
    assert "startTopicAutoRefresh()" in web
    assert 'loadTopics({ quiet: true })' in web
    assert 'target.addEventListener("click", async (event) =>' in web
    assert "await selectTopicCard(button)" in web
    assert "当前对话主题已确定" not in web.split("function bindTopicCards", 1)[1].split("function mergeTopics", 1)[0]
    assert "data-topic-refresh-status" in home
    assert "grid-template-rows: auto auto minmax(0, 1fr) auto auto" in styles
    assert "本轮过程" in shared
    assert "assistantIdentityHtml" in shared
    assert "mountAssistantSections" in shared
    assert ".assistant-markdown > .answer-section" in styles
    assert "mergePublicExecutionTrace" in shared
    assert "upsertResearchTrace" in shared
    assert 'role="listitem"' in shared
    assert "codeLines.join" in shared
    assert 'html += "<ol>"' in shared
    assert "static/vendor/mermaid.min.js" in home
    assert "mountMermaidDiagrams" in shared
    assert "openMermaidViewer" in shared
    assert "mermaidGraphContext" in shared
    assert "data-mermaid-action=\"factcheck\"" in shared
    assert ".mermaid-viewer-dialog" in styles
    assert ".factcheck-workbench" in styles
    assert 'name: "map"' in shared
    assert "生成事件图谱" in shared
    assert 'language.toLowerCase() === "mermaid"' in shared
    assert "function sanitizeMermaidSource" in shared
    assert "normalizeRelatedResearchSteps" in shared
    assert "这是可核验的执行记录，不是隐藏思维链" in shared
    assert ".related-research-path" in styles
    assert "未公布举办地" not in shared
    assert "click\\s+" in shared
    assert ".chat-mermaid-canvas" in styles
    assert "本地新闻引擎 --" in home
    assert "外部搜索工具 --" in home
    assert "ES --" not in home
    assert "MySQL --" not in home


def test_chat_web_search_is_enabled_by_default_but_remains_user_controllable():
    shared = (STATIC_DIR / "shared.js").read_text(encoding="utf-8")
    home = (STATIC_DIR / "home.html").read_text(encoding="utf-8")

    assert "savedWebSearchPreference === null ? true" in shared
    assert "localStorage.setItem(webSearchPreferenceKey()" in shared
    assert home.count("data-web-search-toggle checked") == 2


def test_desktop_chat_clears_submitted_text_and_uses_horizontal_execution_trace():
    web = (STATIC_DIR / "web.js").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    submit_handler = web.split('document.querySelector("#chatForm")', 1)[1].split(
        'document.querySelector("#taskForm")', 1
    )[0]
    assert submit_handler.index('input.value = "";') < submit_handler.index(
        "await handleAssistantInput(message)"
    )
    assert "if (!input.value)" in submit_handler
    assert submit_handler.index("await handleAssistantInput(message)") < submit_handler.index(
        "await loadTopics()"
    )

    desktop_workspace = styles.split("@media (min-width: 1381px)", 1)[1]
    assert "grid-template-columns: 248px minmax(0, 1fr) 280px" in desktop_workspace
    assert "height: calc(100vh - 92px)" in desktop_workspace
    assert ".console-shell .research-trace" in desktop_workspace
    assert "display: flex" in desktop_workspace
    assert "overflow-x: auto" in desktop_workspace


def test_chat_model_selector_is_logical_and_sent_with_each_chat_request():
    home = (STATIC_DIR / "home.html").read_text(encoding="utf-8")
    shared = (STATIC_DIR / "shared.js").read_text(encoding="utf-8")
    web = (STATIC_DIR / "web.js").read_text(encoding="utf-8")
    mobile = (STATIC_DIR / "mobile.js").read_text(encoding="utf-8")

    assert home.count("data-chat-model-select") == 2
    assert "元融大模型" in home
    assert "Qwen 3.6" in home
    assert "DeepSeek V4 Flash" in home
    assert 'const DEFAULT_CHAT_MODEL_KEY = "yuanrong-personal-assistant"' in shared
    assert "model_key: chatContext.model_key || getChatModelKey()" in shared
    assert "provider_model" not in shared.split("async function loadOnboardingOptions", 1)[1].split("async function loadProfileIntoForm", 1)[0]
    assert "model_key: getChatModelKey()" in web
    assert "model_key: getChatModelKey()" in mobile


def test_first_run_onboarding_opens_automatically_and_topics_come_from_real_state():
    home = (STATIC_DIR / "home.html").read_text(encoding="utf-8")
    shared = (STATIC_DIR / "shared.js").read_text(encoding="utf-8")
    web = (STATIC_DIR / "web.js").read_text(encoding="utf-8")
    mobile = (STATIC_DIR / "mobile.js").read_text(encoding="utf-8")

    assert "data-onboarding-intro" in home
    assert "initializeUserProfileState()" in web
    assert "data.profile?.onboarding_completed" in web
    assert "dialog.showModal()" in web
    assert 'dataset.firstRun === "true"' in web
    assert "initializeMobileProfileState()" in mobile
    assert "showOnboardingForm()" in mobile
    assert "const disabled = item.implemented" in shared
    assert "aria-disabled" in shared

    web_bootstrap = web.split("const bootstrapTopics = [", 1)[1].split("];", 1)[0]
    mobile_bootstrap = mobile.split("const mobileBootstrapTopics = [", 1)[1].split("];", 1)[0]
    assert 'topic_type: "user"' not in web_bootstrap
    assert 'topic_type: "user"' not in mobile_bootstrap
    assert web_bootstrap.count('topic_type: "system"') == 2
    assert mobile_bootstrap.count('topic_type: "system"') == 2
    assert "composeTopicItems(persisted, stableRecommendations)" in web
    assert "return [...userTopics, ...recommended, ...systemTopics, ...bootstrapTopics]" in web
    assert "composeMobileTopicItems(persisted, recommended.items || [])" in mobile
    assert "return [...userTopics, ...recommended, ...systemTopics, ...mobileBootstrapTopics]" in mobile
    assert "展示 CC 如何" not in shared
    assert "展示 Agent 如何锚定语境" in shared
