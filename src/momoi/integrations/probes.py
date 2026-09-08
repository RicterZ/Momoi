"""Real requests used by builtin adapters' connection tests."""

from dataclasses import replace
import math

from .errors import ErrorCategory, IntegrationError


def invalid(message):
    raise IntegrationError(message, category=ErrorCategory.INVALID_RESPONSE)


async def model_probe(provider):
    # Probe instances are disposable; production options and clients are untouched.
    provider.config = replace(provider.config, max_retries=0)
    result = await provider.complete(
        "This is a connection test. Reply with OK only.",
        [{"role": "user", "content": "Reply OK."}],
    )
    if not any(
        item.get("type") == "text" and str(item.get("text", "")).strip()
        for item in result.content
    ):
        invalid("模型未返回有效文本，请检查模型名称及生成参数。")
    return {"model": provider.config.model}


async def embedding_probe(provider):
    # /healthz is not portable and cannot verify credentials/model/dimensions.
    for query in (True, False):
        vectors = await provider.encode(["Momoi connection test"], query=query)
        if (
            len(vectors) != 1
            or len(vectors[0]) != provider.space.dimensions
            or not all(math.isfinite(value) for value in vectors[0])
            or not any(vectors[0])
        ):
            invalid("向量响应无效或维度与配置不一致。")
    return {"model": provider.space.model, "dimensions": provider.space.dimensions}

