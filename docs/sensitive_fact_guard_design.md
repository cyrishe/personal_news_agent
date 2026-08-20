# API Query Safety Skill 设计

## 当前主线

安全策略只在 API Key 对话入口执行。浏览器对话、离线抓取、热点聚合、报告和定时任务不会自动调用它。

唯一策略文件是 `.claude/skills/api-query-safety/SKILL.md`。Skill 同时输出两个正交维度：

1. `categories`：歧视仇恨、色情、暴力、政治公共事务、主权领土、恐怖主义、违法犯罪、自伤、隐私安全等；
2. `decision`：`pass`、`refuse` 或 `safe_answer`。

`pass` 是默认路由结果，不产生安全文案；`refuse` 返回简短拒答；`safe_answer` 返回完整、立场明确的安全回答。类别命中不等于必须拒答，新闻、历史、教育、预防和求助类问题仍可正常通过。

## 调用链

```text
POST /api/v1/conversations/{id}/messages
  -> NewsChatService(enforce_api_safety=True)
  -> ApiQuerySafetyService
  -> CC Runtime + api-query-safety Skill
  -> pass: 继续原对话流程
  -> refuse / safe_answer: 直接返回受约束回答并保存对话记录
```

安全判断不开放本地搜索、外部搜索或其他工具，只允许一次 CC 判断。输出必须是固定 JSON，服务端再次校验枚举、字段和回答约束。CC 不可用或输出无效时，API 返回 `503 api_query_safety_unavailable`，不会绕过检查。

## 配置

- `PNA_API_QUERY_SAFETY_ENABLED=1`：启用 API Key 对话安全 Skill；默认开启。
- `PNA_SENSITIVE_FACT_GUARD_ENABLED=0`：旧全局敏感事实护栏保留作回滚，默认关闭；仅设置环境变量也不会自动注入普通 CC 对话，代码调用方还必须显式请求。

不要为每个类别增加环境变量。类别、分支和输出约束统一维护在 Skill 中。

## 审计

每次判断在 `operation_logs` 写入 `api_query_safety` 记录，只保存用户、对话、类别、风险级别、决策和原因码，不保存内部思考过程。API 原始请求与回答继续由现有 conversation audit JSONL 记录并执行敏感字段脱敏。

## 旧模板

`personal_news_agent/prompts/sensitive_fact_guard.md` 和 `sensitive_fact_rules.json` 仍保留，但不再是默认主线。这样可以保留已有草稿，又不会影响普通 Web 对话。
