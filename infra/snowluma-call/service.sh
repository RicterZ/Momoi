#!/usr/bin/env bash
set -euo pipefail
runtime=${QQ_CALL_RUNTIME:?}
case ${1:?} in
  audio)
    exec "$runtime/scripts/audio-control.sh" start ;;
  av-host)
    for _ in {1..40}; do
      pactl info >/dev/null 2>&1 && break
      sleep 0.25
    done
    pactl info >/dev/null
    export MAIBOT_QQ_CALL_AV_HOST=1
    exec /opt/QQ/qq --no-sandbox --user-data-dir="$runtime/runtime/av-host-profile" ;;
  broker) exec python3 -m momoi.channel.onebot.voice_call.broker ;;
  *) exit 2 ;;
esac
