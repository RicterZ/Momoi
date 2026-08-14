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

ARG NAP_MSG_COMMIT=f0e789d0134375faf5e56de2673139f685a0c7cf

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
    pip wheel --wheel-dir /wheels \
        --requirement /tmp/requirements.txt \
        "nap-msg @ https://github.com/RicterZ/Openclaw-NapcatQQ/archive/${NAP_MSG_COMMIT}.zip#subdirectory=nap-msg"


FROM python-dependencies AS build

COPY README.md ./
COPY src ./src
RUN rm -rf ./src/momoi/dashboard
COPY --from=dashboard-build /build/src/momoi/dashboard ./src/momoi/dashboard
RUN pip wheel --no-deps --wheel-dir /wheels .


FROM python:3.13-slim-trixie AS release

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/momoi

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs npm tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl \
    && rm -rf /wheels

EXPOSE 8787 8788
ENTRYPOINT ["momoi"]
CMD ["run", "--dashboard"]
