#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${DEMO_DIR}/.." && pwd)"
# shellcheck source=../scripts/service_lifecycle.sh
source "${ROOT_DIR}/scripts/service_lifecycle.sh"
MODEL_DIR="${DEMO_DIR}/model"
VENV_DIR="${DEMO_VENV:-${DEMO_DIR}/.venv/base}"
PYTHON="${VENV_DIR}/bin/python"
LOG_DIR="${DEMO_DIR}/.run-logs"
PID_DIR="${DEMO_DIR}/tmp/pids"
DATA_DIR="${DEMO_DIR}/data"
REF_AUDIO="${DEMO_DIR}/assets/ref_audio/ref_minicpm_signature.wav"
GATEWAY_PORT="${DEMO_PORT:-8006}"
GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT:-8007}"
BACKEND_PORT="${BACKEND_PORT:-22500}"
WORKER_PORT="${WORKER_PORT:-22400}"
LOCK_FILE="${ROOT_DIR}/.run/avatar.lock"

mkdir -p "${LOG_DIR}" "${PID_DIR}" "${DATA_DIR}" "$(dirname "${LOCK_FILE}")"
cd "${DEMO_DIR}"

if [[ ! -x "${PYTHON}" ]]; then
    echo "Missing demo Python environment: ${PYTHON}" >&2
    exit 1
fi

if [[ ! -f "${MODEL_DIR}/config.json" ]]; then
    echo "Missing model config: ${MODEL_DIR}/config.json" >&2
    exit 1
fi

if [[ ! -f "${DEMO_DIR}/certs/cert.pem" || ! -f "${DEMO_DIR}/certs/key.pem" ]]; then
    echo "Missing TLS certificate in ${DEMO_DIR}/certs" >&2
    exit 1
fi

pid_file_for() {
    echo "${PID_DIR}/$1.pid"
}

pid_marker_for() {
    case "$1" in
        gateway) echo "gateway.py" ;;
        backend) echo "py_backend.server" ;;
        worker) echo "worker.py" ;;
        *) return 1 ;;
    esac
}

label_for() {
    case "$1" in
        gateway) echo "gateway" ;;
        backend) echo "backend" ;;
        worker) echo "worker" ;;
        *) echo "$1" ;;
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

health_url_for() {
    case "$1" in
        gateway) echo "https://127.0.0.1:${GATEWAY_PORT}/health" ;;
        backend) echo "http://127.0.0.1:${BACKEND_PORT}/health" ;;
        worker) echo "http://127.0.0.1:${WORKER_PORT}/health" ;;
        *) return 1 ;;
    esac
}

pid_value_for() {
    local pid_file
    pid_file="$(pid_file_for "$1")"
    [[ -s "${pid_file}" ]] || return 1
    cat "${pid_file}"
}

service_is_owned() {
    local name="$1"
    local pid
    pid="$(pid_value_for "${name}")" || return 1
    service_pid_is_owned "${pid}" "$(pid_marker_for "${name}")"
}

service_is_healthy() {
    local name="$1"
    service_is_owned "${name}" || return 1
    service_http_ok "$(health_url_for "${name}")"
}

worker_is_registered() {
    local response
    response="$(curl -skf --max-time 2 \
        "https://127.0.0.1:${GATEWAY_PORT}/workers" 2>/dev/null)" || return 1
    [[ "${response}" == *'"worker_id":"worker-0"'* ]]
}

all_services_healthy() {
    service_is_healthy gateway \
        && service_is_healthy backend \
        && service_is_healthy worker \
        && worker_is_registered
}

any_service_owned() {
    local name
    for name in gateway backend worker; do
        service_is_owned "${name}" && return 0
    done
    return 1
}

service_pid_is_registered() {
    local target_pid="$1"
    local name pid_file candidate_pid marker

    for name in gateway backend worker; do
        pid_file="$(pid_file_for "${name}")"
        [[ -s "${pid_file}" ]] || continue
        candidate_pid="$(cat "${pid_file}")"
        marker="$(pid_marker_for "${name}")"
        if [[ "${candidate_pid}" == "${target_pid}" ]] \
            && service_pid_is_owned "${candidate_pid}" "${marker}"; then
            return 0
        fi
    done
    return 1
}

unregistered_service_pids() {
    local marker pid
    for marker in gateway.py py_backend.server worker.py; do
        while IFS= read -r pid; do
            [[ -n "${pid}" ]] || continue
            service_pid_is_registered "${pid}" || printf '%s %s\n' "${pid}" "${marker}"
        done < <(service_find_workspace_pids "${marker}" "${DEMO_DIR}")
    done
}

print_unregistered_service_pids() {
    local pid marker
    while read -r pid marker; do
        [[ -n "${pid}" ]] || continue
        echo "Unregistered ${marker} process:" >&2
        ps -p "${pid}" -o pid=,ppid=,lstart=,args= >&2 || true
    done < <(unregistered_service_pids)
}

