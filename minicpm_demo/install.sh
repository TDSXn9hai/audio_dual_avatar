#!/usr/bin/env bash
# Compatibility wrapper. The repository root installer is the single source of truth.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_INSTALLER="${PROJECT_DIR}/install.sh"

args=()
for arg in "$@"; do
    case "${arg}" in
        --no-start)
            # The root installer never starts services; retain this legacy flag.
            ;;
        --help|-h)
            exec "${ROOT_INSTALLER}" --help
            ;;
        *)
            args+=("${arg}")
            ;;
    esac
done

if [[ -n "${DEMO_VENV:-}" && -z "${AVATAR_VENV:-}" ]]; then
    export AVATAR_VENV="${DEMO_VENV}"
fi
if [[ -n "${DEMO_PYTHON:-}" && -z "${AVATAR_PYTHON:-}" ]]; then
    export AVATAR_PYTHON="${DEMO_PYTHON}"
fi

exec "${ROOT_INSTALLER}" "${args[@]}"
