from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_news_agent.config import settings
from personal_news_agent.services.realname import RealNameVerificationError, RealNameVerificationService


def main() -> None:
    parser = argparse.ArgumentParser(description="Test configured real-name mobile verification provider.")
    parser.add_argument("--provider", default=None, help="Override provider, e.g. tencent or mock.")
    parser.add_argument("--name", default=None)
    parser.add_argument("--mobile", default=None)
    args = parser.parse_args()

    if args.provider:
        object.__setattr__(settings, "realname_provider", args.provider)
    name = args.name or settings.realname_test_name
    mobile = args.mobile or settings.realname_test_mobile
    if not name or not mobile:
        raise SystemExit("missing test identity: set PNA_REALNAME_TEST_NAME/TEST_NAME and PNA_REALNAME_TEST_MOBILE/TEST_NUMBER")

    svc = RealNameVerificationService(settings)
    status = svc.status()
    print(
        {
            "provider": status["provider"],
            "provider_name": status.get("provider_name"),
            "provider_configured": _provider_configured(status),
            "aliyun_endpoint": status.get("aliyun_endpoint"),
            "test_identity_configured": status["test_identity_configured"],
            "mobile": _mask_mobile(mobile),
        }
    )
    try:
        result = svc.verify(real_name=name, id_card=None, mobile=mobile)
    except RealNameVerificationError as exc:
        raise SystemExit(f"verification failed before/at provider call: {exc}") from exc
    print(
        {
            "passed": result.passed,
            "provider": result.provider,
            "request_id": result.request_id,
            "message": result.message,
            "verified_at": result.verified_at,
        }
    )


def _mask_mobile(value: str) -> str:
    return value[:3] + "****" + value[-4:] if len(value) == 11 else "***"


def _provider_configured(status: dict) -> bool:
    provider = status.get("provider")
    if provider == "mock":
        return bool(status.get("mock_enabled"))
    if provider == "aliyun":
        return bool(status.get("aliyun_configured"))
    if provider == "tencent":
        return bool(status.get("tencent_configured"))
    return False


if __name__ == "__main__":
    main()
