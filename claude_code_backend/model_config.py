from __future__ import annotations

from dataclasses import dataclass

from claude_code_backend.config import LocalAgentSettings


YUANRONG_SYSTEM_PROMPT = "你是元融个人助理大模型，回答要简洁、可信、贴合用户长期兴趣。"


@dataclass(frozen=True)
class ModelOption:
    key: str
    name: str
    provider_model: str
    description: str
    fixed_system_prompt: str = ""


def model_options(runtime_model: str = "deepseek-v4-flash") -> list[ModelOption]:
    return [
        ModelOption(
            key="yuanrong-personal-assistant",
            name="元融个人助理大模型",
            provider_model=runtime_model,
            description="默认个人资讯助理角色，与其他选项共享服务端统一运行模型。",
            fixed_system_prompt=YUANRONG_SYSTEM_PROMPT,
        ),
        ModelOption(
            key="qwen3.5-flash",
            name="Qwen3.5 Flash",
            provider_model=runtime_model,
            description="轻量快速模型，适合日常摘要和资讯问答。",
        ),
        ModelOption(
            key="qwen3.5-plus",
            name="Qwen3.5 Plus",
            provider_model=runtime_model,
            description="通用增强模型，适合更完整的分析和报告。",
        ),
        ModelOption(
            key="deepseek-v4-flash",
            name="DeepSeek V4 Flash",
            provider_model=runtime_model,
            description="DeepSeek 快速非推理模型，适合新闻摘要、简报和日常问答。",
        ),
    ]


def get_model_option(key: str | None, settings: LocalAgentSettings) -> ModelOption:
    selected = key or settings.default_model_key
    options = {option.key: option for option in model_options(settings.runtime_model)}
    if selected in options:
        return options[selected]
    for option in options.values():
        if selected == option.provider_model:
            return option
    return options["yuanrong-personal-assistant"]


def public_model_options(runtime_model: str = "deepseek-v4-flash") -> list[dict]:
    return [
        {
            "key": option.key,
            "name": option.name,
            "provider_model": option.provider_model,
            "description": option.description,
            "has_fixed_system_prompt": bool(option.fixed_system_prompt),
        }
        for option in model_options(runtime_model)
    ]
