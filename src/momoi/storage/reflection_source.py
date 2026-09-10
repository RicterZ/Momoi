from .memory_values import truncate_tokens
from .reflection_values import (
    _reflection_compact_value,
    _reflection_json,
    reflection_window,
)
from .transcripts import _MESSAGE_TIME_SQL


class ReflectionSourceStore:
    def reflection_source(
        self, local_date: str, *, at: str = "03:00", end_at: float | None = None,
    ) -> dict[str, object]:
        start, end = reflection_window(local_date, at, self._timezone, end_at=end_at)
        mood_entries: list[str] = []
        for row in self._db.execute(
            """SELECT turn_id, created_at, payload_json FROM turn_journal
               WHERE created_at>=? AND created_at<? AND item_type='final'
               ORDER BY created_at, sequence""",
            (start, end),
        ).fetchall():
            payload = _reflection_json(
                row["payload_json"], {}, record_id=row["turn_id"], field="payload_json",
            )
            mood = payload.get("mood_change") if isinstance(payload, dict) else None
            if isinstance(mood, dict) and mood.get("state"):
                mood_entries.append(
                    f"{self.context_timestamp(row['created_at'])} state={mood.get('state')} "
                    f"intensity={mood.get('intensity', 'unknown')} "
                    f"cause={_reflection_compact_value(mood.get('cause'), 180)}"
                )

        episode_entries: list[str] = []
        episode_rows = self._db.execute(
            f"""SELECT id, title, status, narrative_summary,
                      emotional_context_json, outcomes_json, topics_json,
                      open_loops_json, created_at, updated_at
               FROM conversation_episodes
               WHERE (created_at>=? AND created_at<?)
                  OR (updated_at>=? AND updated_at<?)
                  OR EXISTS (
                      SELECT 1 FROM episode_turns et JOIN messages m ON m.turn_id=et.turn_id
                      LEFT JOIN webhook_steps ws
                        ON m.turn_id=('webhook:' || ws.run_id || ':' || ws.step_index)
                      LEFT JOIN webhook_runs wr ON wr.id=ws.run_id
                      WHERE et.episode_id=conversation_episodes.id
                        AND ({_MESSAGE_TIME_SQL})>=? AND ({_MESSAGE_TIME_SQL})<?
                  )
               ORDER BY updated_at""",
            (
                start,
                end,
                start,
                end,
                start,
                end,
            ),
        ).fetchall()
        for row in episode_rows:
            # working_summary contains verbatim evidence quotes, not a narrative.
            summary = str(row["narrative_summary"] or "").strip()
            topics = _reflection_json(
                row["topics_json"], [], record_id=row["id"], field="topics_json"
            )
            loops = _reflection_json(
                row["open_loops_json"],
                [],
                record_id=row["id"],
                field="open_loops_json",
            )
            emotional = _reflection_json(
                row["emotional_context_json"],
                {},
                record_id=row["id"],
                field="emotional_context_json",
            )
            outcomes = _reflection_json(
                row["outcomes_json"],
                [],
                record_id=row["id"],
                field="outcomes_json",
            )
            parts = [
                f"{self.context_timestamp(row['updated_at'])} id={row['id']} {row['status']} {row['title']}",
            ]
            if summary:
                parts.append(f"summary={_reflection_compact_value(summary, 320)}")
            if topics:
                parts.append(f"topics={_reflection_compact_value(topics, 180)}")
            if emotional:
                parts.append(f"emotional_context={_reflection_compact_value(emotional, 180)}")
            if outcomes:
                parts.append(f"outcomes={_reflection_compact_value(outcomes, 180)}")
            if loops:
                parts.append(f"open_loops={_reflection_compact_value(loops, 180)}")
            episode_entries.append("; ".join(parts))
        return {
            "mood_timeline": truncate_tokens(
                "\n".join(mood_entries), 1200
            ) or "(no recorded mood changes)",
            "episode_timeline": "\n".join(episode_entries) or "(no episode in this period)",
            "start_at": start,
            "end_at": end,
        }
