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
python3 scripts/audit_source_search.py --query 机车赛事 --category sports --output source_search_audit_sports.json
python3 scripts/ingest_native_search.py --query 机车赛事 --category sports --max-results 10 --fetch-articles 8 --output native_search_ingest_sports.json
python3 scripts/deep_dive.py '国际局势 大宗商品' --category politics --output deep_dive_results.json
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

### 低并发持续抓取

第一阶段抓取以板块索引页为入口，只展开一层到文章详情页，不递归追踪文章中的链接。当前重点覆盖：

- 体育：央视体育、新浪体育、搜狐体育、虎扑。
- 娱乐：新浪娱乐、搜狐娱乐、凤凰娱乐。
- 财经：新浪财经、凤凰财经、搜狐财经、第一财经、界面新闻。
- 游戏/动漫：游民星空、3DM、17173、游侠网、游民 ACG。
- 数码：中关村在线、太平洋科技。
- 军事：中国军网、中华网军事。

持续 worker 默认使用两个并发槽。每轮按到期时间和 source 优先级取索引页，完成后立即处理下一批；没有到期索引页时短暂等待。每个索引页本身按 10-20 分钟间隔重新到期，因此一轮运行十几分钟也不会重叠启动另一轮。

```bash
source .env.ext
python3 scripts/run_crawl_loop.py \
  --workers 2 \
  --due-limit 20 \
  --per-section-limit 20 \
  --fetch-articles 5
```

生产环境不把爬虫或用户定时任务放入 FastAPI 子线程，而是由 systemd 同时管理 Web、唯一 crawler 和用户级 task runner。服务器项目目录是 `/home/che/cyris/personal_news_agent`，首次部署建议按下面的顺序执行：

```bash
cd /home/che/cyris/personal_news_agent
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# 将 deploy/env.ext.server.example 的 Linux 路径合并到服务器 .env.ext
chmod 600 .env .env.ext
./start.sh preflight
./start.sh install
```

脚本会从自身位置识别项目目录，不依赖 `/data/...` 等旧路径。以后更新代码或环境配置后，直接统一重启：

```bash
./start.sh restart
```

`start.sh` 会校验 `.env`、`.env.ext`、Python 路径、pip 依赖一致性、FastAPI 初始化和 `sources.yaml`，渲染 `deploy/systemd/` 中的 unit 模板，启动服务并检查 `/api/health`。它支持：

```bash
./start.sh start       # 启动全部服务；不带参数时也是 start
./start.sh restart     # 更新 unit、重启全部服务并验证
./start.sh stop        # 停止全部服务
./start.sh status      # 查看三个服务状态
./start.sh logs        # 查看最近日志
./start.sh follow      # 持续跟踪日志
./start.sh health      # 只检查 Web 健康状态
./start.sh preflight   # 只检查配置和 Python 运行环境
```

也可以分别管理后端（Web + 用户定时任务）和抓取进程：

```bash
./start_backend.sh restart
./start_backend.sh status

./start_crawler.sh restart
./start_crawler.sh follow
```

底层安装脚本仍可单独使用；`--no-start` 只更新 systemd unit，不启动服务：

```bash
./scripts/install_systemd_services.sh
./scripts/install_systemd_services.sh --no-start
```

安装后启用 `personal-news.target`，由它管理：

```text
personal-news-web.service
personal-news-crawler.service
personal-news-tasks.service
```

如果需要直接使用 systemctl，常用命令仍然是：

```bash
sudo systemctl restart personal-news.target
sudo systemctl status personal-news-web personal-news-crawler personal-news-tasks
sudo journalctl -u personal-news-crawler -f
sudo journalctl -u personal-news-tasks -f
```

systemd 默认使用当前登录用户；通过 `sudo` 运行安装脚本时使用 `SUDO_USER`。如需指定用户：

```bash
PNA_RUN_USER=pna PNA_RUN_GROUP=pna ./scripts/install_systemd_services.sh
```

