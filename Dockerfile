FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG NAP_MSG_COMMIT=f0e789d0134375faf5e56de2673139f685a0c7cf

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg nodejs npm tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
        "nap-msg @ https://github.com/RicterZ/Openclaw-NapcatQQ/archive/${NAP_MSG_COMMIT}.zip#subdirectory=nap-msg" \
    && useradd --create-home momoi

USER momoi
EXPOSE 8787
ENTRYPOINT ["momoi"]
CMD ["run"]
