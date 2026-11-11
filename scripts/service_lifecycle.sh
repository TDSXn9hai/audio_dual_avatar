#!/usr/bin/env bash
# Shared lifecycle helpers for the avatar startup scripts.

service_pid_is_owned() {
    local pid="${1:-}"
    local marker="${2:-}"
    local cmdline

    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    [[ -n "${marker}" ]] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1

    cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null)" || return 1
    [[ "${cmdline}" == *"${marker}"* ]]
}

service_acquire_lock() {
    local lock_file="${1:-}"
    local timeout="${2:-10}"

    if [[ "${AVATAR_LIFECYCLE_LOCK_HELD:-0}" == "1" ]]; then
        return 0
    fi
    [[ -n "${lock_file}" ]] || return 1

    mkdir -p "$(dirname "${lock_file}")"
    exec 9>"${lock_file}"
    if ! flock -w "${timeout}" 9; then
        echo "Another avatar lifecycle operation is still running (lock: ${lock_file})" >&2
        return 1
    fi
    export AVATAR_LIFECYCLE_LOCK_HELD=1
}

service_port_is_listening() {
    local port="${1:-}"
    [[ "${port}" =~ ^[0-9]+$ ]] || return 1
    [[ -n "$(ss -H -ltn "sport = :${port}" 2>/dev/null)" ]]
}

service_port_owner_pids() {
    local port="${1:-}"
    [[ "${port}" =~ ^[0-9]+$ ]] || return 1
    ss -H -ltnp "sport = :${port}" 2>/dev/null \
        | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
        | sort -u
}

service_print_port_owner() {
    local port="${1:-}"
    local pid
    local found=0

    while IFS= read -r pid; do
        [[ -n "${pid}" ]] || continue
        found=1
        ps -p "${pid}" -o pid=,ppid=,args= 2>/dev/null || true
    done < <(service_port_owner_pids "${port}")

    if (( found == 0 )); then
        echo "  port ${port}: listener is active but its owner PID is unavailable"
    fi
}

service_wait_port_free() {
    local port="${1:-}"
    local timeout="${2:-30}"
    local deadline=$((SECONDS + timeout))

    while service_port_is_listening "${port}"; do
        (( SECONDS < deadline )) || return 1
        sleep 0.2
    done
}

service_http_ok() {
    local url="${1:-}"
    local -a curl_args=(-sf --max-time 2)

    [[ -n "${url}" ]] || return 1
    [[ "${url}" == https://* ]] && curl_args+=(-k)
    curl "${curl_args[@]}" "${url}" >/dev/null 2>&1
}

service_pid_is_active() {
    local pid="${1:-}"
    local state

    kill -0 "${pid}" 2>/dev/null || return 1
    state="$(awk '{print $3}' "/proc/${pid}/stat" 2>/dev/null)" || return 1
    [[ "${state}" != "Z" ]]
}

service_wait_process_exit() {
    local pid="${1:-}"
    local timeout="${2:-30}"
    local deadline=$((SECONDS + timeout))

    while service_pid_is_active "${pid}"; do
        (( SECONDS < deadline )) || return 1
        sleep 0.2
    done
}

service_stop_pid_file() {
    local pid_file="${1:-}"
    local marker="${2:-}"
    local label="${3:-process}"
    local timeout="${4:-30}"
    local pid

    [[ -f "${pid_file}" ]] || return 0
    pid="$(cat "${pid_file}")"

    if ! service_pid_is_owned "${pid}" "${marker}"; then
        echo "${label}: stale PID file (pid ${pid:-unknown}), removing without signaling" >&2
        rm -f "${pid_file}"
        return 0
    fi

    echo "Stopping ${label}: pid ${pid}"
    kill "${pid}" 2>/dev/null || true
    if ! service_wait_process_exit "${pid}" "${timeout}"; then
        echo "${label}: did not exit after ${timeout}s, sending SIGKILL" >&2
        kill -KILL "${pid}" 2>/dev/null || true
        service_wait_process_exit "${pid}" 5 || return 1
    fi

    rm -f "${pid_file}"
}

service_wait_http() {
    local url="${1:-}"
    local label="${2:-service}"
    local pid="${3:-}"
    local log_file="${4:-}"
    local timeout="${5:-60}"
    local deadline=$((SECONDS + timeout))
    while true; do
        if ! service_pid_is_active "${pid}"; then
            echo "${label} exited during startup" >&2
            if [[ -n "${log_file}" && -f "${log_file}" ]]; then
                echo "Last log lines from ${log_file}:" >&2
                tail -n 25 "${log_file}" >&2 || true
            fi
            return 1
        fi

        if service_http_ok "${url}"; then
            echo "${label} ready: ${url}"
            return 0
        fi

        (( SECONDS < deadline )) || break
        sleep 0.5
    done

    echo "${label} did not become ready within ${timeout}s: ${url}" >&2
    if [[ -n "${log_file}" && -f "${log_file}" ]]; then
        echo "Last log lines from ${log_file}:" >&2
        tail -n 25 "${log_file}" >&2 || true
    fi
    return 1
}
