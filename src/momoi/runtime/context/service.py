import re
import uuid

from ...models import IncomingMessage
from ...storage import MemoryRecallQuery
from ...storage import MEMORY_KINDS
from ...storage.episode.episode_ranking import EpisodeRecallQuery
from ...semantic.topic_selector import RecallSelection, TOPIC_CANDIDATE_LIMIT, select_topics
from ..agent.context_window import context_compaction_tokens
from .presentation import recent_episode_lines, recall_context_lines
from .rendering import assemble_main_context
from .retrieval import build_plan_retrieval, select_plan_recall_queries

_NEW_EPISODE_SLUG = re.compile(r"new:[a-z0-9][a-z0-9_-]{0,39}")


class ContextService:
    async def _select_recall_topics(self, request, selected, dense_evidence, diagnostics=None):
        if diagnostics is not None:
            diagnostics.update(
                status="skipped", skip_reason="no_queries" if not selected else "disabled",
                fallback_reason=dense_evidence.fallback_reason if dense_evidence else "disabled",
                candidate_count=0, selected_ids=[], candidates=[], memory_candidates=[],
                reflection_candidates=[], selected_memory_ids=[], selected_reflection_ids=[],
            )
        if not selected:
            return RecallSelection([], [], [])
        queries = [EpisodeRecallQuery(
            expression=str(item["expression"]),
            unit_ids=tuple(str(value) for value in item["unit_ids"]),
            priority=int(item["priority"]),
            semantic_expression=str(item["semantic_expression"]),
        ) for item in selected]
        candidates = (
            self.store.search_topic_queries(
                queries, TOPIC_CANDIDATE_LIMIT, dense_evidence=dense_evidence,
                minimum_confidence=0,
            )
            if self.config.summary_results > 0 else []
        )
        memory_candidates = self.store.rank_recalled_memories(
            [
                MemoryRecallQuery(
                    expression=str(item["expression"]),
                    unit_ids=tuple(str(value) for value in item["unit_ids"]),
                    priority=int(item["priority"]),
                    semantic_expression=str(item["semantic_expression"]),
                    kinds=tuple(str(kind) for kind in item.get("kinds") or []),
                )
                for item in selected
            ],
            max(0, self.config.memory_results),
            dense_evidence=dense_evidence,
        )
        return await select_topics(
            self.provider, self.store, request, queries, candidates,
            memory_candidates=memory_candidates,
            thinking_effort=self.config.thinking_stages.get("topic_selection", "low"),
            diagnostics=diagnostics,
        )

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
            raise ValueError("units: required nonempty JSON array of intent objects; wrap fields as {\"units\":[{...}]}")
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
            path = f"units[{index - 1}]"
            if not isinstance(raw.get("recall_queries"), list):
                raise ValueError(f"{path}.recall_queries: expected a JSON array, not a string; use [] for skip/reuse")
            if not isinstance(raw.get("episode"), dict):
                raise ValueError(f'{path}.episode: expected a JSON object, e.g. {{"action":"none"}}, not a string')
            unit_id = f"u{index}"
            raw_kinds = raw.get("kind", [])
            if raw_kinds is None:
                raw_kinds = []
            if (
                not isinstance(raw_kinds, list)
                or len(raw_kinds) > len(MEMORY_KINDS)
                or len(set(raw_kinds)) != len(raw_kinds)
                or any(not isinstance(kind, str) or kind not in MEMORY_KINDS for kind in raw_kinds)
            ):
                raise ValueError(
                    f"{path}.kind: expected an optional unique array of canonical memory kinds; empty means all"
                )
            kinds = list(raw_kinds)
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
            if mode not in {"search", "reuse", "skip"}:
                raise ValueError(f"{path}.recall_mode: must be search, reuse, or skip")
            if mode == "search":
                if not queries:
                    raise ValueError(f'{path}.recall_queries: search requires at least one object with nonempty semantic, e.g. [{{"semantic":"此前约定的时间","keywords":[]}}]')
                from_turn_id = ""
            elif mode == "skip":
                if (
                    raw.get("recall_queries") != []
                    or raw.get("recall_from_turn_id") != ""
                ):
                    raise ValueError(
                        f'{path}: skip requires empty recall_queries=[] and recall_from_turn_id=""'
                    )
            elif not from_turn_id or not self.store.recall_reuse_candidates(
                [from_turn_id]
            ):
                raise ValueError(f"{path}.recall_from_turn_id: reuse requires an actual displayed recalled Turn id from recent_recall_context")
            units.append(
                {
                    "id": unit_id,
                    "event_ids": event_ids,
                    "intent": " ".join(str(raw.get("intent") or "").split())[:160],
                    "recall_mode": mode,
                    "recall_queries": queries if mode == "search" else [],
                    "recall_from_turn_id": from_turn_id if mode == "reuse" else "",
                    "kind": kinds,
                    "recall": {
                        "mode": mode,
                        "from_turn_id": from_turn_id if mode == "reuse" else "",
                        "queries": queries if mode == "search" else [],
                        "kind": kinds,
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
                raise ValueError(f"{path}.episode: episode reference does not match its action; continue requires ref copied from a candidate Episode; new requires ref matching new:[a-z0-9][a-z0-9_-]{{0,39}} and a nonempty title")
            episodes.append(binding)
        return {
            "version": 7,
            "intent_units": units,
            "episode_actions": episodes,
            "episode_links": [],
            "uncertainty": [],
        }

    def owner_context_baseline(self) -> dict[str, str]:
        """Assemble the context that holds before any recall decision is made.

        The fixed memory baseline and Goals do not
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
            "recent_episodes": recent_episode_lines(
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
        dense_evidence = None
        if selected:
            dense_evidence = await self.semantic_recall.prepare(
                [
                    MemoryRecallQuery(
                        expression=str(item["expression"]),
                        unit_ids=tuple(str(value) for value in item["unit_ids"]),
                        priority=int(item["priority"]),
                        semantic_expression=str(item["semantic_expression"]),
                        kinds=tuple(str(kind) for kind in item.get("kinds") or []),
                    )
                    for item in selected
                ],
                output_limit=max(self.config.memory_results, TOPIC_CANDIDATE_LIMIT),
            )
        topic_selection = {}
        selection = await self._select_recall_topics(
            "\n".join(event.text for event in events), selected, dense_evidence, topic_selection
        )
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence,
            selected_episode_rows=selection.episodes,
            selected_memory_rows=[*selection.memories, *selection.reflections],
            topic_selection=topic_selection,
        )
        stored = self.store.save_context_retrieval(
            turn_id, int(saved["revision"]), retrieval, state="recalled"
        )
        return assemble_main_context(
            self.store,
            stored["retrieval"],
            self.config.summary_tokens,
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
            output_limit=max(self.config.memory_results, TOPIC_CANDIDATE_LIMIT),
        )
        topic_selection = {}
        selection = await self._select_recall_topics(activity, selected, dense_evidence, topic_selection)
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence,
            selected_episode_rows=selection.episodes,
            selected_memory_rows=[*selection.memories, *selection.reflections],
            topic_selection=topic_selection,
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
