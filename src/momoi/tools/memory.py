import json
import logging
from typing import Any

from .time_range import parse_history_time_range
from ..observability.events import log_event
from ..models import (
    IncomingMessage,
    ToolCall,
    TurnDraft,
)
from ..policies import MemoryPolicy
from ..search import SearchBackend, search_expression
from ..storage import (
    Store,
    MemoryRecallQuery,
    truncate_tokens,
)
from ..storage.episode_ranking import EpisodeRecallQuery
from ..semantic.models import DenseRecallEvidence
from ..semantic.service import SemanticRecallService

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
        role = "OWNER" if claim.get("role") == "user" else "MOMOI"
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
        role = "OWNER" if match.get("role") == "user" else "MOMOI"
        lines.append(
            f"- [{role} ordinal={match.get('ordinal')}] "
            f"{json.dumps(str(match['content']), ensure_ascii=False)}"
        )
    return truncate_tokens("\n".join(lines), _EPISODE_SEARCH_SUMMARY_TOKENS)


_MEMORY_ERROR_MESSAGES = {
    "tool_not_allowed": "This memory tool is not available in the current Turn.",
    "query_required": "Provide a non-empty search query.",
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
        policy: MemoryPolicy = MemoryPolicy(),
        semantic_recall: SemanticRecallService | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.semantic_recall = semantic_recall

    async def execute_async(
        self,
        call: ToolCall,
        current_events: list[IncomingMessage],
        draft: TurnDraft,
    ) -> dict[str, Any]:
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
            return self.execute(call, current_events, draft)
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
    ) -> dict[str, Any]:
        try:
            if call.name == "memory_search":
                return self._search(call.arguments, draft)
            if call.name == "episode_search":
                return self._episode_search(call.arguments)
            if call.name == "episode_read":
                return self._episode_read(call.arguments)
            if call.name == "memory_operation":
                return self._operation(call, current_events, draft)
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
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return _memory_error("invalid_search_cursor")
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
                        }
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
    ) -> dict[str, Any]:
        args = call.arguments
        if set(args) - {"type", "content", "evidence", "target_id"}:
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
