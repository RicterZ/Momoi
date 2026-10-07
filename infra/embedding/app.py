from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from pydantic import BaseModel, ConfigDict

MODEL = "BAAI/bge-small-zh-v1.5"
DIMENSIONS = 512
logger = logging.getLogger("momoi.embedding")
app = FastAPI(title="Momoi embedding sidecar", docs_url=None, redoc_url=None)
embedding = TextEmbedding(
    model_name=MODEL,
    cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/models"),
    threads=None,
)


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    input: str | list[str]


@app.get("/healthz")
def healthz() -> dict[str, object]:
    return {"ok": True, "model": MODEL, "dimensions": DIMENSIONS}


@app.post("/v1/embeddings")
def embeddings(request: EmbeddingRequest) -> dict[str, object]:
    if request.model != MODEL:
        raise HTTPException(status_code=400, detail="unsupported model")
    values = [request.input] if isinstance(request.input, str) else request.input
    if (
        not values
        or len(values) > 64
        or any(not value or len(value) > 200_000 for value in values)
    ):
        raise HTTPException(status_code=400, detail="invalid input batch")
    started = time.monotonic()
    try:
        vectors = [
            vector.tolist()
            for vector in embedding.embed(values, batch_size=len(values))
        ]
    except Exception as error:
        logger.error(
            "embedding_failed batch_size=%d error_type=%s",
            len(values),
            type(error).__name__,
        )
        raise HTTPException(status_code=500, detail="embedding failed") from None
    if any(len(vector) != DIMENSIONS for vector in vectors):
        logger.error("embedding_dimension_mismatch batch_size=%d", len(values))
        raise HTTPException(status_code=500, detail="embedding dimension mismatch")
    logger.info(
        "embedding_completed batch_size=%d duration_ms=%d",
        len(values),
        int((time.monotonic() - started) * 1000),
    )
    return {
        "object": "list",
        "model": MODEL,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }
