"""Validated normalized float32 vectors for storage and cosine search."""
import math
from typing import Iterable

import numpy as np


def encode_vector(vector: Iterable[float], dimensions: int) -> bytes:
    array = np.asarray(tuple(vector), dtype="<f4")
    if array.ndim != 1 or array.size != dimensions:
        raise ValueError("embedding dimension mismatch")
    if not np.isfinite(array).all():
        raise ValueError("embedding contains non-finite values")
    norm = float(np.linalg.norm(array))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding has zero or invalid norm")
    return (array / norm).astype("<f4", copy=False).tobytes()

def decode_vector(blob: object, dimensions: int) -> np.ndarray:
    if not isinstance(blob, bytes) or len(blob) != dimensions * 4:
        raise ValueError("invalid embedding byte length")
    vector = np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)
    if not np.isfinite(vector).all():
        raise ValueError("embedding contains non-finite values")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding has zero or invalid norm")
    if abs(norm - 1.0) > 1e-3:
        vector /= norm
    return vector
