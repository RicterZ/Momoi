"""Offline BGE encoder using the container's model and preprocessing."""

from pathlib import Path

MODEL = "BAAI/bge-small-zh-v1.5"
DIMENSIONS = 512
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"


def load_encoder(model_path: Path):
    from fastembed import TextEmbedding

    required = ("model_optimized.onnx", "tokenizer.json", "config.json", "tokenizer_config.json", "special_tokens_map.json")
    missing = [name for name in required if not (model_path / name).is_file()]
    if missing:
        raise FileNotFoundError(f"BGE model is incomplete: {', '.join(missing)}")
    encoder = TextEmbedding(
        model_name=MODEL,
        specific_model_path=str(model_path),
        local_files_only=True,
        providers=["CPUExecutionProvider"],
    )
    vector = next(encoder.embed(["桃井的离线编码检查"]))
    import numpy as np

    if len(vector) != DIMENSIONS or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
        raise ValueError("BGE offline smoke check failed")
    return encoder


def create_app(encoder):
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, ConfigDict

    class EmbeddingRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        model: str
        input: str | list[str]

    app = FastAPI(title="Momoi local embedding", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "model": MODEL, "dimensions": DIMENSIONS}

    @app.post("/v1/embeddings")
    def embeddings(request: EmbeddingRequest):
        if request.model != MODEL:
            raise HTTPException(400, "unsupported model")
        values = [request.input] if isinstance(request.input, str) else request.input
        if not values or len(values) > 64 or any(not value or len(value) > 200_000 for value in values):
            raise HTTPException(400, "invalid input batch")
        try:
            vectors = [vector.tolist() for vector in encoder.embed(values, batch_size=len(values))]
        except Exception:
            import logging
            logging.getLogger(__name__).exception("embedding failed")
            raise HTTPException(500, "embedding failed") from None
        if len(vectors) != len(values) or any(len(vector) != DIMENSIONS for vector in vectors):
            raise HTTPException(500, "embedding dimension mismatch")
        return {
            "object": "list", "model": MODEL,
            "data": [{"object": "embedding", "index": i, "embedding": vector} for i, vector in enumerate(vectors)],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    return app
