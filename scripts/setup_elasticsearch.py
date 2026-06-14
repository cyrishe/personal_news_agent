from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path


VERSION = "8.15.3"
BASE_URL = "https://artifacts.elastic.co/downloads/elasticsearch"


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a local Elasticsearch runtime under the external disk.")
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--root", type=Path, default=_runtime_root())
    args = parser.parse_args()

    args.root.mkdir(parents=True, exist_ok=True)
    archive = args.root / f"elasticsearch-{args.version}-{_platform_suffix()}.tar.gz"
    install_dir = args.root / f"elasticsearch-{args.version}"
    current = args.root / "current"
    if not install_dir.exists():
        url = f"{BASE_URL}/elasticsearch-{args.version}-{_platform_suffix()}.tar.gz"
        print(f"downloading {url}")
        urllib.request.urlretrieve(url, archive)
        print(f"extracting {archive}")
        with tarfile.open(archive) as tar:
            tar.extractall(args.root)
        extracted = args.root / f"elasticsearch-{args.version}"
        if not extracted.exists():
            candidates = sorted(args.root.glob(f"elasticsearch-{args.version}*"))
            if not candidates:
                raise SystemExit("Elasticsearch archive extracted no install directory")
            candidates[0].rename(extracted)
    if current.exists() or current.is_symlink():
        if current.is_symlink() or current.is_file():
            current.unlink()
        else:
            shutil.rmtree(current)
    current.symlink_to(install_dir, target_is_directory=True)
    _write_config(args.root, install_dir)
    print(f"installed={install_dir}")
    print(f"current={current}")


def _runtime_root() -> Path:
    ext_root = Path(os.getenv("PERSONAL_NEWS_EXT_ROOT", "/Volumes/ext"))
    return ext_root / "personal_news_agent" / "runtime" / "elasticsearch"


def _platform_suffix() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    if system == "darwin":
        return f"darwin-{arch}"
    if system == "linux":
        return f"linux-{arch}"
    raise SystemExit(f"Unsupported platform for Elasticsearch tarball: {system}-{machine}")


def _write_config(root: Path, install_dir: Path) -> None:
    config_dir = root / "config"
    data_dir = root / "data"
    logs_dir = root / "logs"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "elasticsearch.yml").write_text(
        "\n".join(
            [
                "cluster.name: personal-news-agent-local",
                "node.name: pna-local-1",
                "discovery.type: single-node",
                "network.host: 127.0.0.1",
                "http.port: 9200",
                f"path.data: {data_dir}",
                f"path.logs: {logs_dir}",
                "xpack.security.enabled: false",
                "xpack.security.enrollment.enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "jvm.options").write_text("-Xms512m\n-Xmx512m\n", encoding="utf-8")
    log4j_source = install_dir / "config" / "log4j2.properties"
    if log4j_source.exists():
        shutil.copy2(log4j_source, config_dir / "log4j2.properties")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        subprocess.run(["true"], check=False)
        sys.exit(130)
