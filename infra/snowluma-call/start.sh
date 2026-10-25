#!/usr/bin/env bash
set -euo pipefail
if [[ ${QQ_CALL_ENABLED:-0} != 1 ]]; then
  exec /root/start.sh
fi
[[ -z ${SNOWLUMA_EXTRA_QQ_HOMES:-} ]] || {
  echo 'QQ calls require one QQ account per container' >&2; exit 1;
}
export QQ_CALL_RUNTIME=${QQ_CALL_RUNTIME:-/app/data/qq-call}
export MAIBOT_QQ_CALL_BRIDGE_DIR=$QQ_CALL_RUNTIME
export MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE=$QQ_CALL_RUNTIME/runtime/control.token
export MAIBOT_QQ_CALL_QQ_DIR=/opt/QQ
export MAIBOT_QQ_CALL_AVSDK_PATH=/opt/QQ/resources/app/avsdk/libAVSDKPlugin.so
export PULSE_SERVER=unix:$QQ_CALL_RUNTIME/runtime/pulse/native
export PYTHONPATH=/opt/qq-call/python
export LD_LIBRARY_PATH=/opt/QQ/resources/app/avsdk:/opt/QQ/resources/app/avsdk/bugly${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
python3 /opt/qq-call/infra/runtime.py
exec /root/start.sh
