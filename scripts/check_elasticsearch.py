from __future__ import annotations

import json
import urllib.request


def main() -> None:
    with urllib.request.urlopen("http://127.0.0.1:9200/_cluster/health", timeout=5) as response:
        health = json.loads(response.read().decode("utf-8"))
    with urllib.request.urlopen("http://127.0.0.1:9200", timeout=5) as response:
        root = json.loads(response.read().decode("utf-8"))
    print(json.dumps({"ready": True, "cluster_status": health.get("status"), "version": root.get("version", {}).get("number")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
