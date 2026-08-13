#!/usr/bin/env bash

# Shared systemd control functions for the server-facing entrypoints in the
# repository root. This file is sourced; do not execute it directly.

PNA_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PNA_ALL_UNITS=(
  personal-news-web.service
  personal-news-crawler.service
  personal-news-tasks.service
)
PNA_SCOPE_UNITS=()
PNA_SUDO=()

pna_usage() {
  local script_name="$1"
  cat <<EOF
Usage: ./${script_name} [command]

Commands:
  install   Render/install systemd units, then start and verify services
  start     Start services and verify their state (default)
  restart   Reinstall current unit templates, restart, and verify services
  stop      Stop services
  status    Show complete systemd status
  logs      Show the latest service logs
  follow    Follow service logs until interrupted
  health    Check the Web health endpoint
  preflight Validate environment, Python runtime, and source configuration
  help      Show this message

Project directory is detected from the script location:
  ${PNA_PROJECT_DIR}
EOF
}

pna_set_scope() {
  case "$1" in
    all)
      PNA_SCOPE_UNITS=("${PNA_ALL_UNITS[@]}")
      ;;
    backend)
      PNA_SCOPE_UNITS=(personal-news-web.service personal-news-tasks.service)
      ;;
    crawler)
      PNA_SCOPE_UNITS=(personal-news-crawler.service)
      ;;
    *)
      echo "Unknown service scope: $1" >&2
      return 2
      ;;
  esac
}

pna_require_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required; run this command on the Linux service host." >&2
    return 1
  fi
  if [[ "${EUID}" -eq 0 ]]; then
    PNA_SUDO=()
  elif command -v sudo >/dev/null 2>&1; then
    PNA_SUDO=(sudo)
  else
    echo "Run as root or install sudo before managing services." >&2
    return 1
  fi
}

