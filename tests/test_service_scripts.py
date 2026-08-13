from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = [
    ROOT / "start.sh",
    ROOT / "start_backend.sh",
    ROOT / "start_crawler.sh",
    ROOT / "scripts" / "install_systemd_services.sh",
    ROOT / "scripts" / "run_web_service.sh",
    ROOT / "scripts" / "run_crawler_service.sh",
    ROOT / "scripts" / "run_task_service.sh",
]
EXECUTABLE_ENTRYPOINTS = [
    ROOT / "start.sh",
    ROOT / "start_backend.sh",
    ROOT / "start_crawler.sh",
    ROOT / "scripts" / "install_systemd_services.sh",
]


@pytest.mark.parametrize("script", ENTRYPOINTS)
def test_service_entrypoints_have_valid_bash_syntax(script: Path):
    assert script.exists()
    subprocess.run(["bash", "-n", str(script)], cwd=ROOT, check=True)


@pytest.mark.parametrize("script", EXECUTABLE_ENTRYPOINTS)
def test_user_facing_service_entrypoints_are_executable(script: Path):
    assert script.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("script", ["start.sh", "start_backend.sh", "start_crawler.sh"])
def test_service_entrypoint_help_does_not_require_systemd(script: str):
    result = subprocess.run(
        [str(ROOT / script), "help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )
    assert "install" in result.stdout
    assert "restart" in result.stdout
    assert str(ROOT) in result.stdout


def test_preflight_does_not_require_systemd(tmp_path: Path):
    venv_dir = tmp_path / "venv"
    python_bin = venv_dir / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"-\" ]]; then\n"
        "  echo 'Preflight OK: fake isolated runtime and 48 sources loaded.'\n"
        "fi\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o700)
    env_file = tmp_path / "runtime.env"
    env_file.write_text(f'export PERSONAL_NEWS_VENV="{venv_dir}"\n', encoding="utf-8")
    result = subprocess.run(
        [str(ROOT / "start.sh"), "preflight"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PNA_RUNTIME_ENV_FILE": str(env_file)},
    )
    assert "Preflight OK" in result.stdout


def test_preflight_rejects_model_configuration_in_runtime_env(tmp_path: Path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "export PERSONAL_NEWS_VENV=/tmp/not-used\n"
        "PNA_CC_RUNTIME_AUTH_TOKEN=stale-key\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "start.sh"), "preflight"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PNA_RUNTIME_ENV_FILE": str(env_file)},
    )

    assert result.returncode == 1
    assert "model configuration belongs" in result.stderr


def test_preflight_script_rejects_retired_model_keys_in_root_env():
    source = (ROOT / "scripts" / "service_control_lib.sh").read_text(encoding="utf-8")

    for retired_key in (
        "LLM_",
        "ANTHROPIC_",
        "PNA_CC_RUNTIME_(BASE_URL|AUTH_TOKEN|API_KEY|MODEL)",
        "PNA_LOCAL_AGENT_(PROVIDER|BASE_URL|API_KEY|DEFAULT_MODEL|TIMEOUT_SECONDS)",
    ):
        assert retired_key in source
    assert "contains retired model keys" in source


def test_unknown_command_is_rejected_before_systemd_lookup():
    result = subprocess.run(
        [str(ROOT / "start.sh"), "not-a-command"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 2
    assert "Unknown command: not-a-command" in result.stderr


def test_systemd_units_run_launchers_through_bash():
    for name in ("web", "crawler", "tasks"):
        unit = (ROOT / "deploy" / "systemd" / f"personal-news-{name}.service.in").read_text(encoding="utf-8")
        assert "ExecStart=/bin/bash __PROJECT_DIR__/scripts/run_" in unit


def test_installer_supports_render_only_mode():
    source = (ROOT / "scripts" / "install_systemd_services.sh").read_text(encoding="utf-8")
    assert "--no-start" in source
    assert "systemctl enable personal-news.target" in source


def test_service_launchers_share_strict_python_runtime_selection():
    for name in ("web", "crawler", "task"):
        source = (ROOT / "scripts" / f"run_{name}_service.sh").read_text(encoding="utf-8")
        assert 'source "${PROJECT_DIR}/scripts/service_control_lib.sh"' in source
        assert 'PYTHON_BIN="$(pna_python_bin)"' in source


def test_service_launcher_rejects_missing_configured_python(tmp_path: Path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        f'export PERSONAL_NEWS_VENV="{tmp_path / "missing-venv"}"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(ROOT / "scripts" / "run_task_service.sh")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PNA_RUNTIME_ENV_FILE": str(env_file)},
    )
    assert result.returncode != 0
    assert "Configured virtualenv Python does not exist" in result.stderr


def test_server_runtime_template_uses_deployment_root_and_contains_no_credentials():
    source = (ROOT / "deploy" / "env.ext.server.example").read_text(encoding="utf-8")
    assert "/home/che/cyris/personal_news_agent" in source
    assert "PERSONAL_NEWS_VENV" in source
    assert "ACCESS_KEY" not in source
    assert "PASSWORD" not in source


def test_server_web_launcher_defaults_to_nginx_upstream_port():
    launcher = (ROOT / "scripts" / "run_web_service.sh").read_text(encoding="utf-8")
    controls = (ROOT / "scripts" / "service_control_lib.sh").read_text(encoding="utf-8")
    assert '${PNA_WEB_PORT:-22053}' in launcher
    assert '${PNA_WEB_PORT:-22053}' in controls


def test_delete_phone_user_script_has_valid_python_syntax():
    for name in ("delete_phone_user.py", "change_phone_user.py"):
        script = ROOT / "scripts" / name
        compile(script.read_text(encoding="utf-8"), str(script), "exec")

    delete_script = (ROOT / "scripts" / "delete_phone_user.py").read_text(encoding="utf-8")
    assert "PhoneVerificationService" not in delete_script
    assert "phone_challenge_secret" not in delete_script
