ARG SNOWLUMA_BASE=ricterz/snowluma:v2.2.0
FROM ${SNOWLUMA_BASE}
COPY infra/snowluma/patch_runtime.py /tmp/patch_runtime.py
RUN python3 /tmp/patch_runtime.py /app/runtime/index.mjs && rm /tmp/patch_runtime.py
LABEL org.opencontainers.image.title="SnowLuma v2.2.0 with Momoi fixes" \
      org.opencontainers.image.base.name="ricterz/snowluma:v2.2.0"
