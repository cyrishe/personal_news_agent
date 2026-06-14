from __future__ import annotations

import hashlib
import re
from collections import Counter


TOKEN_RE = re.compile(r"[A-Za-z0-9_+\-.#]{2,}|[\u4e00-\u9fff]{2,}")
ENTITY_RE = re.compile(r"[A-Z][A-Za-z0-9&.\-]{1,}|[\u4e00-\u9fff]{2,}(?:公司|集团|汽车|科技|游戏|动漫|球队|影视|娱乐|银行)")


def stable_id(prefix: str, text: str, length: int = 16) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    tokens = [token.strip() for token in TOKEN_RE.findall(text) if len(token.strip()) >= 2]
    stopwords = {"新闻", "最新", "表示", "相关", "发布", "报道", "今日", "一个", "我们"}
    counts = Counter(token for token in tokens if token not in stopwords)
    return [token for token, _ in counts.most_common(limit)]


def extract_entities(text: str, limit: int = 8) -> list[str]:
    seen: dict[str, None] = {}
    for match in ENTITY_RE.findall(text):
        value = match.strip()
        if value and value not in seen:
            seen[value] = None
        if len(seen) >= limit:
            break
    return list(seen)


def summarize(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
