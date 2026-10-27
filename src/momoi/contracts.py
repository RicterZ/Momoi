from typing import Literal, TypedDict


OWNER_PROGRESS_FIELD = "x-momoi-owner-progress"
OWNER_PROGRESS_BEFORE_FIRST_CALL = "before_first_call"


class GoalMutation(TypedDict):
    id: str
    title: str
    success_criteria: str
    authority: str
    source_event_id: str
    status: Literal["active", "waiting", "blocked", "done", "cancelled"]
    plan: list[str]
    next_action: str
    waiting_for: str
    blocked_reason: str
    latest_result: str
    schedule: dict[str, object] | None
    next_review_at: float | None


class ToolResult(TypedDict, total=False):
    image_ids: list[str]
    image_warning: str
    ok: bool
    error: str | None
    message: str
    state: str
    truncated: bool
    provenance: dict[str, str]
    original_chars: int
    content: object
    lines: list[dict[str, object]]
    path: str
    total_lines: int
    sha256: str
    content_offset: int
    next_content_offset: int | None
    result_ref: str
    format: str
    chunk_start: int
    chunk_end: int
    next_cursor: str | None
    has_more: bool
