import hashlib
import json
import logging
from typing import Any

from .time_range import parse_history_time_range
from ..observability.events import log_event
from ..models import (
    IncomingMessage,
    ToolCall,
    TurnDraft,
    speaker_label,
)
from ..storage.core.search import SearchBackend, search_expression
from ..storage import (
    Store,
    MemoryRecallQuery,
    truncate_tokens,
)
from ..storage.episode.episode_ranking import EpisodeRecallQuery
from ..semantic.models import DenseRecallEvidence
from ..semantic.service import SemanticRecallService
from .contracts.memory import MEMORY_TOOL_SPECS
from .validation import validate_tool_arguments

logger = logging.getLogger(__name__)
_EPISODE_SEARCH_SUMMARY_TOKENS = 300


def _episode_claim_excerpt(
    episode: dict[str, object], query: str, search_backend: SearchBackend
) -> str:
    claims = episode.get("working_summary_claims")
    if not isinstance(claims, list):
        return ""
    matched_ids = {
        int(match["id"])
        for match in episode.get("matches", [])
        if isinstance(match, dict) and isinstance(match.get("id"), int)
    }
    ranked = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not str(claim.get("quote") or "").strip():
            continue
        match = (
            None
            if not query.strip()
            else search_expression(query, (str(claim["quote"]),), search_backend)
        )
        ranked.append(
            (
                int(claim.get("message_id") in matched_ids),
                match.score if match else 0.0,
                int(claim.get("role") == "user"),
                index,
                claim,
            )
        )
    if not ranked:
        return ""
    if query.strip():
        ranked.sort(key=lambda item: item[:4], reverse=True)
    else:
        ranked = ranked[-4:]
    lines = []
    for _, _, _, _, claim in ranked:
        role = speaker_label(claim.get("role"))
        lines.append(
            f"- [{role} ordinal={claim.get('ordinal')}] "
            f"{json.dumps(str(claim['quote']), ensure_ascii=False)}"
        )
        excerpt = truncate_tokens("\n".join(lines), _EPISODE_SEARCH_SUMMARY_TOKENS)
        if excerpt != "\n".join(lines):
            lines.pop()
            break
    return truncate_tokens("\n".join(lines), _EPISODE_SEARCH_SUMMARY_TOKENS)


def _episode_match_excerpt(episode: dict[str, object]) -> str:
    lines = []
    for match in episode.get("matches", []):
        if not isinstance(match, dict) or not str(match.get("content") or "").strip():
            continue
        role = speaker_label(match.get("role"))
        lines.append(
            f"- [{role} ordinal={match.get('ordinal')}] "
            f"{json.dumps(str(match['content']), ensure_ascii=False)}"
        )
    return truncate_tokens("\n".join(lines), _EPISODE_SEARCH_SUMMARY_TOKENS)


_MEMORY_ERROR_MESSAGES = {
    "tool_not_allowed": "This memory tool is not available in the current Turn.",
    "query_required": "Provide a non-empty search query.",
    "invalid_execution_page": "execution_cursor must be a non-negative integer and cannot be combined with other cursors or time_range.",
    "turn_id_required": "after_sequence requires turn_id.",
    "invalid_execution_cursor": "turn_id must be a string and after_sequence a non-negative integer.",
    "conflicting_execution_cursor": "Execution cursors cannot be combined with message or time cursors.",
    "episode_turn_not_found": "The requested Turn does not belong to this Episode.",
    "invalid_episode_id": "episode_id must be a non-empty string.",
    "invalid_before_ordinal": "before_ordinal must be an integer greater than one.",
    "invalid_message_cursor": (
        "message_id cannot be combined with before_ordinal or time_range."
    ),
    "message_id_required": "content_offset requires a message_id.",
    "invalid_content_offset": "content_offset must be a non-negative integer.",
    "message_not_found": "The requested archived message was not found.",
    "episode_not_found": "The requested conversation episode was not found.",
    "invalid_time_range": (
        "time_range must be recent with optional days, range with from/to, or all."
    ),
    "invalid_search_cursor": "cursor must be a non-negative integer.",
    "invalid_content": "content must contain between 1 and 2000 characters.",
    "evidence_not_in_current_input": "evidence must be an exact quote from a current authenticated owner message.",
}


def _memory_error(code: str) -> dict[str, object]:
    return {
        "ok": False,
        "error": code,
        "message": _MEMORY_ERROR_MESSAGES[code],
    }


