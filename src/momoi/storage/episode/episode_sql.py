from ..core.turn_workflow import turn_workflow_kind_sql


def topic_turn_sql(turn: str) -> str:
    """Turns archived into topic Episodes: owner and outward-facing autonomous turns."""

    return (
        f"({turn}.kind='owner' OR {turn}.workflow_kind IN "
        "('heartbeat', 'webhook', 'subagent_report', 'goal'))"
    )


def runtime_archive_kind_sql(episode: str) -> str:
    """Resolve explicit archive ownership, with one legacy-read fallback.

    An Episode the owner took part in is a conversation even when it started
    from a Goal or webhook notification, so the fallback never applies to it.
    """

    legacy_workflow = turn_workflow_kind_sql("legacy_archive_source")
    # Preserve webhook > heartbeat > goal precedence in one legacy Turn scan.
    return f"""COALESCE(NULLIF({episode}.archive_kind, ''), (
            SELECT CASE MIN(CASE {legacy_workflow}
                WHEN 'webhook' THEN 1 WHEN 'heartbeat' THEN 2 WHEN 'goal' THEN 3
                WHEN 'subagent_report' THEN 4
            END)
                WHEN 1 THEN 'webhook' WHEN 2 THEN 'heartbeat' WHEN 3 THEN 'goal'
                WHEN 4 THEN 'subagent_report'
            END
            FROM episode_turns AS legacy_archive_turn
            JOIN turns AS legacy_archive_source
              ON legacy_archive_source.id=legacy_archive_turn.turn_id
            WHERE legacy_archive_turn.episode_id={episode}.id
              AND NOT EXISTS (
                  SELECT 1 FROM episode_turns AS legacy_owner_turn
                  JOIN turns AS legacy_owner_source
                    ON legacy_owner_source.id=legacy_owner_turn.turn_id
                  WHERE legacy_owner_turn.episode_id={episode}.id
                    AND legacy_owner_source.kind='owner'
              )
        ))"""
