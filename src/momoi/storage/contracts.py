"""Storage boundary records. JSON-compatible types preserve the public wire format.

TypedDict describes validated records, not untrusted model input. Runtime tool
validation remains responsible for rejecting malformed commands.
"""
from typing import Any, Literal, NotRequired, TypedDict


class PromptFingerprint(TypedDict):
    hash: str
    tokens_est: int


class RequestShape(TypedDict):
    settings_hash: str
    settings_fields: NotRequired[dict[str, str]]
    parts: list[PromptFingerprint]
    tool_parts: NotRequired[list[PromptFingerprint]]
    message_count: int
    input_tokens_est: int


class RequestMetricRecord(TypedDict):
    created_at: float
    request_id: str
    attempt: int
    model: str
    protocol: NotRequired[str]
    route: str
    shape: RequestShape
    status: Literal["success", "error", "cancelled"]
    usage: dict[str, float | int | bool] | None
    first_response_ms: float | None
    first_tool_ms: NotRequired[float]
    first_tool_name: NotRequired[str]
    first_tool_at: NotRequired[float]
    duration_ms: float
    http_status: NotRequired[int | None]
    error_type: NotRequired[str | None]
    turn_id: NotRequired[str]
    stage: NotRequired[str]
    call_id: NotRequired[str]
    round: NotRequired[int]


class MetricsPage(TypedDict):
    cost_available: bool
    totals: dict[str, Any]
    stages: list[dict[str, Any]]
    trend: list[dict[str, Any]]
    items: list[dict[str, Any]]
    next_cursor: int | None
    filters: dict[str, list[str]]
    retention_days: int
    timing: str
