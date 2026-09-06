import asyncio
from enum import StrEnum

import aiohttp
import httpx


class ErrorCategory(StrEnum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    INVALID_RESPONSE = "invalid_response"
    REQUEST = "request"


class IntegrationError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        category: ErrorCategory = ErrorCategory.REQUEST,
        service: str = "",
        operation: str = "",
        retryable: bool = False,
    ):
        super().__init__(detail)
        self.category = category
        self.service = service
        self.operation = operation
        self.retryable = retryable


def error_category(error: BaseException) -> ErrorCategory:
    if isinstance(error, IntegrationError):
        return error.category
    if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
        return ErrorCategory.TIMEOUT
    if isinstance(error, (aiohttp.ClientConnectionError, httpx.NetworkError, OSError)):
        return ErrorCategory.CONNECTION
    return ErrorCategory.REQUEST


def http_category(status: int) -> ErrorCategory:
    if status in {401, 403}:
        return ErrorCategory.AUTHENTICATION
    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if status >= 500:
        return ErrorCategory.SERVER
    return ErrorCategory.REQUEST
