from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Start local Elasticsearch from the external-disk runtime.")
    parser.add_argument("--root", type=Path, default=_runtime_root())
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    root = args.root
    home = root / "current"
    if not home.exists():
        raise SystemExit(f"Elasticsearch is not installed. Run scripts/setup_elasticsearch.py first: {home}")
    _clean_appledouble(root)
    pid_file = root / "elasticsearch.pid"
    log_file = root / "elasticsearch.stdout.log"
    if _is_running(pid_file):
        print(json.dumps({"running": True, "pid": int(pid_file.read_text().strip()), "url": "http://127.0.0.1:9200"}))
        return
    env = os.environ.copy()
    env["ES_PATH_CONF"] = str(root / "config")
    env["ES_JAVA_OPTS"] = env.get("ES_JAVA_OPTS", "-Xms512m -Xmx512m")
    with log_file.open("ab") as output:
        proc = subprocess.Popen(
            [str(home / "bin" / "elasticsearch")],
            cwd=str(home),
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    ready = _wait_ready(args.timeout)
    if not ready:
        raise SystemExit(f"Elasticsearch did not become ready in {args.timeout}s; see {log_file}")
    pid = int(pid_file.read_text().strip()) if pid_file.exists() else proc.pid
    print(json.dumps({"running": True, "pid": pid, "url": "http://127.0.0.1:9200"}))


def _runtime_root() -> Path:
    ext_root = Path(os.getenv("PERSONAL_NEWS_EXT_ROOT", "/Volumes/ext"))
    return ext_root / "personal_news_agent" / "runtime" / "elasticsearch"


def _is_running(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
        return True
    except Exception:
        pid_file.unlink(missing_ok=True)
    return False


def _clean_appledouble(root: Path) -> None:
    for path in root.rglob("._*"):
        try:
            path.unlink()
        except OSError:
            pass


def _wait_ready(timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:9200", timeout=3) as response:
                if response.status == 200:
                    return True
        except (TimeoutError, urllib.error.URLError):
            time.sleep(2)
    return False


if __name__ == "__main__":
    main()
