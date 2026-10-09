import copy

from ...models import IncomingMessage
from ...memory import Memory, MemoryRecallQuery
from ...storage.episode.episode_ranking import EpisodeRecallQuery
from .memory_search import filtered_candidates
from ...storage.memory.catalog import MOMOI_MEMORY_TAGS
from .selection import RecallSelection, TOPIC_CANDIDATE_LIMIT, select_topics
from ..agent.context_window import context_compaction_tokens
from ..turn_support import context_data_message
from .presentation import recent_episode_lines, recall_context_lines
from .rendering import assemble_main_context
from .retrieval import build_plan_retrieval, select_plan_recall_queries


class ContextService:
    memory: Memory

    async def _select_recall_topics(self, request, selected, dense_evidence, diagnostics=None, *, model_selection=True, exclude_episode_ids=()):
        if diagnostics is not None:
            diagnostics.update(
                status="skipped", skip_reason="no_queries" if not selected else "disabled",
                fallback_reason=dense_evidence.fallback_reason if dense_evidence else "disabled",
                candidate_count=0, selected_ids=[], candidates=[], memory_candidates=[],
                reflection_candidates=[], selected_memory_ids=[], selected_reflection_ids=[],
            )
        if not selected:
            return RecallSelection([], [], [])
        if all(not item["semantic_expression"] and not item["expression"] for item in selected):
            memories = self.memory.rank(
                [], min(6, max(0, self.config.memory_results)),
                filters=selected[0].get("memory_filters"),
            )
            for row in memories:
                row["unit_ids"] = selected[0]["unit_ids"]
            if diagnostics is not None:
                diagnostics.update(status="skipped", skip_reason="metadata_only",
                                   memory_candidates=memories, selected_memory_ids=[row["id"] for row in memories])
            return RecallSelection([], memories, [])
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
                exclude_episode_ids=exclude_episode_ids,
            )
            if self.config.summary_results > 0 else []
        )
        if not model_selection:
            if diagnostics is not None:
                diagnostics.update(status="skipped", skip_reason="workflow_selects_candidates",
                                   candidate_count=len(candidates),
                                   selected_ids=[str(row["id"]) for row in candidates])
            return RecallSelection(candidates, [], [])
        memory_queries = [
            MemoryRecallQuery(
                expression=str(item["expression"]),
                unit_ids=tuple(str(value) for value in item["unit_ids"]),
                priority=int(item["priority"]),
                semantic_expression=str(item["semantic_expression"]),
                kinds=tuple(str(kind) for kind in item.get("kinds") or []),
            )
            for item in selected
        ]
        selection = RecallSelection([], [], [])

        async def joint_reranker(current_request, memory_candidates):
            nonlocal selection
            selection = await select_topics(
                self.provider, self.store, current_request, queries, candidates,
                memory_candidates=memory_candidates,
                thinking_effort=self.config.thinking_stages.get("topic_selection", "low"),
                diagnostics=diagnostics,
            )
            return [*selection.memories, *selection.reflections]

        if any(item.get("memory_filters") for item in selected):
            candidates_memory = (await filtered_candidates(self.memory, selected, dense_evidence)
                                 if self.config.memory_results > 0 else [])
            memories = (await joint_reranker(request, candidates_memory))[:min(6, max(0, self.config.memory_results))]
        else:
            memories = await self.memory.search(
                memory_queries, max(0, self.config.memory_results), request=request,
                dense_evidence=dense_evidence, reranker=joint_reranker,
            )
        return RecallSelection(
            selection.episodes,
            [row for row in memories if row["source"] == "confirmed"],
            [row for row in memories if row["source"] == "reflection"],
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
            include_images=True,
        )

    def shared_turn_context(self, turn_id: str) -> dict[str, object]:
        """One canonical prefix and transcript for every conversation executor."""
        from ..transcript.building import build_transcript
        from ..transcript.rendering import render_messages

        cutoff = float(self.store.turn_usage(turn_id)["started_at"])
        rows = self.store.retained_transcript_rows(self._recent_conversation_rows(cutoff))
        ids = list(dict.fromkeys(str(row["turn_id"]) for row in rows))
        exchanges = self.store.turn_exchanges(ids, include_reply_messages=True)
        # Only unreplayable historical assistant speech marks a legacy boundary.
        # Runtime events and silent turns legitimately have no LLM exchange.
        legacy_speech = {str(row["turn_id"]) for row in rows
                         if row["role"] == "assistant"
                         and row.get("delivery_state") != "internal"
                         and not exchanges.get(str(row["turn_id"]))}
        last_legacy = max(
            (index for index, identifier in enumerate(ids) if identifier in legacy_speech),
            default=-1,
        )
        ids = ids[last_legacy + 1:]
        retained = set(ids)
        rows = [row for row in rows if str(row["turn_id"]) in retained]
        activity = self.store.turn_activity(ids)
        transcript = build_transcript(rows, timezone=self.store.timezone, tool_activity=activity)
        memory_state = self.store.transcript_memory_context(ids)
        history = render_messages(
            [*transcript.orphaned, *transcript.groups],
            timezone=self.store.timezone, tool_activity=activity,
            native_exchanges={identifier: exchanges.get(identifier, []) for identifier in ids},
            history_format=memory_state["history_format"],
            result_store=self.tool_results,
        )
        from ..transcript.recall import compact_recall_messages
        compact_recall_messages(history, ids, result_store=self.tool_results)
        from ..transcript.recall import remove_folded_memory_evidence
        remove_folded_memory_evidence(history, memory_state.get("folded_overrides", {}))
        memories = {int(key): value for key, value in memory_state["observed"].items()
                    if value["activation"] == "always"}
        snapshot = [value for value in memory_state["snapshot"].values()
                    if value["activation"] == "always"]
        # Insert immutable events after their observed historical turn, never
        # inside a native assistant/tool exchange.
        for event in reversed(memory_state["events"]):
            indexes = [i for i, message in enumerate(history)
                       if event["anchor"] in message.get("_history_turn_ids", ())]
            index = max(indexes) + 1 if indexes else 0
            history.insert(index, {
                "role": "user", "content": event["content"],
                "_memory_change": event["revision"], "_context_prefix": True,
            })
        goals = self.owner_context_baseline()["goal_directory"]
        prefix = context_data_message(
            ("long_term_memories", self.store._memory_context(snapshot)),
            ("memory_overrides", "\n".join(memory_state["snapshot_overrides"].values())),
            ("goal_directory", goals),
            required=True,
        )
        prefix["_memory_snapshot"] = {
            "revision": memory_state["revision"],
            "turn_ids": ids,
            "overrides": "\n".join(memory_state["overrides"].values()),
            "current": self.store._memory_context(list(memories.values())),
            "goals": goals,
        }
        return {
            "rows": rows, "transcript": transcript, "history": history,
            "memories": memories,
            "messages": [prefix, self.episode_context_message(ids, before_timestamp=cutoff), *history],
        }

    def _plan_from_submission(
        self,
        events: list[IncomingMessage],
        arguments: dict[str, object],
        *,
        turn_id: str,
        revision: int,
    ) -> dict[str, object]:
        """Store query provenance internally without exposing routing fields to the model."""
        from ..tool_contracts.context import recall_search_arguments
        search = recall_search_arguments(arguments)
        event_ids = [event.event_id for event in events]
        units = [{
            "id": f"u{index}", "event_ids": event_ids, "intent": semantic,
            "recall_queries": [{"semantic": semantic, "keywords": search["keyword"]}],
            "memory_filters": search["filters"],
        } for index, semantic in enumerate(search["semantic"] or [""], 1)]
        return {
            "version": 7,
            "intent_units": units,
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
            {"version": 7, "intent_units": []},
            self.config,
        )
        return assemble_main_context(
            self.store,
            retrieval,
            self.config.summary_tokens,
        )

    def _recent_episode_candidates(self, turn_ids: list[str]) -> list[dict[str, object]]:
        limit = max(0, self.config.summary_results)
        if not limit:
            return []
        selected = self.store.episode_directory_for_turns(
            turn_ids, exclude_runtime_archives=True
        )[:limit]
        seen = {str(item["id"]) for item in selected}
        if len(selected) < limit:
            for episode in self.store.list_recent_episode_directory(
                limit + len(selected), exclude_runtime_archives=True
            ):
                episode_id = str(episode["id"])
                if episode_id in seen:
                    continue
                selected.append({
                    "id": episode_id,
                    "title": episode["title"],
                    "narrative_summary": episode["narrative_summary"],
                    "last_activity_timestamp": episode["last_activity_timestamp"],
                    "turn_ids": [],
                })
                seen.add(episode_id)
                if len(selected) == limit:
                    break
        return selected

    def episode_context_message(
        self, turn_ids: list[str], *, before_timestamp: float
    ) -> dict[str, object]:
        """Index episodes with historical turns outside the retained transcript."""
        summaries = recent_episode_lines(
            self.store.compacted_episode_directory(
                turn_ids, self.config.summary_results, before_timestamp=before_timestamp
            ),
            {},
        )
        message = context_data_message(
            ("recent_episodes", summaries or "No episode summaries available."),
            required=True,
        )
        assert message is not None
        message["content"] = self.store.transcript_episode_snapshot(message["content"])
        message["_episode_summary_before"] = before_timestamp
        return message

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
                self._recent_episode_candidates(turn_ids),
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
        *,
        model_selection: bool = True,
        current_episode_ids: tuple[str, ...] = (),
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
        selected, _reused, _emitted, _skipped = select_plan_recall_queries(plan)
        dense_evidence = None
        if any(item["semantic_expression"] or item["expression"] for item in selected):
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
        current_episode_ids = tuple(set(current_episode_ids) | {
            item["episode_id"] for item in self.store.episodes_for_turns([turn_id]).values()
        })
        selection = await self._select_recall_topics(
            ("\n".join(event.text for event in events) or
             "\n".join(str(unit["intent"]) for unit in plan["intent_units"])),
            selected, dense_evidence, topic_selection, model_selection=model_selection,
            exclude_episode_ids=current_episode_ids,
        )
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence,
            selected_episode_rows=selection.episodes,
            selected_memory_rows=[*selection.memories, *selection.reflections],
            topic_selection=topic_selection,
        )
        # Persistence keeps inherited evidence for provenance and future reuse.
        # The tool observation returns only this call's searched evidence.
        observation = copy.deepcopy(retrieval)
        search_units = {
            str(unit_id) for query in selected for unit_id in query.get("unit_ids", [])
        }
        for field in ("recall_memories", "reflection_memories", "episodes"):
            observation[field] = [
                item for item in observation.get(field, [])
                if search_units.intersection(str(value) for value in item.get("unit_ids", []))
            ]
        source_ids = [event.event_id for event in events]
        if record is not None and record["state"] == "recalled":
            # A new query angle is not a new owner request. Only new owner input
            # can revise the original intent/routing. Keep each search separately.
            if record["source_event_ids"] == source_ids:
                query_plan = plan
                plan = copy.deepcopy(record["plan"])
                plan.setdefault("supplemental_queries", []).append(query_plan)
            for field, identity in (
                ("recall_memories", "id"),
                ("episodes", "episode_id"),
            ):
                combined = {item[identity]: item for item in record["retrieval"].get(field, [])}
                combined.update({item[identity]: item for item in retrieval.get(field, [])})
                retrieval[field] = list(combined.values())
            retrieval["effective_recall_queries"] = list(dict.fromkeys([
                *record["retrieval"].get("effective_recall_queries", []),
                *retrieval.get("effective_recall_queries", []),
            ]))
            retrieval["query_recall"] = "\n".join(filter(None, [
                record["retrieval"].get("query_recall", ""),
                f"recall_revision={revision}", retrieval.get("query_recall", ""),
            ]))
        # Do not supersede the previous successful recall before retrieval succeeds.
        saved = self.store.save_context_plan(turn_id, revision, source_ids, plan)
        self.store.save_context_retrieval(
            turn_id, int(saved["revision"]), retrieval, state="recalled"
        )
        return assemble_main_context(
            self.store,
            observation,
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
            if isinstance(query, dict)
        ][:6]
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
                "kind": arguments.get("kind", []),
                "memory_filters": MOMOI_MEMORY_TAGS.filters(arguments.get("memory_filters")),
                "recall_mode": recall_mode,
                "recall_queries": queries if recall_mode == "search" else [],
            },
            "strategy": strategy,
        }
        selected, _reused, _emitted, _skipped = select_plan_recall_queries(plan)
        dense_evidence = None
        if any(item["semantic_expression"] or item["expression"] for item in selected):
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
        selection = await self._select_recall_topics(activity, selected, dense_evidence, topic_selection)
        retrieval = build_plan_retrieval(
            self.store, plan, self.config, dense_evidence=dense_evidence,
            selected_episode_rows=selection.episodes,
            selected_memory_rows=[*selection.memories, *selection.reflections],
            topic_selection=topic_selection,
        )
        return {
            "plan": plan,
            "memory_snapshots": self.memory.snapshots([
                item["id"] for item in retrieval["recall_memories"]
            ]),
            "context": assemble_main_context(
                self.store,
                retrieval,
                self.config.summary_tokens,
            ),
        }
