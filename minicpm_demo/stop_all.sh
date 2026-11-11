#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${DEMO_DIR}/.." && pwd)"
# shellcheck source=../scripts/service_lifecycle.sh
source "${ROOT_DIR}/scripts/service_lifecycle.sh"
PID_DIR="${DEMO_DIR}/tmp/pids"
LOCK_FILE="${ROOT_DIR}/.run/avatar.lock"
GATEWAY_PORT="${DEMO_PORT:-8006}"
GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT:-8007}"
BACKEND_PORT="${BACKEND_PORT:-22500}"
WORKER_PORT="${WORKER_PORT:-22400}"

service_acquire_lock "${LOCK_FILE}" 10

pid_marker_for() {
    case "$1" in
        gateway) echo "gateway.py" ;;
        backend) echo "py_backend.server" ;;
        worker) echo "worker.py" ;;
        *) return 1 ;;
    esac
}

ports_for() {
    case "$1" in
        gateway) printf '%s\n' "${GATEWAY_PORT}" "${GATEWAY_INTERNAL_PORT}" ;;
        backend) printf '%s\n' "${BACKEND_PORT}" ;;
        worker) printf '%s\n' "${WORKER_PORT}" ;;
        *) return 1 ;;
    esac
}

for name in worker backend gateway; do
    pid_file="${PID_DIR}/${name}.pid"
    service_stop_pid_file \
        "${pid_file}" "$(pid_marker_for "${name}")" "${name}" 30
done

for name in worker backend gateway; do
    while IFS= read -r port; do
        if ! service_wait_port_free "${port}" 30; then
            echo "${name}: port ${port} is still in use after shutdown" >&2
            service_print_port_owner "${port}" >&2
            exit 1
        fi
    done < <(ports_for "${name}")
done
