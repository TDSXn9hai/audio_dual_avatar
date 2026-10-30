#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${DEMO_PORT:-8006}"

echo "Gateway:"
curl -sk --max-time 5 "https://127.0.0.1:${PORT}/status" || true
echo
echo "Backend:"
curl -s --max-time 5 http://127.0.0.1:22500/health || true
echo
echo "Worker:"
curl -s --max-time 5 http://127.0.0.1:22400/health || true
echo
echo "PIDs:"
for name in gateway backend worker; do
    pid_file="${PROJECT_DIR}/tmp/pids/${name}.pid"
    if [[ -f "${pid_file}" ]]; then
        pid="$(cat "${pid_file}")"
        if kill -0 "${pid}" 2>/dev/null; then
            echo "  ${name}: ${pid} (running)"
        else
            echo "  ${name}: ${pid} (stale)"
        fi
    else
        echo "  ${name}: no pid file"
    fi
done
