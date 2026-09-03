FROM node:24-slim AS dashboard-dependencies

WORKDIR /build
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci


FROM dashboard-dependencies AS dashboard-build

COPY vite.config.js ./
COPY web ./web
RUN npm run build


FROM python:3.13-slim-trixie AS python-dependencies

WORKDIR /build
COPY pyproject.toml ./
RUN python - <<'PY'
import pathlib
import tomllib

project = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
pathlib.Path("/tmp/requirements.txt").write_text(
    "\n".join(project["project"]["dependencies"]) + "\n"
)
PY
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels --requirement /tmp/requirements.txt


FROM python-dependencies AS build

COPY README.md ./
COPY src ./src
RUN mkdir -p ./src/momoi/dashboard/static \
    && find ./src/momoi/dashboard/static -mindepth 1 -delete
COPY --from=dashboard-build /build/src/momoi/dashboard/static ./src/momoi/dashboard/static
RUN pip wheel --no-deps --wheel-dir /wheels .


FROM python:3.13-slim-trixie AS release

ARG VERSION=0.5.4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/momoi

LABEL org.opencontainers.image.title="Momoi" \
      org.opencontainers.image.description="A persistent personal AI companion for private chat." \
      org.opencontainers.image.source="https://github.com/RicterZ/Momoi" \
      org.opencontainers.image.version="${VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl \
    && rm -rf /wheels

COPY config.example /usr/share/momoi/example
COPY docker/entrypoint.sh /usr/local/bin/momoi-entrypoint
RUN chmod +x /usr/local/bin/momoi-entrypoint

EXPOSE 8787 8788
ENTRYPOINT ["momoi-entrypoint"]
CMD ["run", "--dashboard"]
