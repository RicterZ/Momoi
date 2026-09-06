import re
import uuid

from ...models import IncomingMessage
from ...storage import MemoryRecallQuery
from ..agent.context_window import context_compaction_tokens
from .presentation import episode_candidate_lines, recall_context_lines
from .rendering import assemble_main_context
from .retrieval import build_plan_retrieval, select_plan_recall_queries

_NEW_EPISODE_SLUG = re.compile(r"new:[a-z0-9][a-z0-9_-]{0,39}")


class ContextService:
    def _context_compaction_tokens(self) -> int:
        return context_compaction_tokens(self.config)

    def _episode_raw_token_budget(self) -> int:
        return max(1000, self._context_compaction_tokens() // 2)

    def _recent_conversation_rows(
        self, before_timestamp: float | None = None
    ) -> list[dict[str, object]]:
        turn_limit = self.store.transcript_window_turn_limit(
            self.config.transcript_turns_min,
            self.config.transcript_turns_max,
        )
        return self.store.recent_conversation_messages(
            turn_limit,
            self._context_compaction_tokens(),
            before_timestamp,
        )

    def _plan_from_submission(
        self,
        events: list[IncomingMessage],
        arguments: dict[str, object],
        *,
        turn_id: str,
        revision: int,
    ) -> dict[str, object]:
        """Shape an Owner context submission like a stored plan.

        The retrieval path already knows how to turn recall dispositions and
        Episode actions into evidence; only its source moves, from a separate
        planning model to the Owner's own first action.
        """

        event_ids = [event.event_id for event in events]
        units: list[dict[str, object]] = []
        episodes: list[dict[str, object]] = []
        raw_units = arguments.get("units")
        if not isinstance(raw_units, list) or not raw_units:
            raise ValueError("recall requires at least one intent unit")
        candidate_rows = self.store.recent_conversation_messages(
            self.store.transcript_window_turn_limit(
                self.config.transcript_turns_min,
                self.config.transcript_turns_max,
            ),
            self._context_compaction_tokens(),
            min(event.received_at for event in events),
        )
        candidate_ids = {
            str(item["id"])
            for item in self.store.episode_directory_for_turns(
                [str(row["turn_id"]) for row in candidate_rows],
                exclude_runtime_archives=True,
            )
            if item.get("id")
        }
        for index, raw in enumerate(raw_units if isinstance(raw_units, list) else [], 1):
            if not isinstance(raw, dict):
                raise ValueError("each recall unit must be an object")
            unit_id = f"u{index}"
            mode = str(raw.get("recall_mode") or "search")
            queries = [
                {
                    "semantic": " ".join(str(query.get("semantic") or "").split())[:240],
                    "keywords": [
                        " ".join(str(keyword).split())[:60]
                        for keyword in (query.get("keywords") or [])
                        if " ".join(str(keyword).split())
                    ],
                }
                for query in (raw.get("recall_queries") or [])
                if isinstance(query, dict) and str(query.get("semantic") or "").strip()
            ][:3]
            from_turn_id = str(raw.get("recall_from_turn_id") or "")
            if mode not in {"search", "reuse"}:
                raise ValueError("recall_mode must be search or reuse")
            if mode == "search":
                if not queries:
                    raise ValueError("search recall requires at least one query")
                from_turn_id = ""
            elif not from_turn_id or not self.store.recall_reuse_candidates(
                [from_turn_id]
            ):
                raise ValueError("reuse requires a displayed recalled Turn")
            units.append(
                {
                    "id": unit_id,
                    "event_ids": event_ids,
                    "intent": " ".join(str(raw.get("intent") or "").split())[:160],
                    "recall_mode": mode,
                    "recall_queries": queries if mode == "search" else [],
                    "recall_from_turn_id": from_turn_id if mode == "reuse" else "",
                    "recall": {
                        "mode": mode,
                        "from_turn_id": from_turn_id if mode == "reuse" else "",
                        "queries": queries if mode == "search" else [],
                    },
                }
            )
            episode = raw.get("episode")
            action = (
                str(episode.get("action") or "none")
                if isinstance(episode, dict)
                else "none"
            )
            if action not in {"none", "continue", "new"}:
                raise ValueError("episode action must be none, continue, or new")
            if action == "none":
                continue
            binding: dict[str, object] = {"action": action, "unit_ids": [unit_id]}
            reference = str(episode.get("ref") or "") if isinstance(episode, dict) else ""
            title = str(episode.get("title") or "") if isinstance(episode, dict) else ""
            if action == "continue" and reference in candidate_ids:
                binding["episode_id"] = reference
                binding["episode_ref"] = reference
            elif action == "new" and title and _NEW_EPISODE_SLUG.fullmatch(reference):
                binding["episode_id"] = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"momoi:episode:{turn_id}:{revision}:{reference}",
                ).hex
                binding["title"] = title[:80]
                binding["episode_ref"] = reference
            else:
                raise ValueError("episode reference does not match its action")
            episodes.append(binding)
        return {
            "version": 7,
            "intent_units": units,
            "episode_actions": episodes,
            "episode_links": [],
            "uncertainty": [],
        }

    def owner_context_baseline(
        self, events: list[IncomingMessage]
    ) -> dict[str, str]:
        """Assemble the context that holds before any recall decision is made.

        The fixed memory baseline, Goals and folded external events do not
        depend on what this input turns out to need, so they are available
        before the Owner decides anything. Query-driven evidence arrives later,
        as the result of that decision.
        """

        retrieval = build_plan_retrieval(
            self.store,
            {"version": 7, "intent_units": [], "episode_actions": []},
            self.config,
        )
        return assemble_main_context(
            self.store,
            retrieval,
            self.config.summary_tokens,
            recent_before_timestamp=min(event.received_at for event in events),
        )

    def owner_context_candidates(
        self, turn_ids: list[str], labels: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Give the Owner the two catalogs its context decision depends on.

        Continuing an Episode requires seeing which ones are open, and reusing a
        previous recall requires seeing what that recall actually searched for.
        Both were previously visible only to the planning model, which is why
        that model appeared to know something the Owner could not.
        """

        return {
            "candidate_episodes": episode_candidate_lines(
                self.store.episode_directory_for_turns(
                    turn_ids,
                    exclude_runtime_archives=True,
                ),
                labels or {},
            ),
            "recent_recall_context": recall_context_lines(
                self.store.recall_reuse_candidates(turn_ids)
            ),
        }

    async def submit_owner_context(
        self,
        events: list[IncomingMessage],
        turn_id: str,
        arguments: dict[str, object],
    ) -> dict[str, str]:
        """Persist the Owner's context decision and return the evidence it asked for."""

        record = self.store.context_plan(turn_id)
        revision = int(record["revision"]) + 1 if record is not None else 1
        plan = self._plan_from_submission(
            events,
            arguments,
            turn_id=turn_id,
            revision=revision,
        )
        saved = self.store.save_context_plan(
            turn_id, revision, [event.event_id for event in events], plan
        )
        selected, _reused, _emitted, _skipped = select_plan_recall_queries(plan)
        dense_evidence = await self.semantic_recall.prepare(
            [
                MemoryRecallQuery(
                    expression=str(item["expression"]),
                    unit_ids=tuple(str(value) for value in item["unit_ids"]),
                    priority=int(item["priority"]),
                    semantic_expression=str(item["semantic_expression"]),
                )
                for item in selected
            ],
            output_limit=max(self.config.memory_results, self.config.summary_results),
        )
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence
        )
        stored = self.store.save_context_retrieval(
            turn_id, int(saved["revision"]), retrieval, state="recalled"
        )
        return assemble_main_context(
            self.store,
            stored["retrieval"],
            self.config.summary_tokens,
            recent_before_timestamp=min(event.received_at for event in events),
        )

    async def prepare_heartbeat_context(
        self, arguments: dict[str, object]
    ) -> dict[str, object]:
        activity = " ".join(str(arguments.get("activity") or "").split())[:300]
        mode = str(arguments.get("mode") or "")
        recall_mode = str(arguments.get("recall_mode") or "")
        strategy = [
            " ".join(str(item).split())[:300]
            for item in (arguments.get("strategy") or [])
            if str(item).strip()
        ][:4]
        raw_queries = arguments.get("recall_queries")
        queries = [
            {
                "semantic": " ".join(str(query.get("semantic") or "").split())[:240],
                "keywords": [
                    " ".join(str(keyword).split())[:60]
                    for keyword in (query.get("keywords") or [])
                    if " ".join(str(keyword).split())
                ][:6],
            }
            for query in (raw_queries if isinstance(raw_queries, list) else [])
            if isinstance(query, dict) and str(query.get("semantic") or "").strip()
        ][:2]
        if not activity or mode not in {"work", "rest"}:
            raise ValueError("heartbeat_begin requires an activity and work/rest mode")
        if recall_mode not in {"search", "skip"}:
            raise ValueError("heartbeat recall_mode must be search or skip")
        if recall_mode == "search" and not queries:
            raise ValueError("heartbeat search requires at least one recall query")
        if recall_mode == "skip" and queries:
            raise ValueError("heartbeat skip requires empty recall_queries")
        if mode == "rest" and strategy:
            raise ValueError("heartbeat rest requires an empty strategy")
        if mode == "work" and not strategy:
            raise ValueError("heartbeat work requires an execution strategy")
        plan = {
            "version": 4,
            "activity": {
                "intent": activity,
                "recall_mode": recall_mode,
                "recall_queries": queries if recall_mode == "search" else [],
            },
            "strategy": strategy,
        }
        selected, _reused, _emitted, _skipped = select_plan_recall_queries(plan)
        dense_evidence = await self.semantic_recall.prepare(
            [
                MemoryRecallQuery(
                    expression=str(item["expression"]),
                    unit_ids=tuple(str(value) for value in item["unit_ids"]),
                    priority=int(item["priority"]),
                    semantic_expression=str(item["semantic_expression"]),
                )
                for item in selected
            ],
            output_limit=max(self.config.memory_results, self.config.summary_results),
        )
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence
        )
        return {
            "plan": plan,
            "memory_snapshots": self.store.memory_snapshots([
                item["id"] for item in retrieval["recall_memories"]
            ]),
            "context": assemble_main_context(
                self.store,
                retrieval,
                self.config.summary_tokens,
            ),
        }

