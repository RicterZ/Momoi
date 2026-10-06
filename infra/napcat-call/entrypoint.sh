#!/usr/bin/env bash
set -euo pipefail
export PULSE_SERVER="unix:${QQ_CALL_RUNTIME:-/app/qq-call}/runtime/pulse/native"
exec bash /opt/qq-call/entrypoint-upstream.sh
