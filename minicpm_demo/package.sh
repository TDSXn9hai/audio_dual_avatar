#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "${PROJECT_DIR}")"
PROJECT_NAME="$(basename "${PROJECT_DIR}")"
OUTPUT_PATH="${1:-${PARENT_DIR}/${PROJECT_NAME}.tar}"

echo "Creating portable archive: ${OUTPUT_PATH}"
tar \
    -C "${PARENT_DIR}" \
    --exclude="${PROJECT_NAME}/.venv" \
    --exclude="${PROJECT_NAME}/.bin" \
    --exclude="${PROJECT_NAME}/.run-logs" \
    --exclude="${PROJECT_NAME}/data" \
    --exclude="${PROJECT_NAME}/tmp" \
    --exclude="${PROJECT_NAME}/tests/results" \
    --exclude="${PROJECT_NAME}/__pycache__" \
    --exclude='*/__pycache__' \
    --exclude='*.pyc' \
    --exclude="${PROJECT_NAME}/.pytest_cache" \
    -cf "${OUTPUT_PATH}" \
    "${PROJECT_NAME}"

echo "Archive created:"
ls -lh "${OUTPUT_PATH}"
echo
echo "On the target machine:"
echo "  tar -xf $(basename "${OUTPUT_PATH}")"
echo "  cd ${PROJECT_NAME}"
echo "  ./install.sh"
