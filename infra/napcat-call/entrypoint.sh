#!/usr/bin/env bash
set -euo pipefail
export PULSE_SERVER="unix:${QQ_CALL_RUNTIME:-/app/qq-call}/runtime/pulse/native"
exec python3 /opt/qq-call/infra/supervisor.py
