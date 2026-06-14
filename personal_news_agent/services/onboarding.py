from __future__ import annotations

from personal_news_agent.config import Settings
from personal_news_agent.core.categories import CATEGORIES
from personal_news_agent.services.model_config import get_model_option, public_model_options
from personal_news_agent.services.store import NewsStore


EXTRA_CATEGORY_OPTIONS = {
    "food": "美食",
    "travel": "旅行",
    "health": "健康",
    "education": "教育",
    "fashion": "时尚",
    "real_estate": "房产",
    "local": "本地生活",
}

GENDER_OPTIONS = ["不透露", "男", "女", "其他"]
ZODIAC_OPTIONS = ["不透露", "白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座", "天秤座", "天蝎座", "射手座", "摩羯座", "水瓶座", "双鱼座"]
OUTPUT_STYLE_OPTIONS = [
    {"key": "casual", "name": "休闲", "prompt": "像熟悉的朋友一样自然表达，少术语，先讲结论。"},
    {"key": "entertainment", "name": "娱乐", "prompt": "语气轻快，突出看点、反转、人物关系和适合继续八卦式追问的线索。"},
    {"key": "formal", "name": "正式", "prompt": "语气克制严谨，结构清楚，明确区分事实、推断和不确定信息。"},
    {"key": "humorous", "name": "幽默", "prompt": "允许轻度幽默和类比，但不牺牲事实准确性。"},
    {"key": "anime", "name": "二次元", "prompt": "表达更有角色感和弹幕感，适合动漫、游戏、娱乐内容，但避免夸张失真。"},
    {"key": "timeline", "name": "时间线优先", "prompt": "优先按时间顺序梳理事件推进、关键节点和后续观察点。"},
    {"key": "deep", "name": "深度解释", "prompt": "优先解释背景、机制、影响链条和多方立场。"},
]


class OnboardingService:
    def __init__(self, store: NewsStore, settings: Settings):
        self.store = store
        self.settings = settings

    def options(self) -> dict:
        implemented = [{"key": key, "name": name, "implemented": True} for key, name in CATEGORIES.items()]
        future = [{"key": key, "name": name, "implemented": False} for key, name in EXTRA_CATEGORY_OPTIONS.items()]
        return {
            "profile_fields": {
                "gender_options": GENDER_OPTIONS,
                "zodiac_options": ZODIAC_OPTIONS,
                "age_min": 12,
                "age_max": 100,
            },
            "categories": implemented + future,
            "default_categories": ["tech", "game", "auto"],
            "output_styles": OUTPUT_STYLE_OPTIONS,
            "models": public_model_options(),
            "default_model": self.settings.llm_default_model,
        }

    def complete(self, user_id: str, payload: dict) -> dict:
        user = self.store.get_user(user_id)
        if not user:
            raise ValueError(f"Unknown user_id: {user_id}")
        normalized = self._normalize_payload(payload)
        model = get_model_option(normalized["model_key"], self.settings)
        assistant_prompt = self._build_assistant_prompt(user, normalized, model.fixed_system_prompt)
        profile = self.store.complete_onboarding(user_id, normalized, assistant_prompt)
        return {
            "user_id": user_id,
            "profile": profile,
            "model": {
                "key": model.key,
                "name": model.name,
                "provider_model": model.provider_model,
                "has_fixed_system_prompt": bool(model.fixed_system_prompt),
            },
            "assistant_prompt": assistant_prompt,
            "preparation": [
                {"key": "profile_saved", "status": "completed"},
                {"key": "assistant_prompt_saved", "status": "completed"},
                {"key": "interest_feed_ready", "status": "completed"},
                {"key": "followup_question_seeds_ready", "status": "completed"},
            ],
        }

    def _normalize_payload(self, payload: dict) -> dict:
        age = payload.get("age")
        if age in ("", None):
            age = None
        elif not (12 <= int(age) <= 100):
            raise ValueError("age must be between 12 and 100")
        preferred = [item for item in payload.get("preferred_categories", []) if item in CATEGORIES or item in EXTRA_CATEGORY_OPTIONS]
        if not preferred:
            preferred = ["tech", "game", "auto"]
        watch_keywords = [item.strip() for item in payload.get("watch_keywords", []) if item and item.strip()]
        negative_keywords = [item.strip() for item in payload.get("negative_keywords", []) if item and item.strip()]
        return {
            "display_name": (payload.get("display_name") or "").strip() or None,
            "self_description": (payload.get("self_description") or "").strip(),
            "age": int(age) if age is not None else None,
            "gender": payload.get("gender") or "不透露",
            "zodiac": payload.get("zodiac") or "不透露",
            "preferred_categories": preferred,
            "watch_keywords": watch_keywords,
            "negative_keywords": negative_keywords,
            "model_key": payload.get("model_key") or self.settings.llm_default_model,
            "output_style": self._normalize_output_style(payload.get("output_style")),
        }

    def _normalize_output_style(self, value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            return "休闲"
        for item in OUTPUT_STYLE_OPTIONS:
            if raw == item["key"] or raw == item["name"]:
                return item["name"]
        return raw[:40]

    def _build_assistant_prompt(self, user: dict, profile: dict, fixed_system_prompt: str) -> str:
        category_names = [CATEGORIES.get(key) or EXTRA_CATEGORY_OPTIONS.get(key) or key for key in profile["preferred_categories"]]
        parts = []
        if fixed_system_prompt:
            parts.append(fixed_system_prompt)
        parts.append(f"你正在服务用户“{profile.get('display_name') or user['display_name']}”。")
        if profile.get("age"):
            parts.append(f"用户年龄约 {profile['age']} 岁。")
        if profile.get("self_description"):
            parts.append(f"用户自我描述：{profile['self_description']}。")
        if profile.get("gender") and profile["gender"] != "不透露":
            parts.append(f"用户性别偏好标记为{profile['gender']}。")
        if profile.get("zodiac") and profile["zodiac"] != "不透露":
            parts.append(f"用户星座是{profile['zodiac']}。")
        parts.append(f"重点关注板块：{'、'.join(category_names)}。")
        if profile["watch_keywords"]:
            parts.append(f"长期关注关键词：{'、'.join(profile['watch_keywords'])}。")
        if profile["negative_keywords"]:
            parts.append(f"尽量避开：{'、'.join(profile['negative_keywords'])}。")
        style_prompt = next((item["prompt"] for item in OUTPUT_STYLE_OPTIONS if item["name"] == profile["output_style"]), "")
        parts.append(f"播报风格：{profile['output_style']}。{style_prompt}")
        parts.append("优先给出可信来源、时间线、相关主体和可继续追问的线索。")
        return "\n".join(parts)