Web、crawler 和 task runner 都会加载 `.env.ext`，然后使用 `PERSONAL_NEWS_VENV` 指向的 Python；如果配置了该变量但解释器不存在，预检和服务都会直接报错，避免悄悄回退到依赖不完整的系统 Python。服务器上的绝对路径必须使用 Linux 路径。完整的非敏感模板见 `deploy/env.ext.server.example`，核心路径例如：

```bash
export PERSONAL_NEWS_EXT_ROOT="/home/che/cyris/personal_news_agent/runtime"
export PERSONAL_NEWS_VENV="/home/che/cyris/personal_news_agent/.venv"
export PERSONAL_NEWS_DB="sqlite:////home/che/cyris/personal_news_agent/personal_news.db"
export PNA_WEB_PORT="22053"
```

因此本地 `.env.ext` 的变量名可以保持不变，但 `/Volumes/ext/...` 的变量值不能原样用于服务器。crawler 和 task runner 都只应各运行一个 systemd 实例。可在 `.env.ext` 中调整：

systemd 的 Web unit 会设置 `PNA_WEB_DISABLE_BACKGROUND_CRAWL=1`，避免 Web 内嵌抓取与独立 crawler 重复运行；本地只启动 Web 时仍可用 `PERSONAL_NEWS_BACKGROUND_CRAWL=1` 自动抓取。

通过域名子路径 `/pna/` 部署时，可将 `deploy/nginx/personal-news-location.conf.example` 放入 HTTPS `server` 块。该配置会把 `/pna/` 转发到 `127.0.0.1:22053`；前端静态资源、页面跳转和 API 请求均保留 `/pna` 前缀。修改 `.env.ext` 或 Nginx 后执行：

```bash
./start_backend.sh restart
sudo nginx -t && sudo systemctl reload nginx
curl http://127.0.0.1:22053/api/health
curl https://你的域名/pna/api/health
```

```bash
export PNA_CRAWL_WORKERS=2
export PNA_CRAWL_DUE_LIMIT=20
export PNA_CRAWL_PER_SECTION_LIMIT=20
export PNA_CRAWL_FETCH_ARTICLES=5
export PNA_CRAWL_IDLE_SECONDS=30
export PNA_TOPIC_EXTRACTION_LIMIT=20
export PNA_TASK_DUE_LIMIT=10
export PNA_TASK_IDLE_SECONDS=30
export PNA_PHONE_CHALLENGE_REQUEST_TIMEOUT_SECONDS=12
```

单轮调试仍可使用：

```bash
python3 scripts/run_due_crawl.py --workers 2 --limit 10 --per-section-limit 20 --fetch-articles 5
```

用户定时推送的常驻 runner 可本地单独启动：

```bash
source .env.ext
python3 scripts/run_task_loop.py --limit 10 --idle-seconds 30
```

聊天里输入 `/schedule 帮我定时每天早晨9点收集关于AI Agent的新闻，并总结成一个专题发给我` 会创建 `scheduled_push` 任务。主线流程是：LLM 先把自然语言抽取成标准任务参数（`schedule`、`topics`、`category_scope`、`output_style`、`parsed_workflow` 等），服务端规范化后调用 `create_task` 入库。`scheduled_tasks` 会保存 owner/user、cron、创建时间、原始任务描述和解析后的任务流程。到期后 runner 会抓取/召回相关新闻，生成专题摘要，并追加到该用户固定的 `Scheduled Push` 对话。

发现的 URL 会先去除 fragment 和常见追踪参数，再按 canonical URL 去重。正文入库时还会按内容 hash 做第二层去重。抓取结果会返回 `saved_articles`、`duplicate_articles`、`skipped_articles` 和各 worker 的执行记录。

### LLM 主题抽取与合并

持续抓取进程会在每轮抓取后处理尚未分析的文章。板块来自 `sources.yaml` 中索引页的固定绑定；LLM 只读取文章 `title + content`，抽取核心 subject、主题名称、核心事件摘要、关键词和置信度。

每次调用会附带最近 5 天同板块的已有主题。模型判断属于已有主题时必须返回候选列表中的 `topic_id`，程序会校验该 ID 并合并文章；否则创建新主题。文章与主题的关联是幂等的，同一文章不会重复增加计数。

