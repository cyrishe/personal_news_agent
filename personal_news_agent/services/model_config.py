from __future__ import annotations

from dataclasses import dataclass

from personal_news_agent.config import Settings


YUANRONG_SYSTEM_PROMPT = "你是元融个人助理大模型，回答要简洁、可信、贴合用户长期兴趣。"


@dataclass(frozen=True)
class ModelOption:
    key: str
    name: str
    provider_model: str
    description: str
    fixed_system_prompt: str = ""


def model_options() -> list[ModelOption]:
    return [
        ModelOption(
            key="yuanrong-personal-assistant",
            name="元融个人助理大模型",
            provider_model="qwen3.5-plus",
            description="默认个人资讯助理模型，底层接 qwen3.5-plus，并绑定简短固定系统提示词。",
            fixed_system_prompt=YUANRONG_SYSTEM_PROMPT,
        ),
        ModelOption(
            key="qwen3.5-flash",
            name="Qwen3.5 Flash",
            provider_model="qwen3.5-flash",
            description="轻量快速模型，适合日常摘要和资讯问答。",
        ),
        ModelOption(
            key="qwen3.5-plus",
            name="Qwen3.5 Plus",
            provider_model="qwen3.5-plus",
            description="通用增强模型，适合更完整的分析和报告。",
        ),
    ]


def get_model_option(key: str | None, settings: Settings) -> ModelOption:
    selected = key or settings.llm_default_model
    options = {option.key: option for option in model_options()}
    if selected in options:
        return options[selected]
    for option in options.values():
        if selected == option.provider_model:
            return option
    return options["yuanrong-personal-assistant"]


def public_model_options() -> list[dict]:
    return [
        {
            "key": option.key,
            "name": option.name,
            "provider_model": option.provider_model,
            "description": option.description,
            "has_fixed_system_prompt": bool(option.fixed_system_prompt),
        }
        for option in model_options()
    ]
