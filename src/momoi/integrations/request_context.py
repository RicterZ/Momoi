"""Request-scoped model options, independent of providers and runtime stages."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_THINKING_EFFORT: ContextVar[str | None] = ContextVar("thinking_effort", default=None)


@contextmanager
def model_request(*, thinking_effort: str | None = None) -> Iterator[None]:
    """Scope an effort override to one request, including tasks spawned within it."""
    token = _THINKING_EFFORT.set(thinking_effort or None)
    try:
        yield
    finally:
        _THINKING_EFFORT.reset(token)


def requested_thinking_effort(default: str = "") -> str:
    """Providers encode this resolved value into their own wire format."""
    return _THINKING_EFFORT.get() or default
