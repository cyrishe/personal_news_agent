from __future__ import annotations

from dataclasses import dataclass

from personal_news_agent.config import Settings


YUANRONG_SYSTEM_PROMPT = "你是元融个人助理大模型，回答要简洁、可信、贴合用户长期兴趣。"
DEFAULT_LOGICAL_MODEL = "yuanrong-personal-assistant"
DEFAULT_RUNTIME_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class ModelOption:
    key: str
    name: str
    provider_model: str
    description: str
    fixed_system_prompt: str = ""


def model_options(runtime_model: str = DEFAULT_RUNTIME_MODEL) -> list[ModelOption]:
    return [
        ModelOption(
            key=DEFAULT_LOGICAL_MODEL,
            name="元融大模型",
            provider_model=runtime_model,
            description="默认个人资讯助理角色，强调长期兴趣、可信来源和持续跟踪。",
            fixed_system_prompt=YUANRONG_SYSTEM_PROMPT,
        ),
        ModelOption(
            key="qwen3.6",
            name="Qwen 3.6",
            provider_model=runtime_model,
            description="逻辑模型角色，偏向层次清楚的通用分析；当前与其他选项共享同一运行模型。",
            fixed_system_prompt="以层次清楚、覆盖完整的方式组织资讯分析，先结论后证据。",
        ),
        ModelOption(
            key="deepseek-v4-flash",
            name="DeepSeek V4 Flash",
            provider_model=runtime_model,
            description="逻辑模型角色，偏向快速、直接的新闻摘要和日常问答。",
            fixed_system_prompt="回答直接、紧凑，优先给出结论、关键证据和下一步观察点。",
        ),
    ]


def get_model_option(key: str | None, settings: Settings | None = None) -> ModelOption:
    selected = key or getattr(settings, "llm_default_model", DEFAULT_LOGICAL_MODEL)
    runtime_model = getattr(settings, "llm_model", DEFAULT_RUNTIME_MODEL)
    options = {option.key: option for option in model_options(runtime_model)}
    if selected in options:
        return options[selected]
    for option in options.values():
        if selected == option.provider_model:
            return option
    return options[DEFAULT_LOGICAL_MODEL]


def public_model_options(runtime_model: str = DEFAULT_RUNTIME_MODEL) -> list[dict]:
    return [
        {
            "key": option.key,
            "name": option.name,
            "provider_model": option.provider_model,
            "description": option.description,
            "has_fixed_system_prompt": bool(option.fixed_system_prompt),
            "logical_only": True,
            "runtime_model": runtime_model,
        }
        for option in model_options(runtime_model)
    ]
