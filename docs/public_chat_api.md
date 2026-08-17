# 对话 API 与审计日志

## 1. 创建 API Key

先通过 `/api/auth/login` 获得登录会话 `session.token`。使用该会话创建用户自己的 API Key：

```bash
curl -X POST http://127.0.0.1:22053/api/v1/api-keys \
  -H 'Authorization: Bearer <session-token>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"服务端调用","expires_in_days":90}'
```

响应中的 `api_key` 只返回一次。数据库仅保存 SHA-256 摘要和可识别前缀，不保存明文 Key。

管理接口：

- `GET /api/v1/api-keys`：列出当前用户的 Key，不返回明文。
- `DELETE /api/v1/api-keys/{api_key_id}`：撤销当前用户的 Key。

## 2. 创建并使用 API 对话

```bash
API_KEY='<created-api-key>'

curl -X POST http://127.0.0.1:22053/api/v1/conversations \
  -H "Authorization: Bearer ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"title":"每日热点研究"}'
```

使用返回的 `conversation.id` 发送消息：

```bash
curl -X POST http://127.0.0.1:22053/api/v1/conversations/<conversation-id>/messages \
  -H "Authorization: Bearer ${API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{
    "message":"总结今天人工智能领域最值得关注的变化",
    "allow_web_search":true,
    "model_key":"yuanrong-personal-assistant"
  }'
```

读取历史：`GET /api/v1/conversations/{conversation_id}`。API Key 只能访问其所属用户创建的 API 对话。

## 3. 对话数据和日志边界

- SQLite `conversation_turns`：正式业务记录，保存用户问题、最终回答、结构化响应和所属用户。
- `operation_logs`：服务调用摘要，例如审核、抓取和 CC Runtime 的结果状态。
- `logs/conversations/conversation-YYYY-MM-DD.jsonl`：原始请求、响应、流事件、异常和耗时，按 UTC 日期滚动。

JSONL 文件默认保留 30 天，文件权限设置为 `0640`。可通过以下配置调整：

```dotenv
PNA_CONVERSATION_AUDIT_LOG_ENABLED=1
PNA_CONVERSATION_AUDIT_LOG_DIR=logs/conversations
PNA_CONVERSATION_AUDIT_LOG_RETENTION_DAYS=30
```

## 4. 阿里云输入审核

```dotenv
PNA_CONTENT_MODERATION_ENABLED=1
PNA_CONTENT_MODERATION_FAIL_OPEN=1
ALIYUN_CONTENT_MODERATION_QUERY_SERVICE=llm_query_moderation
ALIYUN_CONTENT_MODERATION_ENDPOINT=green-cip.cn-shanghai.aliyuncs.com
```

部署前可执行：

```bash
./venvs/personal_news_agent/bin/python scripts/check_content_moderation.py
```

风险命中始终阻断。`FAIL_OPEN=1` 只影响云服务未开通、权限不足或网络异常等“审核未完成”场景，此类异常会写入 `operation_logs`，不会被误判成内容违规。
