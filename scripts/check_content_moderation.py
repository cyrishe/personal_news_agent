#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.content_moderation import TextModerationPlusService


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely probe Aliyun query moderation configuration.")
    parser.add_argument(
        "--text",
        default="你好，请介绍今天值得关注的科技新闻。",
        help="Short query to submit to llm_query_moderation.",
    )
    args = parser.parse_args()

    service = TextModerationPlusService(
        access_key_id=settings.aliyun_access_key_id,
        access_key_secret=settings.aliyun_access_key_secret,
        endpoint=settings.content_moderation_endpoint,
        query_service=settings.content_moderation_query_service,
        fail_open=settings.content_moderation_fail_open,
    )
    print(
        json.dumps(
            {
                "enabled": settings.content_moderation_enabled,
                "configured": service.configured,
                "aliyun_credentials_configured": service._aliyun_configured,
                "service": service.query_service,
                "endpoint": service.endpoint,
                "fail_open": service.fail_open,
            },
            ensure_ascii=False,
        )
    )
    try:
        result = service.check_query_text(args.text, account_id="configuration_probe", data_id="probe")
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "provider_code": getattr(exc, "provider_code", None),
                    "request_id": getattr(exc, "request_id", None),
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "allowed": result.allowed,
                "code": result.code,
                "risk_level": result.risk_level,
                "label": result.label,
                "request_id": result.request_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
