#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/service_lifecycle.sh
source "${ROOT}/scripts/service_lifecycle.sh"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() {
    echo "PASS: $*"
}

cleanup_pids=()
TMP_DIR="$(mktemp -d)"
cleanup() {
    local pid
    for pid in "${cleanup_pids[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    rm -rf -- "${TMP_DIR}"
}
trap cleanup EXIT

start_tagged_sleep() {
    local marker="$1"
    bash -c 'exec -a "$1" sleep 30' _ "${marker}" </dev/null >/dev/null 2>&1 &
    SPAWNED_PID="$!"
    cleanup_pids+=("${SPAWNED_PID}")
}

start_listener() {
    local port_file="$1"
    python -c '
import socket
import sys
import time

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(("127.0.0.1", 0))
server.listen()
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(str(server.getsockname()[1]))
time.sleep(30)
' "${port_file}" </dev/null >/dev/null 2>&1 &
    SPAWNED_PID="$!"
    cleanup_pids+=("${SPAWNED_PID}")
    for _ in $(seq 1 50); do
        [[ -s "${port_file}" ]] && return 0
        sleep 0.1
    done
    fail "listener did not report its port"
}

start_python_sleeper() {
    local directory="$1"
    local marker="$2"
    (
        cd "${directory}"
        exec python -c 'import time; time.sleep(30)' "${marker}"
    ) </dev/null >/dev/null 2>&1 &
    SPAWNED_PID="$!"
    cleanup_pids+=("${SPAWNED_PID}")
}

test_pid_ownership_rejects_unrelated_process() {
    local owned_pid unrelated_pid
    start_tagged_sleep "avatar-lifecycle-owned"
    owned_pid="${SPAWNED_PID}"
    start_tagged_sleep "avatar-lifecycle-unrelated"
    unrelated_pid="${SPAWNED_PID}"

    service_pid_is_owned "${owned_pid}" "avatar-lifecycle-owned" \
        || fail "owned process was not recognized"
    if service_pid_is_owned "${unrelated_pid}" "avatar-lifecycle-owned"; then
        fail "unrelated process was treated as owned"
    fi

    pass "PID ownership distinguishes the expected command"
}

test_port_listener_is_detected() {
    local port_file="${TMP_DIR}/listener-port"
    local port
    start_listener "${port_file}"
    port="$(cat "${port_file}")"

    service_port_is_listening "${port}" \
        || fail "active listener on port ${port} was not detected"

    pass "active TCP listener is detected"
}

test_port_listener_pid_is_reported() {
    local port_file="${TMP_DIR}/owner-port"
    local port
    local owner_pid
    start_listener "${port_file}"
    port="$(cat "${port_file}")"
    owner_pid="${SPAWNED_PID}"

    [[ "$(service_port_owner_pids "${port}")" == "${owner_pid}" ]] \
        || fail "listener PID ${owner_pid} was not reported for port ${port}"

    pass "TCP listener PID is reported"
}

test_wait_port_free_succeeds_after_listener_exits() {
    local port_file="${TMP_DIR}/released-port"
    local port
    local listener_pid
    start_listener "${port_file}"
    port="$(cat "${port_file}")"
    listener_pid="${SPAWNED_PID}"

    kill "${listener_pid}"
    wait "${listener_pid}" 2>/dev/null || true
    service_wait_port_free "${port}" 3 \
        || fail "port ${port} was not observed as free after listener exit"
    kill -0 "${listener_pid}" 2>/dev/null \
        && fail "listener ${listener_pid} unexpectedly survived cleanup"

    pass "port release wait succeeds after listener exit"
}

test_wait_port_free_fails_while_listener_remains() {
    local port_file="${TMP_DIR}/busy-port"
    local port
    start_listener "${port_file}"
    port="$(cat "${port_file}")"

    if service_wait_port_free "${port}" 1; then
        fail "port ${port} was reported free while its listener was active"
    fi
    service_port_is_listening "${port}" \
        || fail "listener on port ${port} disappeared during timeout test"

    pass "port release wait times out while listener remains"
}

