# Personal News Agent

聚焦版个人资讯助手 MVP，按 `/Users/chenghe/Downloads/personal_news_assistant_focused_architecture.md` 实现第一版主链路：

```text
重点板块源配置
→ 抓取/搜索统一接口
→ 热点事件聚类
→ 个性化资讯流
→ 新闻多轮深挖
→ 时间线专题报告
→ 每日/每周摘要任务
```

## 已支持范围

- 7 个一级板块：经济、科技、汽车、游戏、动漫、娱乐、体育。
- `sources.yaml` 统一管理 28 个首批核心源/搜索兜底源。
- 当前源策略：22 个源/section 主动抓取通过 smoke；7 个反爬、授权或正文边界更重的源设置为 search-only，通过外部搜索 domain filter 参与补充。
- FastAPI + SQLite MVP，启动时初始化表结构、同步 source/section 配置。
- 注册后有正式初始化流程：年龄、性别、星座、兴趣板块、关注词、模型选择，并生成用户助手提示词。
- 用户/初始化相关表使用 `pna_` 前缀；MySQL/SQLite 建表和删表脚本在 `sql/`。
- 通用 `ListPageAdapter` 和 `ArticleFetchService`，支持列表页链接抽取和文章正文抽取。
- `UnifiedSearchService`：本地库检索 + 外部搜索 provider 抽象 + domain filter。
- `EventDiscoveryService`：按 category 生成轻量热点事件和 `hot_score`。
- `PersonalizationService`：基于用户板块/关键词/负向词生成资讯流和推荐理由。
- `NewsChatService`：维护 conversation turn，支持“第二条展开说说”这类 follow-up。
- `ReportGenerationService`：生成包含时间线、主体、看点、来源和不确定性说明的报告。
- `ScheduledTaskService`：创建每日/每周摘要任务，并运行生成 report 记录。
- `/` 提供一个简单 Web 演示页。

默认会写入少量演示文章，保证离线也能演示主链路。真实外部搜索 API 尚未绑定，预留在 `ExternalSearchProvider`。

## 启动

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
uvicorn personal_news_agent.app:app --reload --port 8000
```

推荐先把 Python 环境和缓存放到外置盘：

```bash
./scripts/setup_ext_env.sh
source .env.ext
${PERSONAL_NEWS_VENV}/bin/uvicorn personal_news_agent.app:app --reload --port 8000
```

脚本默认使用 `/Volumes/ext/venvs/personal_news_agent`、`/Volumes/ext/.cache/pip`、`/Volumes/ext/conda_pkgs`、`/Volumes/ext/conda_envs`。如果你的挂载点确实是 `/Volumn/ext`，先设置：

```bash
export PERSONAL_NEWS_EXT_ROOT=/Volumn/ext
```

普通启动：

```text
http://127.0.0.1:8000/web
http://127.0.0.1:8000/mobile
```

## 前端

- `/web`：桌面 Web 工作台，包含注册、登录、个人配置，以及五组主界面原型。
- `/mobile`：移动端适配页面，信息流和追问优先，注册/登录/个人配置折叠展示。
- `/` 默认进入 Web 版。

## 源管理、抓取与个性化推送

源配置以 `sources.yaml` 为准。每个源会解析出分类、标签、地域、语言、可信度、抓取间隔和 section 状态。没有显式配置 `tags` 时，会从分类、源类型和域名自动生成基础标签。
当前抓取间隔会被限制在 10-20 分钟；MySQL 表 `pna_crawl_urls` 负责管理 section URL、文章 URL、下一次抓取时间、错误次数和内容更新状态。SQLite 只作为本地开发 fallback。

常用接口：

```bash
curl http://127.0.0.1:8000/api/sources
curl http://127.0.0.1:8000/api/sources/summary
curl 'http://127.0.0.1:8000/api/crawl/due?category=tech&limit=5'
curl 'http://127.0.0.1:8000/api/crawl/urls/due?category=tech&url_type=article&limit=10'
curl 'http://127.0.0.1:8000/api/feed?user_id=default&limit=10'
```

触发到期抓取：

```bash
curl -X POST http://127.0.0.1:8000/api/crawl/due \
  -H 'content-type: application/json' \
  -d '{"category":"tech","limit":5,"per_section_limit":10,"fetch_articles":1}'
