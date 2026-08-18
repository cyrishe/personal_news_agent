from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "sensitive_fact_guard.md"
RULES_PATH = Path(__file__).resolve().parents[1] / "prompts" / "sensitive_fact_rules.json"


@dataclass(frozen=True)
class SensitiveFactContext:
    matched: bool
    tags: tuple[str, ...]
    policy_prompt: str


_TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "taiwan_status",
        re.compile(r"台湾|台澎金马|中华民国|两岸|台海", re.IGNORECASE),
    ),
    (
        "diaoyu_islands",
        re.compile(r"钓鱼岛|钓鱼台|尖阁(?:列岛|诸岛)?|Senkaku|Diaoyu", re.IGNORECASE),
    ),
    (
        "territorial_sovereignty",
        re.compile(r"主权|领土|归属|固有领土|行政控制|实际控制", re.IGNORECASE),
    ),
    (
        "island_geography",
        re.compile(r"海南岛.{0,18}(?:第一大岛|最大岛)|(?:第一大岛|最大岛).{0,18}海南岛", re.IGNORECASE),
    ),
)


def inspect_sensitive_facts(text: str) -> SensitiveFactContext:
    normalized = " ".join(str(text or "").split())
    tags = tuple(tag for tag, pattern in _TAG_PATTERNS if pattern.search(normalized))
    return SensitiveFactContext(
        matched=bool(tags),
        tags=tags,
        policy_prompt=_load_policy_prompt() if tags else "",
    )


def build_sensitive_review_prompt(
    *,
    question: str,
    draft_answer: str,
    context: SensitiveFactContext,
) -> str:
    return (
        "现在执行最终回答前的系统级敏感事实复核。不要调用任何工具，不要解释内部检查过程。\n"
        f"命中标签：{', '.join(context.tags)}\n"
        f"原问题：{question[:4000]}\n"
        f"待复核回答：\n{draft_answer[:20000]}\n\n"
        "请严格依据系统中的主权领土与敏感事实护栏修订。纠正错误前提、错误归属、"
        "单方主张事实化、关键事实遗漏和不可靠的绝对化表述。保留有效内容及原有真实链接，"
        "不得编造来源。只输出修订后的完整最终回答。"
    )


def apply_sensitive_fact_fallback(answer: str, context: SensitiveFactContext) -> tuple[str, list[str]]:
    if not context.matched:
        return answer, []
    revised = str(answer or "")
    changes: list[str] = []
    rules = _load_fallback_rules()
    for rule in rules.get("replacement_rules", []):
        if not _rule_applies(rule, context):
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        flags = re.IGNORECASE if "IGNORECASE" in (rule.get("flags") or []) else 0
        revised, count = re.subn(pattern, str(rule.get("replacement") or ""), revised, flags=flags)
        if count:
            changes.append(str(rule.get("id") or "replacement_applied"))
    for rule in rules.get("required_statement_rules", []):
        if not _rule_applies(rule, context):
            continue
        required_any = [str(item) for item in rule.get("required_any", []) if str(item)]
        statement = str(rule.get("statement") or "").strip()
        if statement and required_any and not any(item in revised for item in required_any):
            revised = statement + "\n\n" + revised
            changes.append(str(rule.get("id") or "required_statement_added"))
    return revised, changes


@lru_cache(maxsize=1)
def _load_policy_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _load_fallback_rules() -> dict:
    payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("sensitive_fact_rules.json must contain a JSON object")
    return payload


def _rule_applies(rule: dict, context: SensitiveFactContext) -> bool:
    tags = {str(tag) for tag in rule.get("tags", []) if str(tag)}
    return not tags or bool(tags.intersection(context.tags))