pna_load_runtime_env() {
  local runtime_env_file="${PNA_RUNTIME_ENV_FILE:-${PNA_PROJECT_DIR}/.env.ext}"
  if [[ -r "${runtime_env_file}" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${runtime_env_file}"
    set +a
  fi
  pna_load_phone_env
}

pna_load_phone_env() {
  local phone_env_file="${PNA_PHONE_ENV_FILE:-}"
  if [[ -z "${phone_env_file}" ]]; then
    return 0
  fi
  if [[ ! -r "${phone_env_file}" ]]; then
    echo "Configured phone environment file is not readable: ${phone_env_file}" >&2
    return 1
  fi

  # Share only the phone-verification contract. Sourcing another application's
  # complete .env would also override this service's database and LLM settings.
  local key value
  while IFS='=' read -r key value; do
    key="${key#export }"
    case "${key}" in
      FIN_AGENT_PHONE_CHALLENGE_PROVIDER|FIN_AGENT_PHONE_CHALLENGE_SECRET|FIN_AGENT_PHONE_CHALLENGE_MOCK_ENABLED|FIN_AGENT_PHONE_CHALLENGE_TTL_SECONDS|FIN_AGENT_PHONE_CHALLENGE_RESEND_SECONDS|FIN_AGENT_PHONE_CHALLENGE_REQUEST_TIMEOUT_SECONDS|FIN_AGENT_PHONE_CHALLENGE_MAX_ATTEMPTS|FIN_AGENT_PHONE_CHALLENGE_RATE_WINDOW_SECONDS|FIN_AGENT_PHONE_CHALLENGE_MOBILE_RATE_LIMIT|FIN_AGENT_PHONE_CHALLENGE_IP_RATE_LIMIT|FIN_AGENT_PNVS_SIGN_NAME|FIN_AGENT_PNVS_TEMPLATE_CODE|FIN_AGENT_PNVS_SCHEME_NAME|FIN_AGENT_PNVS_ENDPOINT)
        value="${value%$'\r'}"
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        export "${key}=${value}"
        ;;
    esac
  done < "${phone_env_file}"
}

pna_python_bin() {
  if [[ -n "${PERSONAL_NEWS_VENV:-}" ]]; then
    if [[ ! -x "${PERSONAL_NEWS_VENV}/bin/python" ]]; then
      echo "Configured virtualenv Python does not exist: ${PERSONAL_NEWS_VENV}/bin/python" >&2
      return 1
    fi
    printf '%s\n' "${PERSONAL_NEWS_VENV}/bin/python"
    return 0
  fi
  command -v python3
}

pna_preflight() {
  cd "${PNA_PROJECT_DIR}"
  pna_load_runtime_env

  if [[ ! -f "${PNA_PROJECT_DIR}/.env" ]]; then
    echo "WARNING: ${PNA_PROJECT_DIR}/.env is missing; application defaults will be used." >&2
  elif grep -Eq '^[[:space:]]*(export[[:space:]]+)?(LLM_|ANTHROPIC_|PNA_CC_RUNTIME_(BASE_URL|AUTH_TOKEN|API_KEY|MODEL)|PNA_LOCAL_AGENT_(PROVIDER|BASE_URL|API_KEY|DEFAULT_MODEL|TIMEOUT_SECONDS))' "${PNA_PROJECT_DIR}/.env"; then
    echo "Preflight failed: ${PNA_PROJECT_DIR}/.env contains retired model keys; use only the fixed PNA_LLM_* contract." >&2
    return 1
  fi
  local runtime_env_file="${PNA_RUNTIME_ENV_FILE:-${PNA_PROJECT_DIR}/.env.ext}"
  if [[ ! -r "${runtime_env_file}" ]]; then
    echo "WARNING: ${runtime_env_file} is missing; launcher defaults will be used." >&2
  elif grep -Eq '^[[:space:]]*(export[[:space:]]+)?(PNA_LLM_|LLM_|PNA_CC_RUNTIME_|ANTHROPIC_|PNA_LOCAL_AGENT_)' "${runtime_env_file}"; then
    echo "Preflight failed: model configuration belongs in ${PNA_PROJECT_DIR}/.env, not ${runtime_env_file}." >&2
    return 1
  fi
  if [[ ! -f "${PNA_PROJECT_DIR}/sources.yaml" ]]; then
    echo "Missing source registry: ${PNA_PROJECT_DIR}/sources.yaml" >&2
    return 1
  fi

  local python_bin
  python_bin="$(pna_python_bin)" || return 1
  "${python_bin}" -m pip check
  "${python_bin}" - <<'PY'
from pathlib import Path
from urllib.parse import urlsplit

import fastapi
import starlette
import uvicorn  # noqa: F401

from personal_news_agent.config import settings
from personal_news_agent.services.source_registry import SourceRegistryService

if settings.phone_challenge_provider.strip().lower() == "aliyun_pnvs":
    from alibabacloud_dypnsapi20170525.client import Client  # noqa: F401
    from alibabacloud_tea_openapi import models as open_api_models  # noqa: F401
    from alibabacloud_tea_util import models as util_models  # noqa: F401

fastapi.FastAPI(title="Personal News Agent preflight")
registry = SourceRegistryService(Path("sources.yaml"))
registry.load()
llm_url = (settings.llm_endpoint or "").rstrip("/")
llm_host = (urlsplit(llm_url).hostname or "").lower()
cc_url = settings.effective_cc_runtime_base_url.rstrip("/")
cc_host = (urlsplit(cc_url).hostname or "").lower()
cc_summary = f"CC host {cc_host or 'not configured'}"
if llm_url != "https://api.deepseek.com":
    raise SystemExit(
        "Preflight failed: PNA_LLM_ENDPOINT must be exactly https://api.deepseek.com."
    )
if not settings.llm_key:
    raise SystemExit("Preflight failed: DeepSeek requires PNA_LLM_KEY.")
if settings.cc_runtime_enabled:
    if cc_url != "https://api.deepseek.com/anthropic":
        raise SystemExit(
            "Preflight failed: DeepSeek CC Runtime endpoint derivation is invalid."
        )
    if settings.effective_cc_runtime_auth_token != settings.llm_key:
        raise SystemExit("Preflight failed: DeepSeek CC Runtime must reuse PNA_LLM_KEY.")
    cc_summary = "DeepSeek shared PNA_LLM_KEY"
print(
    "Preflight OK: "
    f"FastAPI {fastapi.__version__}, Starlette {starlette.__version__}, "
    f"{len(registry.all_sources())} sources loaded, "
    f"LLM host {llm_host or 'not configured'}, runtime model {settings.effective_runtime_model}, {cc_summary}, "
    f"phone provider {settings.phone_challenge_provider}, "
    "phone credentials AccessKeyID/AccessKeySecret."
)
PY
}

pna_install_units() {
  "${PNA_PROJECT_DIR}/scripts/install_systemd_services.sh" --no-start
}

pna_systemctl() {
  "${PNA_SUDO[@]}" systemctl "$@"
}

pna_wait_units() {
  local timeout="${PNA_SERVICE_START_TIMEOUT_SECONDS:-30}"
  if [[ ! "${timeout}" =~ ^[0-9]+$ ]] || [[ "${timeout}" -lt 1 ]]; then
    timeout=30
  fi

  local unit attempt
  for unit in "${PNA_SCOPE_UNITS[@]}"; do
    for ((attempt = 1; attempt <= timeout; attempt += 1)); do
      if pna_systemctl is-active --quiet "${unit}"; then
        break
      fi
      sleep 1
    done
    if ! pna_systemctl is-active --quiet "${unit}"; then
      echo "Service failed to become active: ${unit}" >&2
      pna_systemctl --no-pager --full status "${unit}" || true
      "${PNA_SUDO[@]}" journalctl -u "${unit}" -n 80 --no-pager || true
      return 1
    fi
  done
}

pna_health_url() {
  local port="${PNA_WEB_PORT:-22053}"
  printf '%s\n' "${PNA_HEALTH_URL:-http://127.0.0.1:${port}/api/health}"
}

pna_health() {
  pna_load_runtime_env
  local url timeout attempt response
  url="$(pna_health_url)"
  timeout="${PNA_HEALTH_TIMEOUT_SECONDS:-30}"
  if [[ ! "${timeout}" =~ ^[0-9]+$ ]] || [[ "${timeout}" -lt 1 ]]; then
    timeout=30
  fi

  for ((attempt = 1; attempt <= timeout; attempt += 1)); do
    if command -v curl >/dev/null 2>&1; then
      if response="$(curl --fail --silent --show-error --max-time 3 "${url}" 2>/dev/null)"; then
        printf 'Health OK: %s\n%s\n' "${url}" "${response}"
        return 0
      fi
    else
      local python_bin
      python_bin="$(pna_python_bin)" || return 1
      if response="$("${python_bin}" - "${url}" <<'PY' 2>/dev/null
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
    print(response.read().decode("utf-8"))
PY
)"; then
        printf 'Health OK: %s\n%s\n' "${url}" "${response}"
        return 0
      fi
    fi
    sleep 1
  done
  echo "Health check failed after ${timeout}s: ${url}" >&2
  return 1
}

