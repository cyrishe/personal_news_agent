#!/usr/bin/env python3
"""为客户签发 30 天 PNA API 试用 Access Key。

默认行为：
- 按客户邮箱创建或复用 PNA 用户；
- 固定签发 30 天试用 Key；
- 若已有未过期的同类试用 Key，拒绝重复签发；
- 使用 --rotate 时，先生成新 Key，再撤销旧的有效试用 Key；
- 完整凭证写入权限为 0600 的 JSON 文件，密钥只保存明文一次。

示例：
    venvs/personal_news_agent/bin/python scripts/create_trial_access_key.py \
      --customer-name "示例客户" \
      --customer-email "customer@example.com"

轮换或续发：
    venvs/personal_news_agent/bin/python scripts/create_trial_access_key.py \
      --customer-name "示例客户" \
      --customer-email "customer@example.com" \
      --rotate
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

from personal_news_agent.config import BASE_DIR, Settings
from personal_news_agent.services.api_keys import ApiKeyService
from personal_news_agent.services.store import NewsStore


TRIAL_DAYS = 30
DEFAULT_API_BASE_URL = "https://ai-agent.kingdomai.com/pna"
TRIAL_KEY_NAME_PREFIX = "customer-trial:"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAFE_FILE_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer-name", required=True, help="客户或公司名称")
    parser.add_argument("--customer-email", required=True, help="客户唯一邮箱")
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("PNA_PUBLIC_API_BASE_URL", DEFAULT_API_BASE_URL),
        help="发给客户的正式 API 根地址",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="凭证 JSON 路径；默认写入 runtime/customer_access_keys/",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="撤销该客户现有的有效试用 Key，并签发新 Key",
    )
    parser.add_argument(
        "--print-key",
        action="store_true",
        help="在终端额外显示一次 Access Key；默认只写入凭证文件",
    )
    return parser.parse_args()


def normalize_customer_name(value: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        raise ValueError("customer_name_empty")
    return cleaned[:120]


def normalize_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not EMAIL_PATTERN.fullmatch(cleaned):
        raise ValueError("customer_email_invalid")
    return cleaned[:254]


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def active_trial_keys(items: list[dict[str, object]], key_name: str) -> list[dict[str, object]]:
    now = datetime.now(timezone.utc)
    active: list[dict[str, object]] = []
    for item in items:
        if item.get("name") != key_name or item.get("revoked_at"):
            continue
        expires_at = parse_time(str(item.get("expires_at") or ""))
        if expires_at is None or expires_at > now:
            active.append(item)
    return active


def default_output_path(email: str) -> Path:
    safe_email = SAFE_FILE_PATTERN.sub("_", email).strip("._-") or "customer"
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    return BASE_DIR / "runtime" / "customer_access_keys" / f"{safe_email}_{stamp}.json"


def write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"凭证文件已存在，拒绝覆盖：{path}")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def main() -> int:
    args = parse_args()
    try:
        customer_name = normalize_customer_name(args.customer_name)
        customer_email = normalize_email(args.customer_email)
    except ValueError as exc:
        message = {
            "customer_name_empty": "客户名称不能为空。",
            "customer_email_invalid": "客户邮箱格式不正确。",
        }.get(str(exc), "客户信息不正确。")
        print(message, file=sys.stderr)
        return 2

    api_base_url = args.api_base_url.strip().rstrip("/")
    if not api_base_url.startswith(("https://", "http://")):
        print("API 地址必须以 https:// 或 http:// 开头。", file=sys.stderr)
        return 2

    output_path = (args.output or default_output_path(customer_email)).resolve()
    if output_path.exists():
        print(f"凭证文件已存在，拒绝覆盖：{output_path}", file=sys.stderr)
        return 2

    settings = Settings()
    store = NewsStore(settings.sqlite_path)
    store.init()
    customer = store.get_user_by_email(customer_email)
    created_user = customer is None
    if customer is None:
        customer = store.create_user(
            display_name=customer_name,
            email=customer_email,
            password_hash=None,
        )

    key_name = f"{TRIAL_KEY_NAME_PREFIX}{customer_email}"
    api_keys = ApiKeyService(store)
    old_active_keys = active_trial_keys(api_keys.list(customer["id"]), key_name)
    if old_active_keys and not args.rotate:
        latest_expiry = max(str(item.get("expires_at") or "长期") for item in old_active_keys)
        print(
            "该客户已有未过期的试用 Access Key，未重复签发。"
            f"有效期：{latest_expiry}。如需换 Key，请增加 --rotate。",
            file=sys.stderr,
        )
        return 3

    created = api_keys.create(customer["id"], key_name, expires_in_days=TRIAL_DAYS)
    expires_utc = parse_time(created["expires_at"])
    assert expires_utc is not None
    expires_beijing = expires_utc.astimezone(ZoneInfo("Asia/Shanghai"))
    credential = {
        "customer": {
            "name": customer_name,
            "email": customer_email,
            "user_id": customer["id"],
            "created_user": created_user,
        },
        "api": {
            "base_url": api_base_url,
            "create_conversation_url": f"{api_base_url}/api/v1/conversations",
            "message_url_template": (
                f"{api_base_url}/api/v1/conversations/{{conversation_id}}/messages"
            ),
            "authorization": "Authorization: Bearer <access_key>",
        },
        "credential": {
            "access_key": created["api_key"],
            "api_key_id": created["id"],
            "key_prefix": created["key_prefix"],
            "valid_days": TRIAL_DAYS,
            "created_at": created["created_at"],
            "expires_at_utc": expires_utc.isoformat(),
            "expires_at_beijing": expires_beijing.isoformat(),
        },
        "notice": "Access Key 仅显示并保存一次；30 天到期后请联系服务方重新申请。",
    }

    try:
        write_private_json(output_path, credential)
    except Exception as exc:
        api_keys.revoke(customer["id"], created["id"])
        print(f"凭证文件写入失败，新 Key 已自动撤销：{exc}", file=sys.stderr)
        return 1

    revoked_ids: list[str] = []
    if args.rotate:
        for item in old_active_keys:
            revoked = api_keys.revoke(customer["id"], str(item["id"]))
            if revoked:
                revoked_ids.append(str(item["id"]))

    print("30 天客户试用 Access Key 已生成。")
    print(f"客户：{customer_name} <{customer_email}>")
    print(f"API：{api_base_url}")
    print(f"北京时间到期：{expires_beijing.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"凭证文件：{output_path}")
    print(f"已轮换旧 Key：{len(revoked_ids)} 个")
    if args.print_key:
        print(f"Access Key：{created['api_key']}")
    else:
        print("Access Key 未输出到终端，请从上述凭证文件读取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