test_stop_pid_file_terminates_owned_process() {
    local pid_file="${TMP_DIR}/owned.pid"
    local owned_pid
    start_tagged_sleep "avatar-lifecycle-stop"
    owned_pid="${SPAWNED_PID}"
    printf '%s\n' "${owned_pid}" >"${pid_file}"

    service_stop_pid_file "${pid_file}" "avatar-lifecycle-stop" "test" 3 \
        || fail "owned process could not be stopped"

    if kill -0 "${owned_pid}" 2>/dev/null; then
        fail "owned process ${owned_pid} is still running"
    fi
    [[ ! -e "${pid_file}" ]] || fail "stale PID file was not removed"

    pass "owned process is stopped and its PID file removed"
}

test_stop_pid_terminates_owned_process_without_pid_file() {
    local owned_pid
    start_tagged_sleep "avatar-lifecycle-orphan-stop"
    owned_pid="${SPAWNED_PID}"

    service_stop_pid "${owned_pid}" "avatar-lifecycle-orphan-stop" "orphan" 3 \
        || fail "owned orphan process could not be stopped"

    if kill -0 "${owned_pid}" 2>/dev/null; then
        fail "owned orphan process ${owned_pid} is still running"
    fi

    pass "owned process is stopped without requiring a PID file"
}

test_stop_pid_file_does_not_signal_foreign_process() {
    local pid_file="${TMP_DIR}/foreign.pid"
    local foreign_pid
    start_tagged_sleep "avatar-lifecycle-foreign"
    foreign_pid="${SPAWNED_PID}"
    printf '%s\n' "${foreign_pid}" >"${pid_file}"

    service_stop_pid_file "${pid_file}" "avatar-lifecycle-stop" "test" 3 \
        || fail "stale PID handling returned failure"

    kill -0 "${foreign_pid}" 2>/dev/null \
        || fail "foreign process ${foreign_pid} was incorrectly signaled"
    [[ ! -e "${pid_file}" ]] || fail "stale PID file was not removed"

    pass "foreign process is left running and stale PID file removed"
}

test_wait_http_fails_fast_when_process_exits() {
    local log_file="${TMP_DIR}/dead-service.log"
    local dead_pid
    local started_at
    printf 'fatal startup error\n' >"${log_file}"
    declare -F service_wait_http >/dev/null \
        || fail "service_wait_http is not implemented"

    sleep 0.1 &
    dead_pid="$!"
    wait "${dead_pid}" 2>/dev/null || true

    started_at="${SECONDS}"
    if service_wait_http "http://127.0.0.1:1/health" "test" "${dead_pid}" "${log_file}" 20; then
        fail "dead process was reported as healthy"
    fi
    (( SECONDS - started_at < 3 )) \
        || fail "dead-process detection exceeded 3 seconds"

    pass "HTTP wait fails fast when its process exits"
}

test_workspace_process_detection_ignores_other_directories() {
    local workspace="${TMP_DIR}/workspace"
    local outside="${TMP_DIR}/outside"
    local inside_pid outside_pid detected
    mkdir -p "${workspace}" "${outside}"

    start_python_sleeper "${workspace}" "avatar-lifecycle-orphan"
    inside_pid="${SPAWNED_PID}"
    start_python_sleeper "${outside}" "avatar-lifecycle-orphan"
    outside_pid="${SPAWNED_PID}"

    detected="$(
        service_find_workspace_pids "avatar-lifecycle-orphan" "${workspace}"
    )"
    [[ "${detected}" == "${inside_pid}" ]] \
        || fail "workspace scan returned '${detected}', expected ${inside_pid} (outside=${outside_pid})"

    pass "workspace process scan ignores identical processes elsewhere"
}

test_pid_ownership_rejects_unrelated_process
test_port_listener_is_detected
test_port_listener_pid_is_reported
test_wait_port_free_succeeds_after_listener_exits
test_wait_port_free_fails_while_listener_remains
test_stop_pid_file_terminates_owned_process
test_stop_pid_terminates_owned_process_without_pid_file
test_stop_pid_file_does_not_signal_foreign_process
test_wait_http_fails_fast_when_process_exits
test_workspace_process_detection_ignores_other_directories