```

测试工具：

```bash
python3 scripts/setup_elasticsearch.py
python3 scripts/start_elasticsearch.py
python3 scripts/check_elasticsearch.py
python3 scripts/audit_sources.py --limit-due 20 --output source_audit_results.json
python3 scripts/run_due_crawl.py --category tech --limit 5 --plan-only --output due_crawl_plan.json
python3 scripts/run_due_crawl.py --category tech --limit 5 --fetch-articles 1 --output due_crawl_results.json
python3 scripts/reindex_elasticsearch.py --limit 500
python3 scripts/audit_source_search.py --query 张雪机车 --category sports --output source_search_audit_zhangxue_sports.json
python3 scripts/ingest_native_search.py --query 张雪机车 --category sports --max-results 10 --fetch-articles 8 --output native_search_ingest_zhangxue_sports.json
python3 scripts/deep_dive.py '俄乌战争 农作物' --category politics --output deep_dive_results.json
python3 scripts/preview_feed.py \
  --user-id sports_preview \
  --preferred-category sports \
  --interest NBA \
  --self-description '关心时政 体育 NBA' \
  --limit 5 \
  --output feed_preview_results.json
```

本地 ES runtime 默认安装到 `${PERSONAL_NEWS_EXT_ROOT:-/Volumes/ext}/personal_news_agent/runtime/elasticsearch`，包含数据、日志、pid 和官方自带 JDK。停止：

```bash
python3 scripts/stop_elasticsearch.py
```

个性化信息流会综合：

- 用户 profile：关注分类、自我描述、关注词、负向词。
- 源 metadata：标签、分类、可信度、优先级、抓取新鲜度。
- 文章内容：标题、摘要、正文关键词。

返回的 feed item 包含 `recommend_reason`、`source_tags`、`matched_profile_terms`，用于解释为什么推送。
多分类用户画像会先按偏好分类补召回候选，再做排序与覆盖，避免同一类新闻占满整条信息流。

Web 主界面原型：

- `兴趣对话`
- `任务编排`
- `专题空间`
- `事件地图`
- `随问随报`

## 注册、初始化与实名手机认证

本地注册接口：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'content-type: application/json' \
  -d '{
    "username":"demo_user",
    "password":"123456",
    "confirm_password":"123456",
    "real_name":"张三",
    "mobile":"13800138000"
  }'
```

实名手机认证当前抽象为 provider：

- `mock`：本地演示，只做格式校验。
- `aliyun`：阿里云手机号二要素核验 `Mobile2MetaVerify`，校验姓名和手机号一致性。
- `tencent`：正式环境可接腾讯云手机号三要素核验。

检查状态：

```bash
curl http://127.0.0.1:8000/api/auth/realname/status
```

阿里云手机号二要素核验使用 `Mobile2MetaVerify`，只校验姓名和手机号一致性。正式测试需要在 `.env` 中提供：

```bash
PNA_REALNAME_PROVIDER=aliyun
ALIYUN_ACCESS_KEY_ID=...
ALIYUN_ACCESS_KEY_SECRET=...
ALIYUN_CLOUDAUTH_ENDPOINT=cloudauth.aliyuncs.com
ALIYUN_REGION_ID=cn-beijing
PNA_REALNAME_TEST_NAME=...
PNA_REALNAME_TEST_MOBILE=...
```

兼容阿里云官方环境变量 `ALIBABA_CLOUD_ACCESS_KEY_ID` / `ALIBABA_CLOUD_ACCESS_KEY_SECRET`，以及本地别名 `AccessKeyID` / `AccessKeySecret`。

测试当前配置：

```bash
python3 scripts/test_realname.py --provider aliyun
```

腾讯云手机号二要素核验使用 `CheckPhoneAndName`，只校验姓名和手机号一致性。正式测试需要在 `.env` 中提供：

```bash
PNA_REALNAME_PROVIDER=tencent
TENCENT_SECRET_ID=...
TENCENT_SECRET_KEY=...
PNA_REALNAME_TEST_NAME=...
PNA_REALNAME_TEST_MOBILE=...
```

兼容旧键名：`SECRETID` 可作为 `TENCENT_SECRET_ID`，`TEST_NAME` 和 `TEST_NUMBER` 可作为测试姓名/手机号。`APPID` 不是该云 API 3.0 请求的签名密钥。

测试当前配置：

```bash
python3 scripts/test_realname.py --provider tencent
```

微信登录本阶段先不做，相关后端接口保留但前端不展示。

初始化接口：

```bash
curl http://127.0.0.1:8000/api/onboarding/options
curl http://127.0.0.1:8000/api/models
```

