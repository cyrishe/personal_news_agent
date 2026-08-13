from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_SESSION_STORE_PATH = Path.cwd() / ".local_agent_sessions.json"


@dataclass(frozen=True)
class LocalAgentSettings:
    enabled: bool = True
    workspace_root: Path | None = None
    provider_name: str = "deepseek"
    base_url: str = os.getenv("PNA_LLM_ENDPOINT", "https://api.deepseek.com")
    chat_completions_path: str = "/chat/completions"
    default_model_key: str = os.getenv("PNA_LLM_DEFAULT_MODEL", "yuanrong-personal-assistant")
    runtime_model: str = os.getenv("PNA_LLM_MODEL", "deepseek-v4-flash")
    api_key: str | None = field(default=os.getenv("PNA_LLM_KEY") or None, repr=False)
    timeout_seconds: float = float(os.getenv("PNA_LLM_TIMEOUT_SECONDS", "120"))
    max_history_messages: int = int(os.getenv("PNA_LOCAL_AGENT_MAX_HISTORY", "30"))
    temperature: float = float(os.getenv("PNA_LOCAL_AGENT_TEMPERATURE", "0.2"))
    session_store_path: Path = Path(
        os.getenv("PNA_LOCAL_AGENT_SESSION_STORE", str(DEFAULT_SESSION_STORE_PATH))
    ).expanduser()
    claude_binary: str = "claude"
    claude_model: str | None = None
    claude_permission_mode: str = "plan"
    claude_max_turns: int = 6
    claude_max_budget_usd: float | None = None


settings = LocalAgentSettings()
