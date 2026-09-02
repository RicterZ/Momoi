from __future__ import annotations

from typing import Literal, cast


TurnWorkflowKind = Literal[
    "owner",
    "webhook",
    "goal",
    "heartbeat",
    "reply_followup",
    "reflection",
    "memory_maintenance",
    "episode_consolidate",
    "episode_anneal",
]

TURN_WORKFLOW_KINDS = frozenset(
    {
        "owner",
        "webhook",
        "goal",
        "heartbeat",
        "reply_followup",
        "reflection",
        "memory_maintenance",
        "episode_consolidate",
        "episode_anneal",
    }
)


def require_turn_workflow_kind(value: str) -> TurnWorkflowKind:
    if value not in TURN_WORKFLOW_KINDS:
        raise ValueError(f"unknown Turn workflow kind: {value}")
    return cast(TurnWorkflowKind, value)


def turn_workflow_kind_sql(table: str = "turns") -> str:
    """Return the authoritative workflow, with one legacy-row adapter."""

    return f"""CASE
        WHEN {table}.workflow_kind IS NOT NULL THEN {table}.workflow_kind
        WHEN {table}.kind='owner' THEN 'owner'
        WHEN {table}.id GLOB 'webhook:*' THEN 'webhook'
        WHEN {table}.stage GLOB 'memory_maintenance_*' THEN 'memory_maintenance'
        WHEN EXISTS (
            SELECT 1 FROM messages AS workflow_message
            WHERE workflow_message.turn_id={table}.id
              AND workflow_message.content
                  LIKE '[AUTONOMOUS HEARTBEAT RECORD;%'
        ) THEN 'heartbeat'
        WHEN EXISTS (
            SELECT 1 FROM json_each({table}.source_ids_json) AS source
            WHERE source.value GLOB 'goal:*'
        ) THEN 'goal'
        WHEN EXISTS (
            SELECT 1 FROM json_each({table}.source_ids_json) AS source
            WHERE source.value GLOB 'reply-followup:*'
        ) THEN 'reply_followup'
        WHEN EXISTS (
            SELECT 1 FROM json_each({table}.source_ids_json) AS source
            WHERE source.value GLOB 'heartbeat:*'
        ) THEN 'heartbeat'
        WHEN EXISTS (
            SELECT 1 FROM json_each({table}.source_ids_json) AS source
            WHERE source.value GLOB 'reflection:*'
        ) THEN 'reflection'
        WHEN EXISTS (
            SELECT 1 FROM json_each({table}.source_ids_json) AS source
            WHERE source.value GLOB 'episode-consolidate:*'
        ) THEN 'episode_consolidate'
        WHEN EXISTS (
            SELECT 1 FROM json_each({table}.source_ids_json) AS source
            WHERE source.value GLOB 'episode-anneal:*'
        ) THEN 'episode_anneal'
    END"""
