#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export STREAMING_WEB_CONFIG=configs/streaming_web_feature_legacy.yaml
exec bash start_streaming.sh
