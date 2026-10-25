ARG SNOWLUMA_IMAGE=motricseven7/snowluma:v2.2.0
FROM ${SNOWLUMA_IMAGE}
USER root
ARG BRIDGE_REV=22f30c021cd3170f75af9dff66cc959a07ebda4b
ARG BRIDGE_SHA256=d659c46e2d38bba14a39694c31261bf2b54eee206da78690b5f5508f8a936fd5
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio pulseaudio-utils python3 python3-aiohttp ffmpeg curl ca-certificates \
    libpulse-mainloop-glib0 libegl1 libopengl0 gcc libc6-dev && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/qq-call/upstream \
    && curl -fsSL "https://codeload.github.com/RicterZ/maibot-qq-voice-call/tar.gz/${BRIDGE_REV}" -o /tmp/bridge.tgz \
    && echo "${BRIDGE_SHA256}  /tmp/bridge.tgz" | sha256sum -c - \
    && tar xzf /tmp/bridge.tgz --strip-components=1 -C /opt/qq-call/upstream \
        maibot-qq-voice-call-${BRIDGE_REV}/LICENSE \
        maibot-qq-voice-call-${BRIDGE_REV}/THIRD_PARTY_NOTICES.md \
        maibot-qq-voice-call-${BRIDGE_REV}/bridge/av-host \
        maibot-qq-voice-call-${BRIDGE_REV}/bridge/napcat-plugin \
        maibot-qq-voice-call-${BRIDGE_REV}/bridge/scripts/audio-control.sh \
        maibot-qq-voice-call-${BRIDGE_REV}/bridge/scripts/common.sh \
    && rm /tmp/bridge.tgz \
    && test -f /opt/QQ/resources/app/avsdk/libAVSDKPlugin.so
COPY infra/qq-call/ /opt/qq-call/qq-call/
COPY infra/snowluma-call/ /opt/qq-call/infra/
COPY src/momoi/channel/onebot/voice_call/ /opt/qq-call/python/momoi/channel/onebot/voice_call/
RUN gcc -shared -fPIC -O2 -fno-stack-protector -D_GNU_SOURCE -I/usr/local/include/node \
        -I/opt/qq-call/infra/native \
        -o /opt/qq-call/infra/session_hook.node \
        /opt/qq-call/infra/native/session_hook.c \
    && python3 /opt/qq-call/infra/prepare.py \
    && bash -n /opt/qq-call/infra/start.sh /opt/qq-call/infra/service.sh
ENV QQ_CALL_ENABLED=0 QQ_CALL_PORT=6112
LABEL org.opencontainers.image.title="Momoi SnowLuma voice-call bridge" \
      io.momoi.qq-call.bridge-revision="${BRIDGE_REV}"
CMD ["bash", "/opt/qq-call/infra/start.sh"]
