from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Stop local Elasticsearch runtime.")
    parser.add_argument("--root", type=Path, default=_runtime_root())
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    pid_file = args.root / "elasticsearch.pid"
    if not pid_file.exists():
        print(json.dumps({"stopped": True, "reason": "pid_missing"}))
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        print(json.dumps({"stopped": True, "reason": "process_missing"}))
        return
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(1)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            print(json.dumps({"stopped": True, "pid": pid}))
            return
    print(json.dumps({"stopped": False, "pid": pid}))


def _runtime_root() -> Path:
    ext_root = Path(os.getenv("PERSONAL_NEWS_EXT_ROOT", "/Volumes/ext"))
    return ext_root / "personal_news_agent" / "runtime" / "elasticsearch"


if __name__ == "__main__":
    main()
