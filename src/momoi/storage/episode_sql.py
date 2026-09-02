from .turn_workflow import turn_workflow_kind_sql


def runtime_archive_kind_sql(episode: str) -> str:
    """Resolve explicit archive ownership, with one legacy-read fallback."""

    legacy_workflow = turn_workflow_kind_sql("legacy_archive_source")
    return f"""CASE
        WHEN COALESCE({episode}.archive_kind, '')<>'' THEN {episode}.archive_kind
        WHEN EXISTS (
            SELECT 1 FROM episode_turns AS legacy_archive_turn
            JOIN turns AS legacy_archive_source
              ON legacy_archive_source.id=legacy_archive_turn.turn_id
            WHERE legacy_archive_turn.episode_id={episode}.id
              AND {legacy_workflow}='webhook'
        ) THEN 'webhook'
        WHEN EXISTS (
            SELECT 1 FROM episode_turns AS legacy_archive_turn
            JOIN turns AS legacy_archive_source
              ON legacy_archive_source.id=legacy_archive_turn.turn_id
            WHERE legacy_archive_turn.episode_id={episode}.id
              AND {legacy_workflow}='heartbeat'
        ) THEN 'heartbeat'
        WHEN EXISTS (
            SELECT 1 FROM episode_turns AS legacy_archive_turn
            JOIN turns AS legacy_archive_source
              ON legacy_archive_source.id=legacy_archive_turn.turn_id
            WHERE legacy_archive_turn.episode_id={episode}.id
              AND {legacy_workflow}='goal'
        ) THEN 'goal'
    END"""
