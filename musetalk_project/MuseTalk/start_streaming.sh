#!/usr/bin/env bash
set -euo pipefail

# Avoid being stopped again if the server is moved to the background while
# its stdout still points at a terminal.
trap '' TTOU TTIN

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Use the project venv even when the caller has not sourced activate.sh.
if [ -z "${VIRTUAL_ENV:-}" ]; then
  source "$SCRIPT_DIR/../activate.sh"
  cd "$SCRIPT_DIR"
fi

CONFIG="${STREAMING_WEB_CONFIG:-configs/streaming_web.yaml}"
# The server exits after live.idle_timeout_seconds without new audio or
# playback activity, releasing this port.
exec python3 stream_server.py --config "$CONFIG"
