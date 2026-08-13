# Claude Code / Local Agent Backend

This directory is an isolated backend scaffold that can run either Claude Code
CLI or an OpenAI-compatible local model service.

It is not wired into the main application. The default remains a local
OpenAI-compatible server, so adding this folder does not call Claude or change
the existing news backend.

## What is included

- `models.py`: request and response schemas for sessions, messages, attachments,
  and chat responses.
- `session_store.py`: JSON-backed session storage for messages, project context,
  and context event history.
- `service.py`: local-agent orchestration for session history and provider calls.
- `provider.py`: OpenAI-compatible local model client.
- `provider.py`: also contains an opt-in Claude Code CLI provider using print
  mode, JSON output, and resumable sessions.
- `model_config.py`: NPA-style model options and `model_key` to `provider_model`
  mapping.
- `routes.py`: FastAPI router under `/api/local-agent`.
- `config.py`: environment-based local model settings.
- `app.py`: optional standalone FastAPI app for local smoke testing.

## Default local model contract

The provider calls:

```text
POST http://127.0.0.1:11434/v1/chat/completions
```

with an OpenAI-compatible payload:

```json
{
  "model": "qwen3.5-plus",
  "messages": [],
  "temperature": 0.2,
  "stream": false
}
```

This follows NPA's existing model contract: the API receives a `model_key`, then
resolves it to the provider-facing `provider_model`.

## Model options

The local agent currently mirrors the NPA model configuration:

```text
yuanrong-personal-assistant -> qwen3.5-plus
qwen3.5-flash              -> qwen3.5-flash
qwen3.5-plus               -> qwen3.5-plus
deepseek-v4-flash          -> deepseek-v4-flash
```

`yuanrong-personal-assistant` also injects the same fixed system prompt used by
NPA:

```text
你是元融个人助理大模型，回答要简洁、可信、贴合用户长期兴趣。
```

Your local model server must expose these model names, or route them internally
to locally installed models.

## Environment variables

```bash
PNA_LLM_ENDPOINT=https://api.deepseek.com
PNA_LLM_KEY=<DeepSeek API Key>
PNA_LLM_MODEL=deepseek-v4-flash
PNA_LLM_DEFAULT_MODEL=yuanrong-personal-assistant
PNA_LLM_TIMEOUT_SECONDS=120
PNA_LOCAL_AGENT_MAX_HISTORY=30
PNA_LOCAL_AGENT_TEMPERATURE=0.2
PNA_LOCAL_AGENT_SESSION_STORE=.local_agent_sessions.json
```

The compatibility agent has no independent provider, endpoint, key, model, or
timeout. It receives the application's `PNA_LLM_*` contract from the service
factory so every model-backed path stays aligned.

The older independently configured Claude Code CLI provider is no longer part
of the application contract. Agent orchestration is handled by CC Runtime, and
all model-backed compatibility calls use the shared `PNA_LLM_*` provider.

## Context recording

The backend records context in two places:

- `project_context`: the latest merged context dictionary used for model calls.
- `context_events`: an append-only history of where each context update came
  from.

Create a session with initial context:

```bash
curl -X POST http://127.0.0.1:8000/api/local-agent/sessions \
  -H "Content-Type: application/json" \
  -d '{"user_id":"default","title":"demo","project_context":{"topic":"AI news"}}'
```

Append or merge context:

```bash
curl -X PUT http://127.0.0.1:8000/api/local-agent/sessions/{session_id}/context \
  -H "Content-Type: application/json" \
  -d '{"source":"frontend","summary":"user selected topic","values":{"topic":"sports AI","language":"zh"}}'
```

Read current context plus history:

```bash
curl http://127.0.0.1:8000/api/local-agent/sessions/{session_id}/context
```

Passing `project_context` in `/chat` also records a `chat_request` context event
and merges those values into the session before calling the local model.

## Optional integration later

When you are ready to connect it to the existing FastAPI app, add this to the
main app setup:

```python
from claude_code_backend import create_local_agent_router

app.include_router(create_local_agent_router())
```

The scaffold intentionally avoids changing the existing app for now.

## Standalone local run

Start a local model first. For Ollama:

```bash
ollama serve
```

Make sure your local server can serve or alias `qwen3.5-plus`,
`qwen3.5-flash`, and `deepseek-v4-flash` if you want to use the default model
keys unchanged.

Then run this backend:

```bash
uvicorn claude_code_backend.app:app --reload
```

Smoke test:

```bash
curl -X POST http://127.0.0.1:8000/api/local-agent/chat \
  -H "Content-Type: application/json" \
  -d '{"model_key":"yuanrong-personal-assistant","message":"hello","project_context":{"feature":"personal news"}}'
```

Model list:

```bash
curl http://127.0.0.1:8000/api/local-agent/models
```

## Next steps

The current provider only sends chat messages to a local model. To become a full
Claude Agent SDK-style coding agent, add a tool layer behind the service:

- read files
- write or patch files
- run shell commands with permission checks
- stream tool events to the frontend
- persist sessions outside memory
- add workspace allowlists and command policies