```bash
curl -X POST http://127.0.0.1:8000/api/onboarding/complete \
  -H 'content-type: application/json' \
  -d '{
    "user_id":"usr_xxx",
    "display_name":"小明",
    "self_description":"互联网从业者，关注 AI 产品、游戏和新能源汽车。",
    "age":28,
    "gender":"男",
    "zodiac":"天秤座",
    "preferred_categories":["sports","entertainment","politics"],
    "watch_keywords":["NBA","OpenAI"],
    "negative_keywords":["短线荐股"],
    "model_key":"yuanrong-personal-assistant",
    "output_style":"休闲"
  }'
```

个人配置可通过 `GET /api/profile?user_id=usr_xxx` 读取，登录后前端的“个人配置”入口会回填并允许再次保存。播报风格预设包括：休闲、娱乐、正式、幽默、二次元、时间线优先、深度解释。生成个性化提示词的具体文案后续可以替换 `OnboardingService._build_assistant_prompt` 的框架。

模型选择：

- `yuanrong-personal-assistant`：展示名“元融个人助理大模型”，底层 `qwen3.5-plus`，绑定一个很短的固定系统提示词。
- `qwen3.5-flash`：直接接入的轻量模型。
- `qwen3.5-plus`：直接接入的增强模型。

初始化完成后会生成个性化 assistant prompt，并保存到 `pna_users.assistant_prompt`。

数据库脚本：

```bash
mysql < sql/pna_schema_mysql.sql
mysql < sql/upgrade_pna_realname_mysql.sql
mysql < sql/upgrade_pna_onboarding_profile_mysql.sql
mysql < sql/drop_pna_tables_mysql.sql
sqlite3 personal_news.db < sql/pna_schema_sqlite.sql
sqlite3 personal_news.db < sql/drop_pna_tables_sqlite.sql
```

也可以让脚本读取当前目录 `.env`：

```bash
python3 scripts/apply_pna_schema.py --target mysql
python3 scripts/apply_pna_schema.py --target mysql --drop
python3 scripts/apply_pna_schema.py --target sqlite
```

真实连接和模型配置放当前目录 `.env`。示例见 `.env.example`，不要提交真实 `.env`。

## 搜索后端与 ES

MVP 默认使用 SQLite FTS5 作为本地全文索引，已覆盖本地新闻库检索和 category fallback。外部搜索通过 provider 抽象接入：

```bash
export EXTERNAL_SEARCH_PROVIDER=bing
export BING_SEARCH_KEY=...
```

检查后端：

```bash
curl http://127.0.0.1:8000/api/news/search/backend
```

搜索架构：ES 负责文章召回检索，`/api/news/search` 支持 `category_scope` 和 `source_scope` 做定向搜索；SQLite FTS 只作为 ES 未配置或不可用时的本地 fallback。接入 ES 后可运行 `scripts/reindex_elasticsearch.py` 把已有文章写入索引。

深度挖掘：`/api/news/deep-dive` 会先做起始召回，再从证据中抽取关键词和实体，生成垂直扩展查询与水平扩展查询并继续召回。LLM planner 的接入点已保留，后续可以由模型决定新事件、新主体、新搜索词和停止条件。

## 常用 API

```bash
curl http://127.0.0.1:8000/api/health
curl 'http://127.0.0.1:8000/api/feed?category=tech&limit=5'
curl 'http://127.0.0.1:8000/api/events?category=auto'
curl http://127.0.0.1:8000/api/news/search/backend
```

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"conversation_id":"demo","message":"今天游戏圈有什么新闻？"}'

curl -X POST http://127.0.0.1:8000/api/chat \
  -H 'content-type: application/json' \
  -d '{"conversation_id":"demo","message":"第二条展开说说。"}'

curl -X POST http://127.0.0.1:8000/api/reports \
  -H 'content-type: application/json' \
  -d '{"topic":"新能源汽车价格战","category_scope":["auto","economy"],"time_range":"30d"}'
```

## 测试

```bash
python3 -m compileall personal_news_agent
pytest
```

## 抓取与源 smoke

```bash
python3 scripts/smoke_sources.py --limit-sources 16 --links 5 --fetch
python3 scripts/crawl_all.py --category tech --category game --per-section-limit 10 --fetch-articles 1
```

当前验证记录：

```text
python3 scripts/smoke_sources.py --links 5 --fetch
=> 29 total / 22 ok / 0 weak / 0 error / 7 skipped(search-only)

python3 scripts/crawl_all.py --per-section-limit 2 --fetch-articles 1
=> 7 categories / 22 sections / 22 saved_articles / 0 errors
```
