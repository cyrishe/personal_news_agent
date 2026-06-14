from __future__ import annotations

CATEGORIES: dict[str, str] = {
    "politics": "时政",
    "economy": "经济",
    "tech": "科技",
    "auto": "汽车",
    "game": "游戏",
    "anime": "动漫",
    "entertainment": "娱乐",
    "sports": "体育",
}


def validate_category(category: str) -> None:
    if category not in CATEGORIES:
        allowed = ", ".join(sorted(CATEGORIES))
        raise ValueError(f"Unknown category '{category}'. Allowed categories: {allowed}")
