"""One offline BGE encoder per model directory, shared across runtime reloads."""

import asyncio
from functools import lru_cache
import os
from pathlib import Path
import threading
import time

import numpy as np

from ..models import EmbeddingSpaceConfig

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
    return TextEmbedding(
        model_name=MODEL,
        specific_model_path=str(model_path),
        local_files_only=True,
        providers=["CPUExecutionProvider"],
    )


class LocalEmbedding:
    def __init__(self, model_path: Path):
        self.space = EmbeddingSpaceConfig(enabled=True, model=MODEL, dimensions=DIMENSIONS)
        self.model_path = model_path
        self._encoder = None
        self._lock = threading.Lock()

    def _encode(self, texts):
        # A cancelled async caller does not stop ONNX; serialize in the worker.
        with self._lock:
            if self._encoder is None:
                self._encoder = load_encoder(self.model_path)
            vectors = list(self._encoder.embed(texts, batch_size=len(texts)))
        if len(vectors) != len(texts):
            raise ValueError("embedding response count mismatch")
        normalized = []
        for vector in vectors:
            vector = np.asarray(vector, dtype=np.float32)
            if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all():
                raise ValueError("invalid embedding vector")
            norm = float(np.linalg.norm(vector))
            if not np.isfinite(norm) or norm <= 0:
                raise ValueError("invalid embedding norm")
            normalized.append((vector / norm).tolist())
        return normalized

    async def encode(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > 64 or any(not isinstance(text, str) or not text or len(text) > 200_000 for text in texts):
            raise ValueError("invalid embedding input batch")
        return await asyncio.to_thread(self._encode, list(texts))

    async def health(self) -> tuple[bool, float, str]:
        started = time.monotonic()
        try:
            await self.encode(["编码检查"], query=True)
            return True, (time.monotonic() - started) * 1000, ""
        except Exception as error:
            return False, (time.monotonic() - started) * 1000, type(error).__name__

    async def close(self) -> None:
        # The process owns the model; retiring a runtime generation must not unload it.
        pass


@lru_cache(maxsize=1)
def _local_embedding(model_path: Path) -> LocalEmbedding:
    return LocalEmbedding(model_path)


def local_embedding(model_path: str = "") -> LocalEmbedding:
    path = model_path or os.environ.get("MOMOI_EMBEDDING_MODEL_PATH")
    return _local_embedding(Path(path).expanduser().resolve() if path else
                            Path("models/bge-small-zh-v1.5"))
