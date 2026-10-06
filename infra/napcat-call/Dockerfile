# QQ and AVSDK remain supplied by the upstream NapCat image.
ARG NAPCAT_IMAGE=mlikiowa/napcat-docker@sha256:1336a777f9a4f1f8cb89fef42f7548deacd3645919a067a50df5b66b5e77390e
FROM ${NAPCAT_IMAGE}
USER root
ARG BRIDGE_REV=22f30c021cd3170f75af9dff66cc959a07ebda4b
ARG BRIDGE_SHA256=d659c46e2d38bba14a39694c31261bf2b54eee206da78690b5f5508f8a936fd5
RUN apt-get update && apt-get -f install -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    pulseaudio pulseaudio-utils libpulse-mainloop-glib0 libegl1 libopengl0 python3 python3-aiohttp ca-certificates curl \
    ffmpeg xvfb unzip tini && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/qq-call/upstream /app/napcat/plugins \
    && curl -fsSL "https://codeload.github.com/RicterZ/maibot-qq-voice-call/tar.gz/${BRIDGE_REV}" -o /tmp/bridge.tgz \
    && echo "${BRIDGE_SHA256}  /tmp/bridge.tgz" | sha256sum -c - \
    && tar -xzf /tmp/bridge.tgz --strip-components=1 -C /opt/qq-call/upstream \
    && rm /tmp/bridge.tgz \
    && test -f /opt/QQ/resources/app/avsdk/libAVSDKPlugin.so \
    && test -f /opt/QQ/resources/app/loadNapCat.js
COPY infra/napcat-call/ /opt/qq-call/infra/
COPY src/momoi/qq_call/ /opt/qq-call/python/momoi/qq_call/
RUN python3 /opt/qq-call/infra/prepare.py \
    && bash -n /opt/qq-call/infra/entrypoint.sh /opt/qq-call/infra/bootstrap.sh
ENV QQ_CALL_ENABLED=0 QQ_CALL_PORT=6112 \
    QQ_CALL_RUNTIME=/app/qq-call \
    PYTHONPATH=/opt/qq-call/python
LABEL org.opencontainers.image.title="Momoi NapCat voice-call bridge" \
      org.opencontainers.image.source="https://github.com/RicterZ/Momoi" \
      io.momoi.qq-call.bridge-revision="${BRIDGE_REV}"
WORKDIR /app
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s \
    CMD python3 /opt/qq-call/infra/health.py
ENTRYPOINT ["/usr/bin/tini", "--", "bash", "/opt/qq-call/infra/entrypoint.sh"]
