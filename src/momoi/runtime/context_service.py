import json
import logging
import re
import uuid

from ..models import IncomingMessage
from ..storage import MemoryRecallQuery
from .context_assembler import (
    assemble_main_context,
    build_plan_retrieval,
    select_plan_recall_queries,
)

logger = logging.getLogger("momoi.runtime.turns")
# Episode reference syntax the runtime already stores.
_NEW_EPISODE_SLUG = re.compile(r"new:[a-z0-9][a-z0-9_-]{0,39}")

def _episode_candidate_lines(items: list[dict[str, object]]) -> str:
    blocks: list[str] = []
    for episode in items:
        fields = [
            f"id={episode['id']}",
            f"status={episode['status']}",
            f"title={str(episode['title'])[:120]}",
        ]
        summary = str(
            episode.get("narrative_summary")
            or episode.get("working_summary")
            or ""
        ).strip()
        if summary:
            fields.append(f"summary={summary[:240]}")
        topics = episode.get("topics") or []
        if topics:
            fields.append("topics=" + ",".join(str(item) for item in topics[:8]))
        loops = episode.get("open_loops") or []
        if loops:
            fields.append(
                "open_loops=" + ",".join(str(item) for item in loops[:4])
            )
        blocks.append("- " + " ".join(fields))
    return "\n".join(blocks)


def _heartbeat_topic_lines(items: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fields: list[str] = []
        for key, limit in (("title", 120), ("updated_timestamp", 32)):
            value = item.get(key)
            if value not in (None, "", [], {}):
                fields.append(f"{key.removesuffix('_timestamp')}={str(value)[:limit]}")
        summary = str(item.get("summary") or "").strip()
        if summary:
            fields.append(f"summary={summary[:240]}")
        for key in ("topics", "entities", "open_loops"):
            values = item.get(key) or []
            if values:
                fields.append(f"{key}=" + ",".join(str(value) for value in values[:8]))
        if fields:
            lines.append("- " + " ".join(fields))
    return "\n".join(lines)


def _heartbeat_activity_lines(items: list[dict[str, str]]) -> str:
    rendered = "\n".join(
        f"- at={item.get('at') or '?'} activity={str(item.get('text') or '').strip()}"
        for item in items
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    return rendered or "(none)"


def _heartbeat_self_state_lines(value: str) -> str:
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        return value
    if not isinstance(state, dict):
        return str(value)
    lines: list[str] = []
    mood = state.get("mood")
    if isinstance(mood, dict):
        fields = [
            f"state={mood.get('state') or 'unknown'}",
            f"intensity={mood.get('intensity') or 0}",
        ]
        for key in ("cause", "age_minutes", "updated_at"):
            if mood.get(key) not in (None, "", [], {}):
                fields.append(f"{key}={mood[key]}")
        lines.append("mood: " + " ".join(fields))
    activity = state.get("activity")
    if isinstance(activity, dict):
        fields = []
        for key in ("text", "result", "since"):
            value = str(activity.get(key) or "none").replace("\n", " ")
            fields.append(f"{key}={value}")
        lines.append("activity: " + " ".join(fields))
    if state.get("last_heartbeat_at"):
        lines.append(f"last heartbeat: {state['last_heartbeat_at']}")
    return "\n".join(lines) or "(none)"


def _recall_context_lines(
    values: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    for value in values:
        turn_id = str(value.get("turn_id") or "")
        queries = [str(item) for item in value.get("queries") or []]
        if not turn_id or not queries:
            continue
        lines.append(f"turn={turn_id} queries=" + " ; ".join(queries))
    return "\n".join(lines)






class ContextService:
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
        candidate_ids = {
            str(item["id"])
            for item in self.store.list_recent_episode_directory(
                8, exclude_runtime_archives=True
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

    def owner_context_candidates(self, turn_ids: list[str]) -> dict[str, str]:
        """Give the Owner the two catalogs its context decision depends on.

        Continuing an Episode requires seeing which ones are open, and reusing a
        previous recall requires seeing what that recall actually searched for.
        Both were previously visible only to the planning model, which is why
        that model appeared to know something the Owner could not.
        """

        return {
            "candidate_episodes": _episode_candidate_lines(
                self.store.list_recent_episode_directory(
                    8, exclude_runtime_archives=True
                )
            ),
            "recent_recall_context": _recall_context_lines(
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
            "context": assemble_main_context(
                self.store,
                retrieval,
                self.config.summary_tokens,
            ),
        }

