#!/bin/bash
# Isolated MiniCPM-o backend + worker for interruption experiments, so A/B runs neither queue
# behind nor share KV state with the production worker. Not registered with the gateway.
#
# Usage: bash test_instance.sh start|stop|status
# Env: TEST_GPU (physical GPU, default 0), TEST_BACKEND_PORT (22501), TEST_WORKER_PORT (22401),
#      TEST_LOG_DIR (minicpm_demo/tests/results/interruption/instance)
# Note: on the production GPU this shares compute with live sessions.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DEMO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-$DEMO/../.venv/bin/python}"
GPU="${TEST_GPU:-0}"
BPORT="${TEST_BACKEND_PORT:-22501}"
WPORT="${TEST_WORKER_PORT:-22401}"
LOG_DIR="${TEST_LOG_DIR:-$DEMO/tests/results/interruption/instance}"
REF="$DEMO/assets/ref_audio/ref_minicpm_signature.wav"
mkdir -p "$LOG_DIR"

wait_http() {  # url timeout_s
    for _ in $(seq 1 "$2"); do curl -sf "$1" >/dev/null && return 0; sleep 1; done
    echo "timeout waiting for $1" >&2; return 1
}

stop_one() {  # name port
    local f="$LOG_DIR/$1.pid"
    [[ -f "$f" ]] || return 0
    local pid; pid="$(cat "$f")"
    # only kill the process we started (its cmdline carries the test port)
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "--port $2"; then
        kill "$pid" && echo "stopped $1 ($pid)"
    fi
    rm -f "$f"
}

case "${1:-}" in
    start)
        cd "$DEMO"
        export CUDA_VISIBLE_DEVICES="$GPU" MINICPM_CUDA_VISIBLE_DEVICES="$GPU" PYTHONPATH="$DEMO" PYTHONUNBUFFERED=1
        setsid nohup "$PY" -m py_backend.server --host 127.0.0.1 --port "$BPORT" --gpu-id 0 \
            --model-path "$DEMO/model" --ref-audio-path "$REF" >> "$LOG_DIR/backend.log" 2>&1 < /dev/null &
        echo $! > "$LOG_DIR/backend.pid"
        wait_http "http://127.0.0.1:$BPORT/health" 600 || exit 1
        setsid nohup "$PY" worker.py --host 127.0.0.1 --port "$WPORT" --gpu-id 0 --worker-index 1 \
            --backend-server-url "http://127.0.0.1:$BPORT" --ref-audio-path "$REF" \
            >> "$LOG_DIR/worker.log" 2>&1 < /dev/null &
        echo $! > "$LOG_DIR/worker.pid"
        wait_http "http://127.0.0.1:$WPORT/health" 120 || exit 1
        echo "test worker ready: ws://127.0.0.1:$WPORT/v1/worker/duplex (backend log: $LOG_DIR/backend.log)"
        ;;
    stop)
        stop_one worker "$WPORT"
        stop_one backend "$BPORT"
        ;;
    status)
        curl -s "http://127.0.0.1:$BPORT/health"; echo
        curl -s "http://127.0.0.1:$WPORT/health"; echo
        ;;
    *) sed -n '2,8p' "$0"; exit 2 ;;
esac
