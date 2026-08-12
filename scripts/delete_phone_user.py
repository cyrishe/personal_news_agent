from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from personal_news_agent.config import settings
from personal_news_agent.services.phone_verification import normalize_mainland_mobile
from personal_news_agent.services.store import NewsStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview or delete one phone account and its PNA-owned data."
    )
    parser.add_argument("--mobile", required=True, help="11-digit mainland China mobile number")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag the command only previews counts.",
    )
    args = parser.parse_args()

    mobile = normalize_mainland_mobile(args.mobile)
    store = NewsStore(settings.sqlite_path)
    store.init()
    result = store.delete_phone_user(
        mobile,
        confirm=args.confirm,
    )
    result["database"] = str(settings.sqlite_path)
    result["mode"] = "delete" if args.confirm else "preview"
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not args.confirm:
        print("Preview only. Re-run with --confirm to delete this account.", file=sys.stderr)


if __name__ == "__main__":
    main()
