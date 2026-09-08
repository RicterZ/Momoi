"""Test unsaved provider settings without touching the application runtime."""

import asyncio
import copy
from pathlib import Path
from time import monotonic

import aiohttp
import httpx

from ..config.models import ConfigError
from ..llm.errors import ProviderResponseError
from .configuration import ProviderBinding, ProviderCatalog, CAPABILITIES
from .errors import ErrorCategory, error_category, http_category
from .fields import normalize_fields
from .registry import adapter_definition, ServiceRegistry

TEST_TIMEOUT_SECONDS = 30


def failure(capability, adapter, code, message, elapsed_ms=0, **details):
    return {
        "ok": False,
        "capability": capability,
        "adapter": adapter,
        "elapsed_ms": elapsed_ms,
        "error": {"code": code, "message": message, **details},
    }


async def test_connection(capability, document):
    """Return (HTTP status, result); config errors are distinct from remote failures."""
    started = monotonic()
    name = document.get("adapter") if isinstance(document, dict) else None
    if capability not in CAPABILITIES or not isinstance(name, str) or not name:
        return 400, failure(capability, None, "validation", "需要有效的 capability 和 adapter。")
    if set(document) - {"adapter", "options", "enabled"} or (
        "enabled" in document and type(document["enabled"]) is not bool
    ):
        return 400, failure(capability, name, "validation", "测试参数只接受 adapter、options 和可选的 enabled。")
    try:
        adapter = adapter_definition(name, capability)
    except ValueError:
        return 400, failure(capability, name, "validation", "该 adapter 不支持指定能力。")
    if adapter.test is None:
        return 400, failure(capability, name, "unsupported", "该 adapter 暂不支持连接测试。")
    try:
        options = normalize_fields(adapter.schema, copy.deepcopy(document.get("options", {})))
        adapter.validate(options)
    except (ValueError, TypeError, KeyError) as error:
        message = str(error) if isinstance(error, ConfigError) else "配置参数校验失败。"
        return 400, failure(capability, name, "validation", message)
    catalog = ProviderCatalog(Path("providers.yaml"), {
        capability: ProviderBinding("probe", name, True, options),
    })
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            services = ServiceRegistry(catalog)
            provider = services.get(capability)
            async with services:
                details = await adapter.test(provider)
        return 200, {
            "ok": True, "capability": capability, "adapter": name,
            "elapsed_ms": round((monotonic() - started) * 1000),
            "details": details,
        }
    except Exception as error:
        status = getattr(error, "_http_status", None)
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
        elif isinstance(error, aiohttp.ClientResponseError):
            status = error.status
        category = http_category(status) if status else error_category(error)
        if isinstance(error, ProviderResponseError) or isinstance(error, ValueError):
            category = ErrorCategory.INVALID_RESPONSE
        messages = {
            ErrorCategory.AUTHENTICATION: "认证失败，请检查密钥及权限。",
            ErrorCategory.CONNECTION: "连接失败，请检查接口地址、DNS、网络及 TLS 配置。",
            ErrorCategory.TIMEOUT: "请求超时，请检查服务状态和超时配置。",
            ErrorCategory.RATE_LIMIT: "请求受限，请检查额度或稍后重试。",
            ErrorCategory.SERVER: "服务端异常，请稍后重试。",
            ErrorCategory.INVALID_RESPONSE: "接口返回无效数据，请检查协议、模型及向量维度。",
            ErrorCategory.REQUEST: "请求失败，请检查接口地址、模型名称及参数。",
        }
        return 200, failure(
            capability, name, str(category), messages[category],
            round((monotonic() - started) * 1000),
            **({"http_status": status} if status else {}),
        )
