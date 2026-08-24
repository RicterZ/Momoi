from typing import Literal, NotRequired, TypedDict


class EpisodeBinding(TypedDict):
    action: str
    unit_ids: list[str]
    episode_ref: NotRequired[str]
    episode_id: NotRequired[str]
    relation: NotRequired[str]
    title: NotRequired[str]
    topics: NotRequired[list[str]]
    entities: NotRequired[list[str]]
    open_loops: NotRequired[list[str]]
    salience: NotRequired[float]


class ContextPlan(TypedDict):
    version: int
    intent_units: list[dict[str, object]]
    episode_actions: list[EpisodeBinding]
    episode_links: list[dict[str, object]]
    owner_handoff: NotRequired[dict[str, object]]
    uncertainty: list[str]


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
    ok: bool
    error: str | None
    message: str
    state: str
    truncated: bool
    provenance: dict[str, str]
    original_chars: int
    content: object
