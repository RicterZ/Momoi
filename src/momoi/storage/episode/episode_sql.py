from ..core.turn_workflow import turn_workflow_kind_sql


def topic_turn_sql(turn: str) -> str:
    """Turns archived into topic Episodes: owner turns and heartbeats."""

    return f"({turn}.kind='owner' OR {turn}.workflow_kind='heartbeat')"


def runtime_archive_kind_sql(episode: str) -> str:
    """Resolve explicit archive ownership, with one legacy-read fallback."""

    legacy_workflow = turn_workflow_kind_sql("legacy_archive_source")
    # Preserve webhook > heartbeat > goal precedence in one legacy Turn scan.
    return f"""COALESCE(NULLIF({episode}.archive_kind, ''), (
            SELECT CASE MIN(CASE {legacy_workflow}
                WHEN 'webhook' THEN 1 WHEN 'heartbeat' THEN 2 WHEN 'goal' THEN 3
            END)
                WHEN 1 THEN 'webhook' WHEN 2 THEN 'heartbeat' WHEN 3 THEN 'goal'
            END
            FROM episode_turns AS legacy_archive_turn
            JOIN turns AS legacy_archive_source
              ON legacy_archive_source.id=legacy_archive_turn.turn_id
            WHERE legacy_archive_turn.episode_id={episode}.id
        ))"""
