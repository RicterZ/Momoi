import logging
import math
import time

import httpx
import numpy as np

from ..config.models import EmbeddingConfig
from ..logging_context import log_event
from ..policies import SemanticPolicy

logger = logging.getLogger(__name__)


def semantic_error_category(error: BaseException) -> str:
    """Classify embedding failures without changing the fallback payload."""
    return (
        "timeout"
        if isinstance(error, (TimeoutError, httpx.TimeoutException))
        else "error"
    )


class EmbeddingClient:
    def __init__(
        self,
        config: EmbeddingConfig,
        policy: SemanticPolicy = SemanticPolicy(),
    ) -> None:
        self.config = config
        self.policy = policy
        self._client = httpx.AsyncClient()
        self._query_failures = 0
        self._query_breaker_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def encode(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        now = time.monotonic()
        if query and now < self._query_breaker_until:
            raise RuntimeError("query circuit breaker is open")
        headers = (
            {"Authorization": f"Bearer {self.config.api_key}"}
            if self.config.api_key
            else {}
        )
        timeout = (
            self.config.query_timeout_seconds
            if query
            else self.config.document_timeout_seconds
        )
        try:
            response = await self._client.post(
                self.config.endpoint,
                json={"model": self.config.model, "input": texts},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("embedding response count mismatch")
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            vectors = [item.get("embedding") for item in ordered]
            if any(not isinstance(vector, list) for vector in vectors):
                raise ValueError("embedding response has invalid vectors")
            normalized: list[list[float]] = []
            for raw in vectors:
                array = np.asarray(raw, dtype=np.float32)
                if array.ndim != 1 or array.size != self.config.dimensions:
                    raise ValueError("embedding dimension mismatch")
                if not np.isfinite(array).all():
                    raise ValueError("embedding contains non-finite values")
                norm = float(np.linalg.norm(array))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("embedding has invalid norm")
                normalized.append((array / norm).tolist())
            if query:
                self._query_failures = 0
            return normalized
        except Exception:
            if query:
                self._query_failures += 1
                if self._query_failures >= self.policy.query_failure_limit:
                    self._query_breaker_until = (
                        time.monotonic() + self.policy.query_breaker_seconds
                    )
                    log_event(
                        logger,
                        logging.WARNING,
                        "semantic_query_breaker_opened",
                        failures=self._query_failures,
                        cooldown_seconds=self.policy.query_breaker_seconds,
                    )
            raise

    async def health(self) -> tuple[bool, float, str]:
        started = time.monotonic()
        try:
            url = self.config.endpoint.rsplit("/v1/embeddings", 1)[0] + "/healthz"
            response = await self._client.get(
                url, timeout=self.config.query_timeout_seconds
            )
            response.raise_for_status()
            return True, (time.monotonic() - started) * 1000, ""
        except Exception as error:
            return False, (time.monotonic() - started) * 1000, type(error).__name__
