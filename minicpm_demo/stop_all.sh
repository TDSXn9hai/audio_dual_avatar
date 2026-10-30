#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="${DEMO_DIR}/tmp/pids"

for name in worker backend gateway; do
    pid_file="${PID_DIR}/${name}.pid"
    if [[ -f "${pid_file}" ]]; then
        pid="$(cat "${pid_file}")"
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}"
            echo "Stopped ${name}: pid ${pid}"
        fi
        rm -f "${pid_file}"
    fi
done
