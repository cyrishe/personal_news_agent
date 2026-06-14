#!/usr/bin/env bash
set -euo pipefail

EXT_ROOT="${PERSONAL_NEWS_EXT_ROOT:-/Volumes/ext}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PERSONAL_NEWS_VENV:-${EXT_ROOT}/venvs/personal_news_agent}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-${EXT_ROOT}/.cache/pip}"
CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-${EXT_ROOT}/conda_pkgs}"
CONDA_ENVS_PATH="${CONDA_ENVS_PATH:-${EXT_ROOT}/conda_envs}"

mkdir -p "${VENV_DIR}" "${PIP_CACHE_DIR}" "${CONDA_PKGS_DIRS}" "${CONDA_ENVS_PATH}"

echo "Using external disk paths:"
echo "  venv: ${VENV_DIR}"
echo "  pip cache: ${PIP_CACHE_DIR}"
echo "  conda pkgs: ${CONDA_PKGS_DIRS}"
echo "  conda envs: ${CONDA_ENVS_PATH}"

if [ ! -x "${VENV_DIR}/bin/python" ]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
PIP_CACHE_DIR="${PIP_CACHE_DIR}" "${VENV_DIR}/bin/pip" install -r "${PROJECT_DIR}/requirements.txt" pytest

cat > "${PROJECT_DIR}/.env.ext" <<EOF
export PERSONAL_NEWS_EXT_ROOT="${EXT_ROOT}"
export PERSONAL_NEWS_VENV="${VENV_DIR}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR}"
export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS}"
export CONDA_ENVS_PATH="${CONDA_ENVS_PATH}"
export PERSONAL_NEWS_DB="sqlite:///${PROJECT_DIR}/personal_news.db"
EOF

echo "Wrote ${PROJECT_DIR}/.env.ext"
echo "Run: source ${PROJECT_DIR}/.env.ext && ${VENV_DIR}/bin/uvicorn personal_news_agent.app:app --reload --port 8000"
