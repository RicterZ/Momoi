FROM python:3.12-slim AS model
COPY infra/asr/model.json infra/asr/prepare_model.py /prepare/
RUN python /prepare/prepare_model.py --output /models --archive /tmp/model.tar.bz2

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir sherpa-onnx==1.13.8 numpy==2.4.4 aiohttp==3.13.3 httpx==0.28.1 fastapi==0.135.1 uvicorn==0.41.0 websockets==15.0.1
COPY --from=model /models /models
COPY src/momoi /app/momoi
COPY infra/asr/app.py /app/app.py
COPY LICENSE /app/LICENSE
WORKDIR /app
USER nobody
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "1", "--no-access-log"]
