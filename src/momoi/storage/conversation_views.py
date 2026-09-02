import json
import re

from .episode_sql import runtime_archive_kind_sql
from .integrity import decode_stored_json
from .memory_values import estimate_tokens, token_chunk


class ConversationViewStore:
    def list_episode_directory(
        self,
        limit: int = 64,
        *,
        after: float | None = None,
        exclude_runtime_archives: bool = False,
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            f"""SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE (? IS NULL OR COALESCE((
                   SELECT MAX(t.updated_at) FROM episode_turns AS et
                   JOIN turns AS t ON t.id=et.turn_id
                   WHERE et.episode_id=e.id
               ), e.updated_at)>=?)
               AND (?=0 OR {runtime_archive_kind_sql('e')} IS NULL)
               ORDER BY status='open' DESC, status='closing' DESC,
                        COALESCE((
                            SELECT MAX(t.updated_at) FROM episode_turns AS et
                            JOIN turns AS t ON t.id=et.turn_id
                            WHERE et.episode_id=e.id
                        ), e.updated_at) DESC, salience DESC LIMIT ?""",
            (after, after, int(exclude_runtime_archives), limit),
        ).fetchall()
        results = []
        for row in rows:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = self.context_timestamp(
                row["last_activity_at"]
            )
            results.append(episode)
        return results

    def list_recent_episode_directory(
        self, limit: int = 8, *, exclude_runtime_archives: bool = False
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            f"""SELECT e.*, COALESCE((
                       SELECT MAX(t.updated_at) FROM episode_turns AS et
                       JOIN turns AS t ON t.id=et.turn_id
                       WHERE et.episode_id=e.id
                   ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE ?=0 OR {runtime_archive_kind_sql('e')} IS NULL
               ORDER BY last_activity_at DESC, e.id DESC
               LIMIT ?""",
            (int(exclude_runtime_archives), limit),
        ).fetchall()
        results = []
        for row in rows:
            episode = self._episode_dict(row)
            episode["last_activity_timestamp"] = self.context_timestamp(
                row["last_activity_at"]
            )
            results.append(episode)
        return results

    def episode_directory_for_turns(
        self,
        turn_ids: list[str],
        *,
        exclude_runtime_archives: bool = False,
    ) -> list[dict[str, object]]:
        ordered_ids = [str(value) for value in dict.fromkeys(turn_ids) if value]
        if not ordered_ids:
            return []
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._db.execute(
            f"""SELECT e.*, MAX(t.updated_at) AS last_activity_at,
                       GROUP_CONCAT(DISTINCT selected.turn_id) AS selected_turn_ids
                FROM episode_turns AS selected
                JOIN conversation_episodes AS e ON e.id=selected.episode_id
                JOIN episode_turns AS all_turns ON all_turns.episode_id=e.id
                JOIN turns AS t ON t.id=all_turns.turn_id
                WHERE selected.turn_id IN ({placeholders})
                GROUP BY e.id
                ORDER BY last_activity_at DESC, e.id DESC""",
            tuple(ordered_ids),
        ).fetchall()
        results = []
        for row in rows:
            episode_id = str(row["id"])
            if exclude_runtime_archives and self._runtime_archive_kind(episode_id):
                continue
            results.append(
                {
                    "id": episode_id,
                    "title": str(row["title"]),
                    "last_activity_timestamp": self.context_timestamp(
                        row["last_activity_at"]
                    ),
                    "turn_ids": [
                        value
                        for value in str(row["selected_turn_ids"] or "").split(",")
                        if value
                    ],
                }
            )
        return results

    def list_dashboard_conversations(
        self, limit: int = 64
    ) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        episode_rows = self._db.execute(
            """SELECT * FROM conversation_episodes
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        items = [
            {**self._episode_dict(row), "record_type": "episode"}
            for row in episode_rows
        ]
        turn_rows = self._db.execute(
            """SELECT t.id, t.updated_at, d.action, d.reason,
                      (
                          SELECT m.content FROM messages AS m
                          WHERE m.turn_id=t.id AND m.role='user'
                          ORDER BY m.id LIMIT 1
                      ) AS owner_content,
                      (
                          SELECT m.content FROM messages AS m
                          WHERE m.turn_id=t.id AND m.role='assistant'
                            AND m.delivery_state IN ('delivered', 'uncertain')
                          ORDER BY m.id DESC LIMIT 1
                      ) AS assistant_content
               FROM turns AS t
               LEFT JOIN episode_consolidation_decisions AS d ON d.turn_id=t.id
               WHERE t.state='completed'
                 AND NOT EXISTS (
                     SELECT 1 FROM episode_turns AS et WHERE et.turn_id=t.id
                 )
                 AND EXISTS (
                     SELECT 1 FROM messages AS m
                     WHERE m.turn_id=t.id
                       AND (
                           m.role='user'
                           OR m.role='assistant'
                              AND m.delivery_state IN ('delivered', 'uncertain')
                       )
                 )
               ORDER BY t.updated_at DESC, t.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        for row in turn_rows:
            owner = re.sub(
                r"^\d{4}-\d{2}-\d{2}T\S+\s+",
                "",
                str(row["owner_content"] or ""),
            ).strip()
            assistant = " ".join(str(row["assistant_content"] or "").split())
            status = str(row["action"] or "unclassified")
            items.append(
                {
                    "id": str(row["id"]),
                    "turn_id": str(row["id"]),
                    "record_type": "turn",
                    "status": status,
                    "title": (" ".join(owner.split()) or assistant or "未归类聊天")[:80],
                    "working_summary": "",
                    "summary": str(row["reason"] or assistant)[:240],
                    "topics": [],
                    "updated_at": float(row["updated_at"]),
                }
            )
        items.sort(
            key=lambda item: (float(item.get("updated_at") or 0), str(item["id"])),
            reverse=True,
        )
        return items[:limit]

    def dashboard_conversation_turn(
        self, turn_id: str
    ) -> dict[str, object] | None:
        row = self._db.execute(
            """SELECT t.id, t.updated_at, d.action, d.reason
               FROM turns AS t
               LEFT JOIN episode_consolidation_decisions AS d ON d.turn_id=t.id
               WHERE t.id=? AND t.state='completed'""",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        messages = [
            {
                "id": int(message["id"]),
                "role": str(message["role"]),
                "content": str(message["content"]),
                "created_at": float(message["created_at"]),
                "delivery_state": str(message["delivery_state"]),
            }
            for message in self._db.execute(
                """SELECT id, role, content, created_at, delivery_state
                   FROM messages
                   WHERE turn_id=? AND role IN ('user', 'assistant')
                   ORDER BY id""",
                (turn_id,),
            ).fetchall()
        ]
        owner = next(
            (str(message["content"]) for message in messages if message["role"] == "user"),
            "",
        )
        owner = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+", "", owner).strip()
        status = str(row["action"] or "unclassified")
        return {
            "id": turn_id,
            "turn_id": turn_id,
            "record_type": "turn",
            "status": status,
            "title": (" ".join(owner.split()) or "未归类聊天")[:80],
            "working_summary": "",
            "summary": str(row["reason"] or ""),
            "topics": [],
            "updated_at": float(row["updated_at"]),
            "messages": messages,
            "truncated": False,
            "next_before_ordinal": None,
        }

    def open_conversation_inventory(self, limit: int = 64) -> list[dict[str, object]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            f"""SELECT e.id, e.status, e.title, e.working_summary, e.open_loops_json,
                      e.updated_at,
                      COALESCE((
                          SELECT MAX(t.updated_at) FROM episode_turns AS et
                          JOIN turns AS t ON t.id=et.turn_id
                          WHERE et.episode_id=e.id
                      ), e.updated_at) AS last_activity_at
               FROM conversation_episodes AS e
               WHERE e.status IN ('open', 'closing')
                 AND {runtime_archive_kind_sql('e')} IS NULL
               ORDER BY e.status='open' DESC, last_activity_at DESC, e.updated_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        inventory: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["open_loops"] = decode_stored_json(
                item.pop("open_loops_json"),
                entity="conversation_episode",
                record_id=item["id"],
                field="open_loops_json",
                expected_type=list,
                fallback=[],
            )
            item["last_activity_timestamp"] = self.context_timestamp(
                item["last_activity_at"]
            )
            item["updated_timestamp"] = self.context_timestamp(item.pop("updated_at"))
            inventory.append(item)
        return inventory

    def open_conversation_inventory_context(self) -> str:
        rows = self.open_conversation_inventory()
        if not rows:
            return "No open or closing conversations are stored."
        lines = [
            "Inventory of conversations still marked open or closing. Use episode_id "
            "in conversation_actions to close a thread that is finished or expired. "
            "Leave it unchanged when it may still continue."
        ]
        for row in rows:
            summary = " ".join(str(row["working_summary"] or "").split())[:240]
            loops = json.dumps(row["open_loops"], ensure_ascii=False)
            lines.append(
                f"episode_id={row['id']} status={row['status']} title={row['title']} "
                f"last_activity={row['last_activity_timestamp']} open_loops={loops}"
                + (f" summary={summary}" if summary else "")
            )
        return "\n".join(lines)

    def episode_turns(self, episode_id: str) -> list[dict[str, object]]:
        rows = self._db.execute(
            "SELECT * FROM episode_turns WHERE episode_id=? ORDER BY ordinal",
            (episode_id,),
        ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "unit_ids_json"},
                "unit_ids": json.loads(str(row["unit_ids_json"])),
            }
            for row in rows
        ]

    def episode_messages(
        self,
        episode_id: str,
        token_budget: int,
        *,
        after_ordinal: int = 0,
        before_ordinal: int | None = None,
        exclude_message_ids: set[int] | None = None,
        include_nondelivered: bool = False,
        after: float | None = None,
        before: float | None = None,
    ) -> list[dict[str, object]]:
        if token_budget <= 0:
            return []
        rows = self._db.execute(
            """SELECT m.id, m.turn_id, et.ordinal, m.role, m.content, m.created_at,
                      m.delivery_state
               FROM episode_turns AS et
               JOIN messages AS m ON m.turn_id=et.turn_id
               WHERE et.episode_id=? AND et.ordinal>?
                 AND (? IS NULL OR et.ordinal<?)
                 AND (? IS NULL OR m.created_at>=?)
                 AND (? IS NULL OR m.created_at<?)
                 AND (? OR m.role IN ('user', 'event') OR m.delivery_state IN
                      ('delivered', 'uncertain', 'internal'))
               ORDER BY et.ordinal DESC, m.id""",
            (
                episode_id,
                after_ordinal,
                before_ordinal,
                before_ordinal,
                after,
                after,
                before,
                before,
                int(include_nondelivered),
            ),
        ).fetchall()
        excluded = exclude_message_ids or set()
        groups: list[list[dict[str, object]]] = []
        for row in rows:
            item = dict(row)
            item["timestamp"] = self.context_timestamp(item["created_at"])
            if int(item["id"]) in excluded:
                continue
            if not groups or groups[-1][0]["turn_id"] != item["turn_id"]:
                groups.append([])
            groups[-1].append(item)
        selected: list[list[dict[str, object]]] = []
        used = 0
        for group in groups:
            size = sum(estimate_tokens(str(item["content"])) for item in group)
            if selected and used + size > token_budget:
                break
            if not selected and size > token_budget:
                if len(group) > token_budget:
                    group = (
                        [group[0]]
                        if token_budget == 1
                        else [group[0], *group[-(token_budget - 1) :]]
                    )
                per_message = max(1, token_budget // len(group))
                for item in group:
                    content, next_offset = token_chunk(
                        str(item["content"]), 0, per_message
                    )
                    item["content"] = content
                    item["content_offset"] = 0
                    item["next_content_offset"] = next_offset
                size = sum(estimate_tokens(str(item["content"])) for item in group)
            selected.append(group)
            used += size
        return [item for group in reversed(selected) for item in group]
