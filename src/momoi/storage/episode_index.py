import json

from ..search import search_alternatives
from .context_plan_adapter import normalize_context_plan
from .context_plans import recall_query_texts


class EpisodeIndexStore:
    @staticmethod
    def _recall_terms(*values: object) -> set[str]:
        terms: set[str] = set()

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    collect(key)
                    collect(item)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    collect(item)
                return
            terms.update(search_alternatives(str(value or "")))

        for value in values:
            collect(value)
        return terms

    def _episode_recall_key(self, episode_id: str) -> int:
        self._db.execute(
            """INSERT OR IGNORE INTO recall_episode_ids (episode_id)
               VALUES (?)""",
            (episode_id,),
        )
        row = self._db.execute(
            "SELECT id FROM recall_episode_ids WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("episode recall id was not created")
        return int(row["id"])

    def _recall_term_ids(self, terms: set[str], *, create: bool) -> dict[str, int]:
        if not terms:
            return {}
        ordered = sorted(terms)
        if create:
            self._db.executemany(
                "INSERT OR IGNORE INTO recall_terms (term) VALUES (?)",
                ((term,) for term in ordered),
            )
        resolved: dict[str, int] = {}
        for offset in range(0, len(ordered), 500):
            chunk = ordered[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._db.execute(
                f"SELECT id, term FROM recall_terms WHERE term IN ({placeholders})",
                chunk,
            ).fetchall()
            resolved.update({str(row["term"]): int(row["id"]) for row in rows})
        return resolved

    def _index_episode_terms(self, episode_id: str, *values: object) -> None:
        episode_key = self._episode_recall_key(episode_id)
        term_ids = self._recall_term_ids(self._recall_terms(*values), create=True)
        self._db.executemany(
            """INSERT OR IGNORE INTO episode_recall_terms
               (episode_key, term_id) VALUES (?, ?)""",
            ((episode_key, term_id) for term_id in term_ids.values()),
        )

    def _index_episode_message_terms(
        self,
        episode_id: str,
        message_id: int,
        content: str,
        terms: set[str] | None = None,
    ) -> None:
        terms = self._recall_terms(content) if terms is None else terms
        episode_key = self._episode_recall_key(episode_id)
        term_ids = self._recall_term_ids(terms, create=True)
        self._db.executemany(
            """INSERT OR IGNORE INTO episode_message_recall_terms
               (episode_key, message_id, term_id) VALUES (?, ?, ?)""",
            (
                (episode_key, message_id, term_id)
                for term_id in term_ids.values()
            ),
        )

    def _index_turn_episode_terms(self, turn_id: str) -> None:
        messages = self._db.execute(
            """SELECT id, role, content, delivery_state FROM messages
               WHERE turn_id=?""",
            (turn_id,),
        ).fetchall()
        plan_row = self._db.execute(
            """SELECT plan_json FROM context_plans
               WHERE turn_id=? AND state<>'superseded'
               ORDER BY revision DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        plan = (
            normalize_context_plan(json.loads(str(plan_row["plan_json"])))
            if plan_row
            else {}
        )
        units = {
            str(unit["id"]): unit
            for unit in plan.get("intent_units", [])
            if isinstance(unit, dict) and unit.get("id")
        }
        for episode in self._db.execute(
            """SELECT episode_id, relation, unit_ids_json FROM episode_turns
               WHERE turn_id=?""",
            (turn_id,),
        ).fetchall():
            episode_id = str(episode["episode_id"])
            unit_values = [
                units[unit_id]
                for unit_id in json.loads(str(episode["unit_ids_json"]))
                if unit_id in units
            ]
            unit_terms = self._recall_terms(
                *(
                    value
                    for unit in unit_values
                    for value in (
                        unit.get("text"),
                        unit.get("intent"),
                        unit.get("references"),
                        [
                            text
                            for query in unit.get("recall_queries") or []
                            for text in recall_query_texts(query)
                        ],
                    )
                )
            )
            episode_key = self._episode_recall_key(episode_id)
            self._db.execute(
                """DELETE FROM episode_message_recall_terms
                   WHERE episode_key=? AND message_id IN (
                       SELECT id FROM messages WHERE turn_id=?
                   )""",
                (episode_key, turn_id),
            )
            if unit_terms:
                self._index_episode_terms(episode_id, *unit_values)
            for message in messages:
                if message["role"] == "assistant" and message["delivery_state"] not in {
                    "delivered",
                    "uncertain",
                    "internal",
                }:
                    continue
                content = str(message["content"])
                content_terms = self._recall_terms(content)
                if not unit_terms:
                    indexed_terms = content_terms
                elif message["role"] == "user":
                    indexed_terms = unit_terms
                elif content_terms & unit_terms or episode["relation"] == "primary":
                    indexed_terms = content_terms
                else:
                    continue
                self._index_episode_terms(episode_id, *indexed_terms)
                self._index_episode_message_terms(
                    episode_id, int(message["id"]), content, indexed_terms
                )

    def _reindex_episode_terms(self, episode_id: str) -> None:
        episode = self.episode(episode_id)
        if episode is None:
            return
        episode_key = self._episode_recall_key(episode_id)
        self._db.execute(
            "DELETE FROM episode_recall_terms WHERE episode_key=?", (episode_key,)
        )
        self._db.execute(
            "DELETE FROM episode_message_recall_terms WHERE episode_key=?",
            (episode_key,),
        )
        self._index_episode_terms(
            episode_id,
            episode["title"],
            episode["working_summary"],
            episode["narrative_summary"],
            episode["emotional_context"],
            episode["outcomes"],
            episode["topics"],
            episode["entities"],
            episode["open_loops"],
        )
        turns = self._db.execute(
            "SELECT turn_id FROM episode_turns WHERE episode_id=? ORDER BY ordinal",
            (episode_id,),
        ).fetchall()
        for turn in turns:
            self._index_turn_episode_terms(str(turn["turn_id"]))