业务提示词位于：

```text
personal_news_agent/prompts/topic_extraction.md
```

提示词可以独立修改；JSON Schema、索引页板块约束、主题 ID 白名单和数据库合并由程序控制。也可以单独执行待处理文章：

```bash
python3 scripts/run_topic_extraction.py --limit 20
```

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
AccessKeyID=...
AccessKeySecret=...
ALIYUN_CLOUDAUTH_ENDPOINT=cloudauth.aliyuncs.com
ALIYUN_REGION_ID=cn-beijing
PNA_REALNAME_TEST_NAME=...
PNA_REALNAME_TEST_MOBILE=...
```

阿里云能力统一使用项目 `.env` 里的 `AccessKeyID` / `AccessKeySecret`，不读取其他别名。

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

- `yuanrong-personal-assistant`：展示名“元融大模型”，默认的个人资讯助理角色。
- `qwen3.6`：偏向层次清楚、覆盖完整的通用分析角色。
- `deepseek-v4-flash`：偏向快速、直接的新闻摘要角色。

以上是面向用户的逻辑模型角色，用于控制交互风格和产品概念；当前三个选项实际统一运行在 `deepseek-v4-flash`，不会因前端切换而改变工具权限、搜索范围或接口协议。

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

Tavily 可作为当前推荐的实时全网搜索 provider：

```bash
export EXTERNAL_SEARCH_PROVIDER=tavily
export TAVILY_API_KEY=tvly-...
export TAVILY_SEARCH_DEPTH=basic
export TAVILY_TRUST_ENV=0
```

聊天研究链路默认启用联网：每个普通对话由 CC 加载通用新闻研究 Skill，先检索本地新闻，再至少调用一次 CC 自带 `WebSearch` 核对最新外部信息。若用户手动关闭“联网回答”，本轮严格只使用本地证据；后台自动热点聚合也保持离线，不消耗联网额度。
`/factcheck 待核查说法` 由 CC 加载项目级事实核查 Skill，先查本地新闻引擎，再用 CC 自带 `WebSearch` 做多源核验，并将结果去重后交给 DeepSeek V4 Flash 统一汇总和判定。搜索失败时会明确回退，不会伪装成已完成联网核查。
`TAVILY_TRUST_ENV=0` 默认忽略系统代理；只有确认本机 HTTP/SOCKS 代理可供 `httpx` 使用时才改为 `1`。

检查后端：

```bash
curl http://127.0.0.1:8000/api/news/search/backend
```

搜索架构：ES 负责文章召回检索，`/api/news/search` 支持 `category_scope` 和 `source_scope` 做定向搜索；SQLite FTS 只作为 ES 未配置或不可用时的本地 fallback。接入 ES 后可运行 `scripts/reindex_elasticsearch.py` 把已有文章写入索引。

深度挖掘：`/api/news/deep-dive` 会先做起始召回，再从证据中抽取关键词和实体，生成垂直扩展查询与水平扩展查询并继续召回。LLM planner 的接入点已保留，后续可以由模型决定新事件、新主体、新搜索词和停止条件。

## CC Runtime 主控（实验分支）

研究型对话由 Claude Agent SDK Runtime 主控，实际运行模型统一为 DeepSeek V4 Flash。Runtime 不直接访问数据库、文件或 Shell，只能加载白名单内的项目级 Skill，并调用应用提供的只读本地新闻引擎与外部搜索工具。普通对话默认联网，且完成回答前必须观察到 CC 自带 `WebSearch` 的真实调用；首轮跳过时会自动补充一轮搜索。工具结果会回填原有 `recommendations`、`evidence`、`research_trace`、`event_line` 和 Markdown 回答结构，前后端协议不变。

项目级 CC Skill 位于 `.claude/skills/`：

- `news-conversation-research`：承接每个普通提问和追问，以自然对话开场，并按事件需要组织现状、人物、时间线、争议、影响和后续观察章节；
- `news-fact-check`：拆分原子命题、区分直接/间接证据并输出保守判定；
- `hot-event-map`：检索主体、时间线、因果与影响关系，输出受限且可渲染的 Mermaid 图谱。

前端命令分别为 `/factcheck <说法>` 和 `/map <热点事件>`。用户可见执行过程只展示“本地新闻引擎”和“外部搜索工具”等产品级步骤，不显示具体存储或检索实现。

自动热点聚合也优先由 CC 执行：后台每 15 分钟读取最近 24 小时标题窗口，将同一具体事件按文章 ID 合并，再由系统根据跨来源数量、最近 6 小时刷新次数与时效性计算热度。CC 聚合超时或输出不满足约束时立即退回已有的确定性推荐，不阻塞抓取进程。

该实验分支默认启用 Runtime；缺少 SDK 或凭据时不会发起调用，而是自动使用原流水线。可在 `.env` 显式配置或用 `PNA_CC_RUNTIME_ENABLED=0` 关闭：

```bash
PNA_LLM_ENDPOINT=https://api.deepseek.com
PNA_LLM_KEY=<DeepSeek API Key，普通 LLM 与 CC 共用且只配置这一处>
PNA_LLM_MODEL=deepseek-v4-flash
PNA_LLM_DEFAULT_MODEL=yuanrong-personal-assistant
PNA_LLM_TIMEOUT_SECONDS=120
PNA_CC_RUNTIME_ENABLED=1
PNA_CC_RUNTIME_EFFORT=max
PNA_CC_RUNTIME_MAX_TURNS=6
PNA_CC_RUNTIME_MAX_BUDGET_USD=
PNA_CC_RUNTIME_TIMEOUT_SECONDS=150
PNA_CC_RUNTIME_ALLOW_EXISTING_LOGIN=0
PNA_CC_RUNTIME_BUILTIN_WEB_SEARCH=1
PNA_CC_RUNTIME_CONFIG_DIR=data/cc_runtime
PNA_LOCAL_AGENT_MAX_HISTORY=30
PNA_LOCAL_AGENT_TEMPERATURE=0.2
PNA_LOCAL_AGENT_SESSION_STORE=data/local_agent_sessions.json
```

普通 LLM、CC Runtime 和兼容 Local Agent 不是三套模型配置：三条路径统一读取 `PNA_LLM_ENDPOINT`、`PNA_LLM_KEY`、`PNA_LLM_MODEL` 和 `PNA_LLM_TIMEOUT_SECONDS`。系统固定推导 CC 地址 `https://api.deepseek.com/anthropic`；`PNA_LLM_DEFAULT_MODEL` 只是前端默认逻辑角色，不改变真实运行模型。不要配置 `LLM_*`、`ANTHROPIC_*`、`PNA_CC_RUNTIME_BASE_URL`、`PNA_CC_RUNTIME_AUTH_TOKEN`、`PNA_CC_RUNTIME_API_KEY`、`PNA_CC_RUNTIME_MODEL` 或 `PNA_LOCAL_AGENT_PROVIDER/BASE_URL/API_KEY/DEFAULT_MODEL/TIMEOUT_SECONDS`。

模型 endpoint、模型名和密钥只允许写在项目根目录 `.env`；`.env.ext` 只放部署路径、虚拟环境、端口和数据库位置。当前 provider 固定为 DeepSeek，`PNA_LLM_ENDPOINT` 必须保持 `https://api.deepseek.com`。`./start.sh preflight` 会拒绝 `.env.ext` 中的模型配置、根目录 `.env` 中的旧配置名以及非官网 endpoint，避免普通 LLM 与 CC 再次发生配置漂移。

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

curl -X POST http://127.0.0.1:8000/api/topics/summary \
  -H 'content-type: application/json' \
  -d '{"topic":"新能源汽车价格战","category_scope":["auto","economy"],"max_articles":8,"use_llm":true}'
```

默认专题摘要 skill 会输出章节、时间线、人物/事件图谱、分析、Markdown 和来源证据。可用 demo 脚本从头到尾跑一遍：

```bash
python3 scripts/demo_topic_summary.py --topic 新能源汽车价格战 --category auto --category economy --no-llm
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
