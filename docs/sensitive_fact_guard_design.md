# 敏感事实全局护栏设计

## 目标

对所有 CC 请求建立统一的“问题预检、提示词注入、输出复核、确定性兜底”链路。
护栏是系统能力，不属于用户可选择的业务 Skill，也不受 API `conversation_mode` 影响。

## 执行位置

主执行点位于 `personal_news_agent/services/cc_runtime.py` 的
`CCRuntimeOrchestrator.run()`。所有普通对话、新闻研究、事实核查、报告和定时任务只要调用
CC Runtime，都会经过这里。

流程：

1. `inspect_sensitive_facts()` 对原始问题和标准化查询做零模型成本检测，生成风险标签。
2. 命中后，将 `sensitive_fact_guard.md` 注入 CC system prompt；原始用户问题保持不变，便于审计。
3. 主回答生成完成后，在同一 CC 会话中执行一次低强度输出复核，不再调用搜索工具。
4. 最终返回前，按 `sensitive_fact_rules.json` 执行确定性替换或必备声明规则。
5. 标签、是否执行 CC 复核、确定性规则修改项和模型成本写入 `operation_logs`。

当前输入内容安全审核 `NewsChatService._moderate_query()` 仍是独立层：它负责违规内容的允许或阻断，
不负责事实口径。两者不应合并。

## 你需要填写的文件

### `personal_news_agent/prompts/sensitive_fact_guard.md`

填写系统级自然语言政策，包括：

- 适用范围；
- 强制事实基线；
- 标准名称和禁止表述；
- 如何纠正错误问题前提；
- 来源与交叉验证要求；
- 输出复核清单和修订风格。

### `personal_news_agent/prompts/sensitive_fact_rules.json`

用于不依赖模型的最终兜底。结构如下：

```json
{
  "version": 1,
  "replacement_rules": [
    {
      "id": "规则唯一名称",
      "tags": ["命中的风险标签"],
      "pattern": "Python 正则表达式",
      "replacement": "替换文本",
      "flags": ["IGNORECASE"]
    }
  ],
  "required_statement_rules": [
    {
      "id": "规则唯一名称",
      "tags": ["命中的风险标签"],
      "required_any": ["回答中至少出现一个的文本"],
      "statement": "均未出现时添加到回答开头的文本"
    }
  ]
}
```

## 配置开关

- `PNA_SENSITIVE_FACT_GUARD_ENABLED=1`：启用问题识别与提示词注入。
- `PNA_SENSITIVE_FACT_CC_REVIEW_ENABLED=1`：命中敏感标签时增加一次 CC 输出复核。

建议在提示词和回归题集完成前，只在测试环境开启第二个开关。

## 成本边界

未命中敏感标签时没有额外模型调用。命中时复用同一 CC 会话增加一次复核请求；复核禁止工具调用，
使用服务器统一配置的低推理强度。确定性规则不产生模型费用。

## 后续扩展点

当前风险标签由 `sensitive_fact_guard.py` 中的 `_TAG_PATTERNS` 提供。增加新主题时，应同时增加：

1. 一个范围明确的标签和检测表达式；
2. 提示词中的事实与表述规则；
3. 正例、诱导前提、别称和非命中负例；
4. 必要时增加确定性兜底规则。
