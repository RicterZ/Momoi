#!/usr/bin/env bash
set -euo pipefail
[[ ${QQ_CALL_ENABLED:-0} == 1 ]] || exit 0
python3 /opt/qq-call/infra/allowlist.py
runtime=${QQ_CALL_RUNTIME:-/app/qq-call}
mkdir -p "$runtime"
export MAIBOT_QQ_CALL_BRIDGE_DIR="$runtime"
export MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE="${QQ_CALL_TOKEN_FILE:-$runtime/runtime/control.token}"
# Installer preserves an existing token and backs up the loader once.
bash /opt/qq-call/upstream/bridge/scripts/install.sh \
  --qq-dir /opt/QQ --napcat-dir /app/napcat --install-dir "$runtime"
chown -R "${NAPCAT_UID:-0}:${NAPCAT_GID:-0}" "$runtime"
export PULSE_SERVER="unix:$runtime/runtime/pulse/native"
export MAIBOT_QQ_CALL_QQ_DIR=/opt/QQ
export MAIBOT_QQ_CALL_AVSDK_PATH=/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so
# Container PID namespaces change across recreation; never reuse PulseAudio PID/socket files.
if ! gosu napcat pactl info >/dev/null 2>&1; then
  rm -f "$runtime/runtime/pulse/pid" "$runtime/runtime/pulse/native"
fi
gosu napcat "$runtime/scripts/audio-control.sh" start
# The AV host shares the existing DISPLAY started by the upstream entrypoint.
gosu napcat "$runtime/scripts/run-av-host.sh" >"$runtime/logs/av-host.log" 2>&1 &
gosu napcat python3 -m momoi.qq_call.broker >"$runtime/logs/broker.log" 2>&1 &
