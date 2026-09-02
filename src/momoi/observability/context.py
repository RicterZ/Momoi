import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("momoi_log_context", default={})


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_log_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    merged = current_log_context()
    merged.update({key: value for key, value in fields.items() if value is not None})
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


@contextmanager
def captured_log_context(snapshot: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    token = _CONTEXT.set(dict(snapshot))
    try:
        yield current_log_context()
    finally:
        _CONTEXT.reset(token)
