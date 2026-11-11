#!/usr/bin/env bash
# One-click launcher for the full-duplex digital human:
#   MuseTalk streaming server (lip-sync video, :8000)
#   MiniCPM-o demo: backend :22500, worker :22400, gateway + web UI :8006
# Both share ./.venv. GPU placement comes from .avatar-env or defaults to GPU 0.
#
# Usage: ./start_avatar.sh [start|stop|status]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/service_lifecycle.sh
source "${ROOT}/scripts/service_lifecycle.sh"
# Optional project-local overrides. Keep the checked-in example portable and
# do not commit machine-specific GPU UUIDs.
AVATAR_ENV_FILE="${AVATAR_ENV_FILE:-${ROOT}/.avatar-env}"
if [[ "${AVATAR_ENV_FILE}" != /* ]]; then
    AVATAR_ENV_FILE="${ROOT}/${AVATAR_ENV_FILE}"
fi
if [[ -f "${AVATAR_ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${AVATAR_ENV_FILE}"
fi

# MiniCPM-o and MuseTalk may run on different physical GPUs. Falling back to
# CUDA_VISIBLE_DEVICES preserves the original single-GPU deployment.
MINICPM_CUDA_VISIBLE_DEVICES="${MINICPM_CUDA_VISIBLE_DEVICES:-${CUDA_VISIBLE_DEVICES:-0}}"
MUSETALK_CUDA_VISIBLE_DEVICES="${MUSETALK_CUDA_VISIBLE_DEVICES:-${MINICPM_CUDA_VISIBLE_DEVICES}}"
export CUDA_VISIBLE_DEVICES="${MINICPM_CUDA_VISIBLE_DEVICES}"
export AVATAR_STREAM_FORMAT="${AVATAR_STREAM_FORMAT:-h264}"
export AVATAR_STREAM_MAX_WIDTH="${AVATAR_STREAM_MAX_WIDTH:-0}"
export AVATAR_H264_BITRATE="${AVATAR_H264_BITRATE:-2M}"
export AVATAR_H264_MAXRATE="${AVATAR_H264_MAXRATE:-3M}"
export AVATAR_H264_BUFSIZE="${AVATAR_H264_BUFSIZE:-2M}"
export AVATAR_H264_GOP="${AVATAR_H264_GOP:-120}"
export AVATAR_H264_PRESET="${AVATAR_H264_PRESET:-ultrafast}"
export AVATAR_H264_PROFILE="${AVATAR_H264_PROFILE:-main}"
export AVATAR_H264_LEVEL="${AVATAR_H264_LEVEL:-4.0}"
export AVATAR_H264_CODEC_STRING="${AVATAR_H264_CODEC_STRING:-avc1.4D4028}"

VENV_DIR="${AVATAR_VENV:-${ROOT}/.venv}"
PYTHON="${VENV_DIR}/bin/python"
MINICPM_DIR="${ROOT}/minicpm_demo"
MUSETALK_PROJECT="${ROOT}/musetalk_project"
MUSETALK_DIR="${MUSETALK_PROJECT}/MuseTalk"
# The 720p knowledge-sharing avatar is the default. Set MUSETALK_CONFIG to
# configs/streaming_duplex.yaml to run the original avatar cache as fallback.
MUSETALK_CONFIG="${MUSETALK_CONFIG:-configs/streaming_duplex_knowledge.yaml}"
MUSETALK_PORT="${MUSETALK_PORT:-8000}"
GATEWAY_PORT="${DEMO_PORT:-8006}"
GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT:-8007}"
BACKEND_PORT="${BACKEND_PORT:-22500}"
WORKER_PORT="${WORKER_PORT:-22400}"
RUN_DIR="${ROOT}/.run"
LOG_DIR="${RUN_DIR}/logs"
PID_DIR="${RUN_DIR}/pids"
MUSETALK_PID="${PID_DIR}/musetalk.pid"
LOCK_FILE="${RUN_DIR}/avatar.lock"
MINICPM_PID_DIR="${MINICPM_DIR}/tmp/pids"

mkdir -p "${LOG_DIR}" "${PID_DIR}"

is_running() {
    local pid_file="$1"
    local marker="$2"
    local pid
    [[ -s "${pid_file}" ]] || return 1
    pid="$(cat "${pid_file}")"
    service_pid_is_owned "${pid}" "${marker}"
}

pid_value() {
    local pid_file="$1"
    [[ -s "${pid_file}" ]] || return 1
    cat "${pid_file}"
}

minicpm_marker() {
    case "$1" in
        gateway) echo "gateway.py" ;;
        backend) echo "py_backend.server" ;;
        worker) echo "worker.py" ;;
        *) return 1 ;;
    esac
}

minicpm_pid_file() {
    echo "${MINICPM_PID_DIR}/$1.pid"
}

minicpm_health_url() {
    case "$1" in
        gateway) echo "https://127.0.0.1:${GATEWAY_PORT}/health" ;;
        backend) echo "http://127.0.0.1:${BACKEND_PORT}/health" ;;
        worker) echo "http://127.0.0.1:${WORKER_PORT}/health" ;;
        *) return 1 ;;
    esac
}

minicpm_service_owned() {
    local name="$1"
    is_running "$(minicpm_pid_file "${name}")" "$(minicpm_marker "${name}")"
}

minicpm_service_healthy() {
    local name="$1"
    minicpm_service_owned "${name}" || return 1
    service_http_ok "$(minicpm_health_url "${name}")"
}

worker_is_registered() {
    local response
    response="$(curl -skf --max-time 2 \
        "https://127.0.0.1:${GATEWAY_PORT}/workers" 2>/dev/null)" || return 1
    [[ "${response}" == *'"worker_id":"worker-0"'* ]]
}

musetalk_is_healthy() {
    local response
    is_running "${MUSETALK_PID}" "stream_server.py" || return 1
    response="$(curl -sf --max-time 2 \
        "http://127.0.0.1:${MUSETALK_PORT}/api/status" 2>/dev/null)" || return 1
    [[ "${response}" == *'"ready":true'* ]]
}

all_services_healthy() {
    musetalk_is_healthy \
        && minicpm_service_healthy gateway \
        && minicpm_service_healthy backend \
        && minicpm_service_healthy worker \
        && worker_is_registered
}

any_service_owned() {
    local name
    is_running "${MUSETALK_PID}" "stream_server.py" && return 0
    for name in gateway backend worker; do
        minicpm_service_owned "${name}" && return 0
    done
    return 1
}

port_has_expected_owner() {
    local port="$1"
    local name="$2"
    local pid_file expected_pid owner_pid

    if [[ "${name}" == "musetalk" ]]; then
        pid_file="${MUSETALK_PID}"
    else
        pid_file="$(minicpm_pid_file "${name}")"
    fi
    expected_pid="$(pid_value "${pid_file}")" || return 1

    if [[ "${name}" == "musetalk" ]]; then
        service_pid_is_owned "${expected_pid}" "stream_server.py" || return 1
    else
        service_pid_is_owned "${expected_pid}" "$(minicpm_marker "${name}")" || return 1
    fi

    while IFS= read -r owner_pid; do
        [[ "${owner_pid}" == "${expected_pid}" ]] && return 0
    done < <(service_port_owner_pids "${port}")
    return 1
}

all_ports() {
    printf '%s\n' \
        "${MUSETALK_PORT}" \
        "${GATEWAY_PORT}" \
        "${GATEWAY_INTERNAL_PORT}" \
        "${BACKEND_PORT}" \
        "${WORKER_PORT}"
}

print_service_states() {
    local name state
    if is_running "${MUSETALK_PID}" "stream_server.py"; then
        if musetalk_is_healthy; then
            state="running (healthy)"
        else
            state="running (unhealthy)"
        fi
        printf '  %-9s %s, pid %s\n' "MuseTalk:" "${state}" "$(pid_value "${MUSETALK_PID}")"
    else
        printf '  %-9s stopped\n' "MuseTalk:"
    fi

    for name in gateway backend worker; do
        if minicpm_service_owned "${name}"; then
            if minicpm_service_healthy "${name}"; then
                state="running (healthy)"
            else
                state="running (unhealthy)"
            fi
            printf '  %-9s %s, pid %s\n' "${name}:" "${state}" "$(pid_value "$(minicpm_pid_file "${name}")")"
        else
            printf '  %-9s stopped\n' "${name}:"
        fi
    done
}

has_unknown_listener() {
    local port
    local known_ports=(
        "${MUSETALK_PORT}:musetalk"
        "${GATEWAY_PORT}:gateway"
        "${GATEWAY_INTERNAL_PORT}:gateway"
        "${BACKEND_PORT}:backend"
        "${WORKER_PORT}:worker"
    )
    local entry

    for entry in "${known_ports[@]}"; do
        port="${entry%%:*}"
        if service_port_is_listening "${port}" \
            && ! port_has_expected_owner "${port}" "${entry#*:}"; then
            return 0
        fi
    done
    return 1
}

print_unknown_listeners() {
    local known_ports=(
        "${MUSETALK_PORT}:musetalk"
        "${GATEWAY_PORT}:gateway"
        "${GATEWAY_INTERNAL_PORT}:gateway"
        "${BACKEND_PORT}:backend"
        "${WORKER_PORT}:worker"
    )
    local entry port

    for entry in "${known_ports[@]}"; do
        port="${entry%%:*}"
        if service_port_is_listening "${port}" \
            && ! port_has_expected_owner "${port}" "${entry#*:}"; then
            echo "Port ${port} is occupied by a process outside this stack:" >&2
            service_print_port_owner "${port}" >&2
        fi
    done
}

check_ports_free() {
    local port
    while IFS= read -r port; do
        if service_port_is_listening "${port}"; then
            echo "Port ${port} is already in use:" >&2
            service_print_port_owner "${port}" >&2
            return 1
        fi
    done < <(all_ports)
}

wait_musetalk() {
    local pid="${1:-}"
    local timeout="${2:-300}"
    local deadline=$((SECONDS + timeout))
    local response

    while true; do
        if ! service_pid_is_active "${pid}"; then
            echo "MuseTalk exited during startup, see ${LOG_DIR}/musetalk.log" >&2
            tail -n 25 "${LOG_DIR}/musetalk.log" >&2 || true
            return 1
        fi
        response="$(curl -sf --max-time 2 \
            "http://127.0.0.1:${MUSETALK_PORT}/api/status" 2>/dev/null)" || response=""
        if [[ "${response}" == *'"ready":true'* ]]; then
            echo "MuseTalk ready: http://127.0.0.1:${MUSETALK_PORT}/"
            return 0
        fi
        (( SECONDS < deadline )) || break
        sleep 1
    done
    echo "MuseTalk did not become ready within ${timeout}s, see ${LOG_DIR}/musetalk.log" >&2
    tail -n 25 "${LOG_DIR}/musetalk.log" >&2 || true
    return 1
}

start_musetalk() {
    if is_running "${MUSETALK_PID}" "stream_server.py"; then
        echo "MuseTalk is already running (pid $(cat "${MUSETALK_PID}"))."
        wait_musetalk "$(cat "${MUSETALK_PID}")" 300
        return 0
    fi
    rm -f "${MUSETALK_PID}"
    # Same environment as musetalk_project/activate.sh, but with the shared venv.
    setsid -f env \
        CUDA_VISIBLE_DEVICES="${MUSETALK_CUDA_VISIBLE_DEVICES}" \
        VIRTUAL_ENV="${VENV_DIR}" \
        PATH="${VENV_DIR}/bin:${MUSETALK_DIR}/ffmpeg-4.4-amd64-static:${PATH}" \
        XDG_CACHE_HOME="${MUSETALK_PROJECT}/.cache" \
        HF_HOME="${MUSETALK_PROJECT}/.cache/huggingface" \
        TORCH_HOME="${MUSETALK_PROJECT}/.cache/torch" \
        MPLCONFIGDIR="${MUSETALK_PROJECT}/.cache/matplotlib" \
        FFMPEG_PATH="${MUSETALK_DIR}/ffmpeg-4.4-amd64-static" \
        PYTHONUNBUFFERED=1 \
        bash -c 'echo "$$" > "$1"; cd "$2"; shift 2; exec "$@"' \
        _ "${MUSETALK_PID}" "${MUSETALK_DIR}" \
        "${PYTHON}" stream_server.py --config "${MUSETALK_CONFIG}" \
        9>&- >"${LOG_DIR}/musetalk.log" 2>&1
    for _ in $(seq 1 50); do
        [[ -s "${MUSETALK_PID}" ]] && break
        sleep 0.1
    done
    if [[ ! -s "${MUSETALK_PID}" ]]; then
        echo "Failed to start MuseTalk, see ${LOG_DIR}/musetalk.log" >&2
        return 1
    fi
    local pid
    pid="$(cat "${MUSETALK_PID}")"
    if ! service_pid_is_active "${pid}"; then
        echo "MuseTalk exited immediately, see ${LOG_DIR}/musetalk.log" >&2
        tail -n 25 "${LOG_DIR}/musetalk.log" >&2 || true
        return 1
    fi
    echo "Started MuseTalk: pid ${pid} config=${MUSETALK_CONFIG}"
    wait_musetalk "${pid}" 300
}

start_minicpm() {
    # The gateway forwards duplex output audio to MuseTalk over this socket,
    # and proxies MuseTalk's viewer endpoints for the /avatar page.
    MINICPM_CUDA_VISIBLE_DEVICES="${MINICPM_CUDA_VISIBLE_DEVICES}" \
    CUDA_VISIBLE_DEVICES="${MINICPM_CUDA_VISIBLE_DEVICES}" \
    AVATAR_BRIDGE_URL="${AVATAR_BRIDGE_URL-ws://127.0.0.1:${MUSETALK_PORT}/ws/minicpm}" \
    AVATAR_HTTP_URL="${AVATAR_HTTP_URL-http://127.0.0.1:${MUSETALK_PORT}}" \
        DEMO_VENV="${VENV_DIR}" \
        DEMO_PORT="${GATEWAY_PORT}" \
        GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT}" \
        BACKEND_PORT="${BACKEND_PORT}" \
        WORKER_PORT="${WORKER_PORT}" \
        "${MINICPM_DIR}/start_all.sh"
}

stop_all() {
    local rc=0

    DEMO_PORT="${GATEWAY_PORT}" \
    GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT}" \
    BACKEND_PORT="${BACKEND_PORT}" \
    WORKER_PORT="${WORKER_PORT}" \
    "${MINICPM_DIR}/stop_all.sh" || rc=1

    service_stop_pid_file \
        "${MUSETALK_PID}" "stream_server.py" "MuseTalk" 60 || rc=1
    if ! service_wait_port_free "${MUSETALK_PORT}" 60; then
        echo "MuseTalk: port ${MUSETALK_PORT} is still in use after shutdown" >&2
        service_print_port_owner "${MUSETALK_PORT}" >&2
        rc=1
    fi
    return "${rc}"
}

show_status() {
    print_service_states
}

case "${1:-start}" in
    start)
        if [[ ! -x "${PYTHON}" ]]; then
            echo "Missing shared venv: ${PYTHON}" >&2
            exit 1
        fi

        service_acquire_lock "${LOCK_FILE}" 10

        if all_services_healthy; then
            echo "Digital human services are already running and healthy."
            print_service_states
            exit 0
        fi

        if any_service_owned; then
            echo "Detected a partial digital-human startup:"
            print_service_states
            if has_unknown_listener; then
                echo "Cannot restart automatically because a non-stack process owns a required port:" >&2
                print_unknown_listeners
                exit 1
            fi
            echo "检测到半启动状态，正在自动清理并重启..."
            if ! stop_all; then
                echo "Failed to clean the previous startup." >&2
                exit 1
            fi
        elif has_unknown_listener; then
            echo "Cannot start the digital-human stack because a required port is in use:" >&2
            print_unknown_listeners
            exit 1
        fi

        check_ports_free

        START_COMPLETE=0
        rollback_on_error() {
            local rc=$?
            if (( rc != 0 && START_COMPLETE == 0 )); then
                echo "Digital-human startup failed; rolling back services started in this run." >&2
                stop_all || true
            fi
            return "${rc}"
        }
        trap rollback_on_error EXIT

        start_musetalk
        start_minicpm
        START_COMPLETE=1
        trap - EXIT

        echo
        echo "Digital human services started"
        echo "  Digital human page:        https://127.0.0.1:${GATEWAY_PORT}/avatar"
        echo "  MiniCPM-o only (no avatar): https://127.0.0.1:${GATEWAY_PORT}/omni"
        echo "  MuseTalk debug viewer:     http://127.0.0.1:${MUSETALK_PORT}/  (one viewer at a time)"
        echo "  Avatar bridge:             gateway -> ws://127.0.0.1:${MUSETALK_PORT}/ws/minicpm"
        echo "  MuseTalk log:              ${LOG_DIR}/musetalk.log"
        echo "  MiniCPM-o logs:            ${MINICPM_DIR}/.run-logs"
        echo "  MiniCPM GPU:               ${MINICPM_CUDA_VISIBLE_DEVICES}"
        echo "  MuseTalk GPU:              ${MUSETALK_CUDA_VISIBLE_DEVICES}"
        echo "  Avatar frame format:       ${AVATAR_STREAM_FORMAT}"
        echo "  Stop:                      $0 stop"
        ;;
    stop)
        service_acquire_lock "${LOCK_FILE}" 10
        stop_all
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 [start|stop|status]" >&2
        exit 2
        ;;
esac