pna_status() {
  pna_systemctl --no-pager --full status "${PNA_SCOPE_UNITS[@]}"
}

pna_logs() {
  local follow="$1"
  local lines="${PNA_LOG_LINES:-100}"
  if [[ ! "${lines}" =~ ^[0-9]+$ ]] || [[ "${lines}" -lt 1 ]]; then
    lines=100
  fi
  local args=(-n "${lines}") unit
  for unit in "${PNA_SCOPE_UNITS[@]}"; do
    args+=(-u "${unit}")
  done
  if [[ "${follow}" == "1" ]]; then
    args+=(-f)
  else
    args+=(--no-pager)
  fi
  "${PNA_SUDO[@]}" journalctl "${args[@]}"
}

pna_verify() {
  pna_wait_units
  if [[ "$1" != "crawler" ]]; then
    pna_health
  fi
  pna_status
}

pna_manage_scope() {
  local scope="$1"
  local script_name="$2"
  local action="${3:-start}"
  pna_set_scope "${scope}" || return

  case "${action}" in
    help|-h|--help)
      pna_usage "${script_name}"
      return 0
      ;;
    preflight)
      pna_preflight
      return
      ;;
    health)
      pna_health
      return
      ;;
    install|start|restart|stop|status|logs|follow)
      ;;
    *)
      echo "Unknown command: ${action}" >&2
      pna_usage "${script_name}" >&2
      return 2
      ;;
  esac

  pna_require_systemd
  cd "${PNA_PROJECT_DIR}"

  case "${action}" in
    install)
      pna_preflight
      pna_install_units
      pna_systemctl restart "${PNA_SCOPE_UNITS[@]}"
      if [[ "${scope}" == "all" ]]; then
        pna_systemctl start personal-news.target
      fi
      pna_verify "${scope}"
      ;;
    start)
      pna_preflight
      if [[ "${scope}" == "all" ]]; then
        pna_systemctl start personal-news.target
      else
        pna_systemctl start "${PNA_SCOPE_UNITS[@]}"
      fi
      pna_verify "${scope}"
      ;;
    restart)
      pna_preflight
      pna_install_units
      pna_systemctl restart "${PNA_SCOPE_UNITS[@]}"
      if [[ "${scope}" == "all" ]]; then
        pna_systemctl start personal-news.target
      fi
      pna_verify "${scope}"
      ;;
    stop)
      if [[ "${scope}" == "all" ]]; then
        pna_systemctl stop personal-news.target
        pna_systemctl stop "${PNA_SCOPE_UNITS[@]}"
      else
        pna_systemctl stop "${PNA_SCOPE_UNITS[@]}"
      fi
      ;;
    status)
      pna_status
      ;;
    logs)
      pna_logs 0
      ;;
    follow)
      pna_logs 1
      ;;
  esac
}