class MemoryTools:
    def __init__(
        self,
        store: Store,
        semantic_recall: SemanticRecallService | None = None,
    ) -> None:
        self.store = store
        self.semantic_recall = semantic_recall

    async def execute_async(
        self,
        call: ToolCall,
        current_events: list[IncomingMessage],
        draft: TurnDraft,
        *, turn_id: str = "",
    ) -> dict[str, Any]:
        spec = next((item for item in MEMORY_TOOL_SPECS if item["name"] == call.name), None)
        if spec:
            normalized, error = validate_tool_arguments(call.name, call.arguments, spec["input_schema"])
            if error:
                return error
            call = ToolCall(call.id, call.name, normalized or {}, call.argument_error)
        try:
            if call.name == "memory_search":
                query = str(call.arguments.get("query") or "").strip()
                dense = (
                    await self.semantic_recall.prepare(
                        [MemoryRecallQuery(query)],
                        include_episode=False,
                        output_limit=min(
                            10, max(1, int(call.arguments.get("limit", 6)))
                        ),
                    )
                    if self.semantic_recall is not None and query
                    else None
                )
                return self._search(call.arguments, draft, dense_evidence=dense)
            if call.name == "episode_search":
                query = str(call.arguments.get("query") or "").strip()
                try:
                    after, before, _window = parse_history_time_range(
                        call.arguments.get("time_range")
                    )
                except ValueError:
                    return self._episode_search(call.arguments)
                dense = (
                    await self.semantic_recall.prepare(
                        [EpisodeRecallQuery(query)],
                        include_memory=False,
                        episode_after=after,
                        episode_before=before,
                        output_limit=min(
                            10, max(1, int(call.arguments.get("limit", 5)))
                        ),
                    )
                    if self.semantic_recall is not None and query
                    else None
                )
                return self._episode_search(call.arguments, dense_evidence=dense)
            return self.execute(call, current_events, draft, turn_id=turn_id)
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "memory_tool_failure",
                tool_name=call.name,
                error_type=type(error).__name__,
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "memory_operation_failed",
                "message": f"Memory operation failed: {type(error).__name__}.",
                "upstream_error_type": type(error).__name__,
            }

    def execute(
        self,
        call: ToolCall,
        current_events: list[IncomingMessage],
        draft: TurnDraft,
        *, turn_id: str = "",
    ) -> dict[str, Any]:
        spec = next((item for item in MEMORY_TOOL_SPECS if item["name"] == call.name), None)
        if spec:
            normalized, error = validate_tool_arguments(call.name, call.arguments, spec["input_schema"])
            if error:
                return error
            call = ToolCall(call.id, call.name, normalized or {}, call.argument_error)
        try:
            if call.name == "memory_search":
                return self._search(call.arguments, draft)
            if call.name == "episode_search":
                return self._episode_search(call.arguments)
            if call.name == "episode_read":
                return self._episode_read(call.arguments)
            if call.name == "memory_operation":
                return self._operation(call, current_events, draft, turn_id=turn_id)
            return _memory_error("tool_not_allowed")
        except Exception as error:
            log_event(
                logger,
                logging.ERROR,
                "memory_tool_failure",
                tool_name=call.name,
                error_type=type(error).__name__,
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "memory_operation_failed",
                "message": f"Memory operation failed: {type(error).__name__}.",
                "upstream_error_type": type(error).__name__,
            }

    def _search(
        self,
        arguments: dict[str, Any],
        draft: TurnDraft,
        *,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _memory_error("query_required")
        try:
            limit = min(10, max(1, int(arguments.get("limit", 6))))
        except (TypeError, ValueError):
            limit = 6
        if dense_evidence is None:
            results = self.store.search_memories(query, limit)
        else:
            results = self.store.rank_recalled_memories(
                [MemoryRecallQuery(query)], limit, dense_evidence=dense_evidence
            )

        ids = [
            int(item["id"])
            for item in results
            if isinstance(item.get("id"), int)
            and item.get("source", "confirmed") == "confirmed"
        ]
        draft.memory_context.update(self.store.memory_snapshots(ids))
        return {"ok": True, "count": len(results), "results": results}

    def _episode_search(
        self,
        arguments: dict[str, Any],
        *,
        dense_evidence: DenseRecallEvidence | None = None,
    ) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        try:
            limit = min(10, max(1, int(arguments.get("limit", 5))))
        except (TypeError, ValueError):
            limit = 5
        cursor = arguments.get("cursor", 0)
        try:
            after, before, window = parse_history_time_range(
                arguments.get("time_range")
            )
        except ValueError as error:
            return _memory_error(str(error))
        results = self.store.search_episodes(
            query,
            limit + 1,
            after=after,
            before=before,
            offset=cursor,
            dense_evidence=dense_evidence,
        )
        has_more = len(results) > limit
        results = results[:limit]
        compact = []
        for episode in results:
            claims = episode.get("working_summary_claims")
            time_scoped = window.get("kind") != "all"
            if time_scoped:
                summary = _episode_match_excerpt(episode)
            else:
                summary = str(episode.get("narrative_summary") or "")
            if not time_scoped and not summary:
                summary = _episode_claim_excerpt(
                    episode, query, self.store.search_backend
                )
            elif summary:
                summary = truncate_tokens(summary, _EPISODE_SEARCH_SUMMARY_TOKENS)
            compact.append(
                {
                    "id": episode["id"],
                    "status": episode["status"],
                    "title": episode["title"],
                    "created_timestamp": episode.get("created_timestamp"),
                    "last_activity_timestamp": episode.get("last_activity_timestamp"),
                    "summary": summary,
                    "summary_quality": (
                        "window_matches"
                        if time_scoped and summary
                        else "narrative"
                        if episode.get("narrative_summary") and not time_scoped
                        else "extractive"
                        if isinstance(claims, list) and claims and not time_scoped
                        else "empty"
                    ),
                    "topics": [] if time_scoped else episode["topics"],
                    "entities": [] if time_scoped else episode["entities"],
                    "open_loops": [] if time_scoped else episode["open_loops"],
                    "matches": [
                        {
                            key: match.get(key)
                            for key in (
                                "id",
                                "turn_id",
                                "ordinal",
                                "role",
                                "delivery_state",
                                "timestamp",
                            )
                        } | ({"quote_targets": match["quote_targets"]} if match.get("quote_targets") else {})
                        for match in episode.get("matches", [])
                        if isinstance(match, dict)
                    ],
                }
            )
        return {
            "ok": True,
            "count": len(compact),
            "time_range": window,
            "next_cursor": cursor + limit if has_more else None,
            "results": compact,
        }

    def _episode_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        episode_id = arguments.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id.strip():
            return _memory_error("invalid_episode_id")
        if "execution_cursor" in arguments:
            cursor = arguments["execution_cursor"]
            if (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
                    or any(key in arguments for key in ("turn_id", "after_sequence", "message_id",
                                                        "content_offset", "before_ordinal", "time_range"))):
                return _memory_error("invalid_execution_page")
            if self.store.episode(episode_id.strip()) is None:
                return _memory_error("episode_not_found")
            from ..storage.episode.execution_evidence import execution_turns
            return {"ok": True, "episode_id": episode_id.strip(),
                    **execution_turns(self.store, episode_id.strip(), limit=10, tool_limit=12,
                                      after_turn_ordinal=cursor)}
        if "after_sequence" in arguments and not arguments.get("turn_id"):
            return _memory_error("turn_id_required")
        if arguments.get("turn_id") is not None:
            if any(key in arguments for key in ("message_id", "content_offset", "before_ordinal", "time_range")):
                return _memory_error("conflicting_execution_cursor")
            from ..storage.episode.execution_evidence import execution_turns
            turn_id = arguments["turn_id"]
            cursor = arguments.get("after_sequence", 0)
            if not isinstance(turn_id, str) or not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                return _memory_error("invalid_execution_cursor")
            if not self.store._db.execute("SELECT 1 FROM episode_turns WHERE episode_id=? AND turn_id=?",
                                          (episode_id.strip(), turn_id)).fetchone():
                return _memory_error("episode_turn_not_found")
            return {"ok": True, "episode_id": episode_id.strip(),
                    **execution_turns(self.store, episode_id.strip(), turn_id=turn_id,
                                      after_sequence=cursor, limit=1, tool_limit=12)}
        before_ordinal = arguments.get("before_ordinal")
        if before_ordinal is not None and (
            isinstance(before_ordinal, bool)
            or not isinstance(before_ordinal, int)
            or before_ordinal < 2
        ):
            return _memory_error("invalid_before_ordinal")
        message_id = arguments.get("message_id")
        content_offset = arguments.get("content_offset", 0)
        if message_id is not None and (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id < 1
            or isinstance(content_offset, bool)
            or not isinstance(content_offset, int)
            or content_offset < 0
            or before_ordinal is not None
            or "time_range" in arguments
        ):
            return _memory_error("invalid_message_cursor")
        if message_id is None and "content_offset" in arguments:
            return _memory_error("message_id_required")
        time_range = arguments.get("time_range")
        if time_range is None:
            after = before = None
            window = None
        else:
            try:
                after, before, window = parse_history_time_range(time_range)
            except ValueError as error:
                return _memory_error(str(error))
        if message_id is not None:
            try:
                message = self.store.conversation_message(
                    episode_id.strip(), message_id, content_offset
                )
            except ValueError:
                return _memory_error("invalid_content_offset")
            if message is None:
                return _memory_error("message_not_found")
            return {"ok": True, "message": message}
        episode = self.store.conversation_episode(
            episode_id.strip(),
            before_ordinal=before_ordinal,
            after=after,
            before=before,
        )
        if episode is None:
            return _memory_error("episode_not_found")
        return {
            "ok": True,
            **({"time_range": window} if window is not None else {}),
            "episode": episode,
        }

    def _operation(
        self,
        call: ToolCall,
        current_events: list[IncomingMessage],
        draft: TurnDraft,
        *, turn_id: str = "",
    ) -> dict[str, Any]:
        args = call.arguments
        if args.get("scope") == "current_state":
            return self._state_operation(call, current_events, turn_id)
        if set(args) - {"type", "content", "evidence", "target_id", "scope"}:
            return {"ok": False, "error": "invalid_memory_operation_fields"}
        if (
            not call.id
            or not isinstance(args.get("type"), str)
            or args["type"] not in {"add", "replace", "forget"}
        ):
            return {"ok": False, "error": "invalid_memory_operation"}
        content, evidence = args.get("content"), args.get("evidence")
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            return _memory_error("invalid_content")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
            return _memory_error("evidence_not_in_current_input")
        event = next(
            (event for event in reversed(current_events) if evidence in event.text),
            None,
        )
        if event is None:
            return _memory_error("evidence_not_in_current_input")
        target_id = args.get("target_id")
        if "target_id" in args and (
            type(target_id) is not int or target_id not in draft.memory_context
        ):
            return {"ok": False, "error": "target_memory_not_in_context"}
        operation = {
            "id": call.id,
            "type": args["type"],
            "content": content.strip(),
            "evidence": evidence,
            "event_id": event.event_id,
            **({"target_id": target_id} if target_id is not None else {}),
        }
        previous = next(
            (item for item in draft.memory_operations if item["id"] == call.id), None
        )
        if previous is not None and previous != operation:
            return {"ok": False, "error": "tool_call_id_conflict"}
        if previous is None:
            draft.memory_operations.append(operation)
        return {
            "ok": True,
            "state": "accepted",
            "operation_id": call.id,
            "message": "Request accepted for private review after this Turn commits. Do not resubmit; the memory change is not effective yet.",
        }

    def _state_operation(self, call, events, turn_id):
        """Apply owner-evidenced state through the existing atomic revision store."""
        args = call.arguments
        action = args["type"]
        if action not in {"replace", "forget"}:
            return {"ok": False, "error": "current_state_add_not_allowed",
                    "message": "Only replace/forget existing states; new states require background maintenance."}
        if (not turn_id or not call.id or "target_id" in args
                or not args.get("subject", "").strip() or not args.get("key")
                or (action == "forget" and "ttl_seconds" in args)
                or (action != "forget" and "ttl_seconds" not in args)):
            return {"ok": False, "error": "invalid_current_state_fields",
                    "message": "Use subject/key, no target_id; replace requires ttl_seconds, forget omits it."}
        evidence = args["evidence"]
        event = next((e for e in reversed(events) if evidence.strip() and evidence in e.text), None)
        if event is None:
            return _memory_error("evidence_not_in_current_input")
        subject, key = args["subject"].strip(), args["key"]
        operation_id = "memory-state:" + hashlib.sha256(f"{turn_id}:{call.id}".encode()).hexdigest()
        manager = self.store.current_state
        snapshot = manager.snapshot()
        old = self.store._db.execute(
            "SELECT request_json FROM current_state_changes WHERE operation_id=?", (operation_id,)
        ).fetchone()
        previous = json.loads(old[0]) if old else None
        slots = [slot for slot in snapshot.slots if (slot.subject, slot.key) == (subject, key)]
        if previous:
            # Rebuild exactly the original request so retries cannot renew TTL or overwrite newer state.
            removed = self.store._db.execute(
                "SELECT removed_json FROM current_state_changes WHERE operation_id=?", (operation_id,)
            ).fetchone()
            dimensions = {(x["subject"], x["key"]) for x in json.loads(removed[0])}
            dimensions.update((x["subject"], x["key"]) for x in previous["add"])
            if (subject, key) not in dimensions:
                return {"ok": False, "error": "tool_call_id_conflict"}
        elif not slots:
            return {"ok": False, "error": "state_not_found"}
        added = [] if action == "forget" else [{
            "subject": subject, "key": key, "value": args["content"].strip(),
            "ttl_seconds": args["ttl_seconds"], "status": "observed",
            "observed_at": event.occurred_at, "evidence_turn_id": turn_id,
            "source_quote": evidence, "source_role": "user", "uncertainty": "",
        }]
        change = {"add": added, "delete": previous["delete"] if previous else [slot.id for slot in slots]}
        try:
            manager.apply_arguments(change, source_turn_id=turn_id, operation_id=operation_id,
                                    expected_revision=previous["expected_revision"] if previous else snapshot.revision)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "operation_id": call.id}