assert_no_unregistered_service_processes() {
    [[ -z "$(unregistered_service_pids)" ]] && return 0
    echo "Detected an unregistered MiniCPM-o service process before backend startup:" >&2
    print_unregistered_service_pids
    return 1
}

port_has_expected_owner() {
    local port="$1"
    local name="$2"
    local expected_pid owner_pid

    expected_pid="$(pid_value_for "${name}")" || return 1
    service_pid_is_owned "${expected_pid}" "$(pid_marker_for "${name}")" || return 1
    while IFS= read -r owner_pid; do
        [[ "${owner_pid}" == "${expected_pid}" ]] && return 0
    done < <(service_port_owner_pids "${port}")
    return 1
}

has_unknown_listener() {
    local name port
    local unknown=1
    for name in gateway backend worker; do
        while IFS= read -r port; do
            if service_port_is_listening "${port}" \
                && ! port_has_expected_owner "${port}" "${name}"; then
                unknown=0
            fi
        done < <(ports_for "${name}")
    done
    (( unknown == 0 ))
}

print_service_states() {
    local name state
    for name in gateway backend worker; do
        if service_is_owned "${name}"; then
            if service_is_healthy "${name}"; then
                state="running (healthy)"
            else
                state="running (unhealthy)"
            fi
            printf '  %-7s %s, pid %s\n' "${name}:" "${state}" "$(pid_value_for "${name}")"
        else
            printf '  %-7s stopped\n' "${name}:"
        fi
    done
}

print_unknown_listeners() {
    local name port
    for name in gateway backend worker; do
        while IFS= read -r port; do
            if service_port_is_listening "${port}" \
                && ! port_has_expected_owner "${port}" "${name}"; then
                echo "Port ${port} is occupied by a process outside this demo:" >&2
                service_print_port_owner "${port}" >&2
            fi
        done < <(ports_for "${name}")
    done
}

check_service_ports_free() {
    local name="$1"
    local port
    while IFS= read -r port; do
        if service_port_is_listening "${port}"; then
            echo "Port ${port} is already in use:" >&2
            service_print_port_owner "${port}" >&2
            return 1
        fi
    done < <(ports_for "${name}")
}

check_ports_free() {
    local name
    for name in gateway backend worker; do
        check_service_ports_free "${name}"
    done
}

start_process() {
    local name="$1"
    shift
    local pid_file="${PID_DIR}/${name}.pid"
    rm -f "${pid_file}"
    if command -v setsid >/dev/null 2>&1; then
        setsid -f bash -c 'echo "$$" > "$1"; shift; exec "$@"' \
            _ "${pid_file}" "$@" 9>&- >"${LOG_DIR}/${name}.log" 2>&1
    else
        nohup bash -c 'echo "$$" > "$1"; shift; exec "$@"' \
            _ "${pid_file}" "$@" 9>&- >"${LOG_DIR}/${name}.log" 2>&1 &
    fi
    for _ in $(seq 1 50); do
        [[ -s "${pid_file}" ]] && break
        sleep 0.1
    done
    if [[ ! -s "${pid_file}" ]]; then
        echo "Failed to start ${name}" >&2
        return 1
    fi
    if ! service_pid_is_active "$(cat "${pid_file}")"; then
        echo "${name} exited immediately; see ${LOG_DIR}/${name}.log" >&2
        tail -n 25 "${LOG_DIR}/${name}.log" >&2 || true
        return 1
    fi
    echo "Started ${name}: pid $(cat "${pid_file}")"
}

export PYTHONPATH="${DEMO_DIR}"
export PYTHONUNBUFFERED=1
# install.sh places a platform-compatible ffmpeg wrapper here.
export PATH="${DEMO_DIR}/.bin:${PATH}"

service_acquire_lock "${LOCK_FILE}" 10

if [[ -n "$(unregistered_service_pids)" ]]; then
    echo "Detected unregistered MiniCPM-o service processes:"
    print_unregistered_service_pids
    echo "检测到未登记的 MiniCPM-o 残留服务进程，正在自动清理并重启..."
    if ! "${DEMO_DIR}/stop_all.sh"; then
        echo "Failed to clean unregistered MiniCPM-o processes." >&2
        exit 1
    fi
elif all_services_healthy; then
    echo "MiniCPM-o demo is already running and healthy."
    exit 0
fi

if any_service_owned; then
    echo "Detected a partial MiniCPM-o startup:"
    print_service_states
    if has_unknown_listener; then
        echo "Cannot automatically restart because a non-demo process owns a required port:" >&2
        print_unknown_listeners
        exit 1
    fi
    echo "检测到 MiniCPM-o 半启动状态，正在自动清理并重启..."
    if ! "${DEMO_DIR}/stop_all.sh"; then
        echo "Failed to clean the previous MiniCPM-o startup." >&2
        exit 1
    fi
elif has_unknown_listener; then
    echo "Cannot start MiniCPM-o because a required port is already in use:" >&2
    print_unknown_listeners
    exit 1
fi

check_ports_free

STARTED_NAMES=()
START_COMPLETE=0
rollback_started() {
    local rc=$?
    local index name pid_file port

    if (( rc != 0 && START_COMPLETE == 0 )); then
        echo "MiniCPM-o startup failed; rolling back processes started in this run." >&2
        for (( index=${#STARTED_NAMES[@]}-1; index>=0; index-- )); do
            name="${STARTED_NAMES[index]}"
            pid_file="$(pid_file_for "${name}")"
            service_stop_pid_file \
                "${pid_file}" "$(pid_marker_for "${name}")" \
                "$(label_for "${name}")" 30 || true
            while IFS= read -r port; do
                service_wait_port_free "${port}" 30 || true
            done < <(ports_for "${name}")
        done
    fi
    return "${rc}"
}
trap rollback_started EXIT

start_process gateway \
    env ENABLE_PUBLIC_ADMIN=1 \
    "${PYTHON}" gateway.py \
    --host 0.0.0.0 \
    --port "${GATEWAY_PORT}" \
    --internal-port "${GATEWAY_INTERNAL_PORT}" \
    --https \
    --ssl-certfile "${DEMO_DIR}/certs/cert.pem" \
    --ssl-keyfile "${DEMO_DIR}/certs/key.pem" \
    --lang zh
STARTED_NAMES+=(gateway)
gateway_pid="$(pid_value_for gateway)"
service_wait_http \
    "https://127.0.0.1:${GATEWAY_PORT}/health" "gateway" \
    "${gateway_pid}" "${LOG_DIR}/gateway.log" 120
service_wait_http \
    "http://127.0.0.1:${GATEWAY_INTERNAL_PORT}/health" "gateway internal" \
    "${gateway_pid}" "${LOG_DIR}/gateway.log" 120

assert_no_unregistered_service_processes
check_service_ports_free backend

start_process backend env \
    CUDA_VISIBLE_DEVICES="${MINICPM_CUDA_VISIBLE_DEVICES:-${CUDA_VISIBLE_DEVICES:-0}}" \
    "${PYTHON}" -m py_backend.server \
    --host 0.0.0.0 \
    --port "${BACKEND_PORT}" \
    --gpu-id 0 \
    --model-path "${MODEL_DIR}" \
    --ref-audio-path "${REF_AUDIO}"
STARTED_NAMES+=(backend)
backend_pid="$(pid_value_for backend)"
service_wait_http \
    "http://127.0.0.1:${BACKEND_PORT}/health" "backend" \
    "${backend_pid}" "${LOG_DIR}/backend.log" 600

check_service_ports_free worker

start_process worker \
    "${PYTHON}" worker.py \
    --host 0.0.0.0 \
    --port "${WORKER_PORT}" \
    --gpu-id 0 \
    --worker-index 0 \
    --backend-server-url "http://127.0.0.1:${BACKEND_PORT}" \
    --ref-audio-path "${REF_AUDIO}"
STARTED_NAMES+=(worker)
worker_pid="$(pid_value_for worker)"
service_wait_http \
    "http://127.0.0.1:${WORKER_PORT}/health" "worker" \
    "${worker_pid}" "${LOG_DIR}/worker.log" 120

for ((i = 1; i <= 60; i++)); do
    if curl -sf -X PUT \
        -H 'content-type: application/json' \
        --data "{\"endpoint\":\"127.0.0.1:${WORKER_PORT}\",\"gpu_group\":\"gpu-0\"}" \
        "http://127.0.0.1:${GATEWAY_INTERNAL_PORT}/internal/workers/worker-0" >/dev/null; then
        echo "Worker registered: worker-0 -> 127.0.0.1:${WORKER_PORT}"
        break
    fi
    if ! service_pid_is_active "${worker_pid}"; then
        echo "worker exited during registration" >&2
        tail -n 25 "${LOG_DIR}/worker.log" >&2 || true
        exit 1
    fi
    sleep 2
    if [[ "${i}" -eq 60 ]]; then
        echo "Worker registration failed" >&2
        exit 1
    fi
done

START_COMPLETE=1
echo
echo "MiniCPM-o demo started"
echo "  UI:       https://127.0.0.1:${GATEWAY_PORT}/"
echo "  Health:   https://127.0.0.1:${GATEWAY_PORT}/health"
echo "  Workers:  https://127.0.0.1:${GATEWAY_PORT}/workers"
echo "  Admin:    https://127.0.0.1:${GATEWAY_PORT}/admin"
echo "  Logs:     ${LOG_DIR}"
echo "  PIDs:     ${PID_DIR}"

