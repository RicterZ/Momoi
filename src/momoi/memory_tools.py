import logging
import re
import time
from datetime import datetime
from typing import Any

from .logging_context import log_event
from .models import (
    IncomingMessage,
    MemoryCandidate,
    MemoryConflictCandidate,
    MemoryForgetCandidate,
    ToolCall,
    TurnDraft,
)
from .storage import (
    MEMORY_ACTIVATIONS,
    MEMORY_KINDS,
    RECENT_MEMORY_MAX_TTL_HOURS,
    RECENT_MEMORY_MIN_TTL_HOURS,
    Store,
    lexical_units,
    truncate_tokens,
)

logger = logging.getLogger(__name__)
_DEFAULT_EPISODE_LOOKBACK_DAYS = 30


def _episode_time_range(
    value: object,
) -> tuple[float | None, float | None, dict[str, object]]:
    now = time.time()
    if value is None:
        after = now - _DEFAULT_EPISODE_LOOKBACK_DAYS * 86400
        return after, None, {
            "kind": "recent",
            "days": _DEFAULT_EPISODE_LOOKBACK_DAYS,
        }
    if not isinstance(value, dict):
        raise ValueError("invalid_time_range")
    kind = value.get("kind")
    if kind == "all" and set(value) == {"kind"}:
        return None, None, {"kind": "all"}
    if kind == "recent" and set(value) <= {"kind", "days"}:
        days = value.get("days", _DEFAULT_EPISODE_LOOKBACK_DAYS)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise ValueError("invalid_time_range")
        return now - days * 86400, None, {"kind": "recent", "days": days}
    if kind == "range" and set(value) <= {"kind", "from", "to"}:
        try:
            after = (
                datetime.fromisoformat(str(value["from"])).timestamp()
                if value.get("from")
                else None
            )
            before = (
                datetime.fromisoformat(str(value["to"])).timestamp()
                if value.get("to")
                else None
            )
        except (ValueError, TypeError):
            raise ValueError("invalid_time_range") from None
        if after is None and before is None or (
            after is not None and before is not None and after >= before
        ):
            raise ValueError("invalid_time_range")
        return after, before, {
            "kind": "range",
            **({"from": str(value["from"])} if after is not None else {}),
            **({"to": str(value["to"])} if before is not None else {}),
        }
    raise ValueError("invalid_time_range")

MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "Search Momoi's committed long-term memory. Use when the user refers "
            "to prior facts, people, preferences, events, or vague earlier context "
            "that is not already present in the supplied context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A concise query using likely entities and concepts.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 6,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "conversation_search",
        "description": (
            "Search conversation episodes when older events or context "
            "are not present in recent messages or durable memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "time_range": {
                    "type": "object",
                    "description": (
                        "Optional search window. Default is the last 30 days. "
                        "Use kind=all only when older history is necessary."
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["recent", "range", "all"],
                        },
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3650,
                        },
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Offset returned as next_cursor.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "conversation_read",
        "description": (
            "Read the archived raw messages covered by one conversation episode "
            "returned by conversation_search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "before_ordinal": {
                    "type": "integer",
                    "minimum": 2,
                    "description": (
                        "For an older page, pass next_before_ordinal from the "
                        "previous result. Omit it for the newest page."
                    ),
                },
                "message_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Read another chunk of one oversized archived message. Use "
                        "the id returned with next_content_offset."
                    ),
                },
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Character offset returned as next_content_offset for the "
                        "same message_id."
                    ),
                },
            },
            "required": ["episode_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_remember",
        "description": (
            "Stage one durable memory explicitly stated by the authenticated user "
            "in the current input. The memory commits atomically only when this turn "
            "finishes successfully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(MEMORY_KINDS),
                    "description": (
                        "Memory category such as episodic, preference, or routine. "
                        "This is not recency; use activation for always, recent, or recall."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": "Stable lowercase dot-separated key; reuse it for corrections.",
                },
                "content": {
                    "type": "string",
                    "description": "Faithful concise canonicalization of the user's statement.",
                },
                "activation": {
                    "type": "string",
                    "enum": sorted(MEMORY_ACTIVATIONS),
                    "description": (
                        "always only for preferences or constraints that affect every Turn; "
                        "recent for a current time-bounded thread or owner state that can "
                        "change autonomous task applicability; recall for everything else."
                    ),
                },
                "ttl_hours": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 168,
                    "description": (
                        "Required. For recent, how long this state should stay active: "
                        "1 to 168 hours, chosen from the content. For always or recall, "
                        "send 0; the value is ignored."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "Exact contiguous quote from one current user message.",
                },
                "importance": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.5,
                },
                "replace_confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "True only when the current user message explicitly confirms "
                        "this value replaces any existing value for the same key."
                    ),
                },
            },
            "required": [
                "kind",
                "key",
                "content",
                "evidence",
                "activation",
                "ttl_hours",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_forget",
        "description": (
            "Forget one committed long-term memory when the authenticated user "
            "explicitly requests it in the current input."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                "key": {"type": "string"},
                "evidence": {
                    "type": "string",
                    "description": "Exact contiguous quote from one current user message.",
                },
            },
            "required": ["kind", "key", "evidence"],
            "additionalProperties": False,
        },
    },
]


MEMORY_TOOL_POLICY = """### Memory tools

- `memory_search` searches committed long-term memory. Call it before saying you
  do not remember when the user refers to an earlier person, fact, preference,
  event, promise, or vague shared context that is not already visible.
- `conversation_search` searches older conversation episodes;
  `conversation_read` retrieves the archived raw messages for a returned episode.
  Search returns compact summaries and evidence locations, not raw message text.
  It defaults to the last 30 days. If the owner clearly refers to older shared
  history and the default search is empty, retry with a longer range or all
  history. Use `conversation_read` only when exact wording, chronology,
  corrections, commitments, or omitted details require raw messages.
- `memory_remember` stages durable memory for this Turn. When the user explicitly
  says to remember something, states a stable preference/relationship/routine,
  or corrects an existing fact, call it before the final reply. Set `activation`
  to `always` only for a rule that should affect every response, `recent` for a
  current bounded thread or a clearly stated owner state that can affect whether
  autonomous work is still applicable, and `recall` by default. `ttl_hours` is
  required: send `0` for `always` or `recall` (ignored). For `recent`, choose 1
  to 168 hours from the content—a momentary pose or this-afternoon state is 1-3
  hours, today-only is about 12-24, a multi-day situation may run several days
  up to 7. Do not store a fleeting remark as `recent` if it will be stale in an
  hour unless you set a matching short TTL.
- In particular, remember clearly stated, time-sensitive owner state such as a
  current situation, travel or schedule change, availability, or physical state
  when it could change a later Goal or Webhook decision, even if it was shared
  casually. Use a stable state key, exact evidence, and a TTL that matches how
  long that state remains true.
- A correction reuses the existing stable key. Set `replace_confirmed=true` only
  when the current user explicitly confirms the replacement. Otherwise a
  different value becomes a pending conflict and the older memory stays active;
  ask the user which value is correct. A later confirmed `memory_remember` call
  for either value resolves the pending conflict.
- Never claim something was remembered unless the tool result is `ok`.
- `memory_forget` requires an explicit current-user request and exact evidence.
  Use it instead of overwriting a memory with an empty or invented value.
- Search may be retried with a better query when the first result is empty.
"""


_MEMORY_ERROR_MESSAGES = {
    "tool_not_allowed": "This memory tool is not available in the current Turn.",
    "query_required": "Provide a non-empty search query.",
    "invalid_episode_id": "episode_id must be a non-empty string.",
    "invalid_before_ordinal": "before_ordinal must be an integer greater than one.",
    "invalid_message_cursor": "message_id and before_ordinal cannot be combined.",
    "message_id_required": "content_offset requires a message_id.",
    "invalid_content_offset": "content_offset must be a non-negative integer.",
    "message_not_found": "The requested archived message was not found.",
    "episode_not_found": "The requested conversation episode was not found.",
    "invalid_time_range": (
        "time_range must be recent with optional days, range with from/to, or all."
    ),
    "invalid_search_cursor": "cursor must be a non-negative integer.",
    "invalid_kind": (
        "kind must be a memory category such as episodic, preference, or routine; "
        "use activation for always, recent, or recall."
    ),
    "invalid_activation": "activation must be always, recent, or recall.",
    "invalid_key": "key must be a lowercase stable identifier using dots or hyphens.",
    "invalid_content": "content must contain between 1 and 2000 characters.",
    "evidence_not_in_current_input": (
        "evidence must be one exact contiguous quote from a current owner message."
    ),
    "invalid_replace_confirmed": "replace_confirmed must be a boolean.",
    "invalid_ttl": "ttl_hours must be within the allowed range for recent memory.",
    "memory_not_found": "The requested committed or staged memory was not found.",
}


def _memory_error(code: str) -> dict[str, object]:
    return {
        "ok": False,
        "error": code,
        "message": _MEMORY_ERROR_MESSAGES[code],
    }


class MemoryTools:
    def __init__(self, store: Store) -> None:
        self.store = store

    def execute(
        self,
        call: ToolCall,
        current_events: list[IncomingMessage],
        draft: TurnDraft,
    ) -> dict[str, Any]:
        try:
            if call.name == "memory_search":
                return self._search(call.arguments, draft)
            if call.name == "conversation_search":
                return self._conversation_search(call.arguments)
            if call.name == "conversation_read":
                return self._conversation_read(call.arguments)
            if call.name == "memory_remember":
                return self._remember(call.arguments, current_events, draft)
            if call.name == "memory_forget":
                return self._forget(call.arguments, current_events, draft)
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

    def _search(self, arguments: dict[str, Any], draft: TurnDraft) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _memory_error("query_required")
        try:
            limit = min(10, max(1, int(arguments.get("limit", 6))))
        except (TypeError, ValueError):
            limit = 6
        results = self.store.search_memories(query, limit)

        query_units = lexical_units(query)
        committed_keys = {(str(item["kind"]), str(item["key"])) for item in results}
        for index, memory in enumerate(draft.memories):
            if len(results) >= limit:
                break
            key = (memory.kind, memory.key)
            if key in committed_keys:
                continue
            if query_units & lexical_units(f"{memory.key} {memory.content}"):
                results.append(
                    {
                        "id": f"draft:{index}",
                        "kind": memory.kind,
                        "key": memory.key,
                        "content": memory.content,
                        "authority": "owner",
                        "state": "staged",
                    }
                )
        return {"ok": True, "count": len(results), "results": results}

    def _conversation_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        try:
            limit = min(10, max(1, int(arguments.get("limit", 5))))
        except (TypeError, ValueError):
            limit = 5
        cursor = arguments.get("cursor", 0)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            return _memory_error("invalid_search_cursor")
        try:
            after, before, window = _episode_time_range(arguments.get("time_range"))
        except ValueError as error:
            return _memory_error(str(error))
        results = self.store.search_episodes(
            query, cursor + limit + 1, after=after, before=before
        )[cursor:]
        has_more = len(results) > limit
        results = results[:limit]
        compact = []
        for episode in results:
            claims = episode.get("working_summary_claims")
            summary = (
                str(episode.get("working_summary") or "")
                if isinstance(claims, list) and claims
                else ""
            )
            compact.append(
                {
                    "id": episode["id"],
                    "status": episode["status"],
                    "title": episode["title"],
                    "created_timestamp": episode.get("created_timestamp"),
                    "last_activity_timestamp": episode.get(
                        "last_activity_timestamp"
                    ),
                    "summary": truncate_tokens(summary, 1200),
                    "summary_quality": (
                        "extractive"
                        if isinstance(claims, list) and claims
                        else "empty"
                    ),
                    "topics": episode["topics"],
                    "entities": episode["entities"],
                    "open_loops": episode["open_loops"],
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

    def _conversation_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
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
        ):
            return _memory_error("invalid_message_cursor")
        if message_id is None and "content_offset" in arguments:
            return _memory_error("message_id_required")
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
            episode_id.strip(), before_ordinal=before_ordinal
        )
        if episode is None:
            return _memory_error("episode_not_found")
        return {"ok": True, "episode": episode}

    def _remember(
        self,
        arguments: dict[str, Any],
        current_events: list[IncomingMessage],
        draft: TurnDraft,
    ) -> dict[str, Any]:
        kind = str(arguments.get("kind") or "").strip()
        key = str(arguments.get("key") or "").strip()
        content = str(arguments.get("content") or "").strip()
        evidence = str(arguments.get("evidence") or "").strip()
        activation = str(arguments.get("activation") or "recall").strip()
        if kind not in MEMORY_KINDS:
            return _memory_error("invalid_kind")
        if activation not in MEMORY_ACTIVATIONS:
            return _memory_error("invalid_activation")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", key):
            return _memory_error("invalid_key")
        if not content or len(content) > 2000:
            return _memory_error("invalid_content")
        if (
            not evidence
            or len(evidence) > 500
            or not any(evidence in event.text for event in current_events)
        ):
            return _memory_error("evidence_not_in_current_input")
        try:
            importance = min(1.0, max(0.0, float(arguments.get("importance", 0.5))))
        except (TypeError, ValueError):
            importance = 0.5
        replace_confirmed = arguments.get("replace_confirmed", False)
        if not isinstance(replace_confirmed, bool):
            return _memory_error("invalid_replace_confirmed")
        raw_ttl = arguments.get("ttl_hours", 0)
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, (int, float)):
            return _memory_error("invalid_ttl")
        if activation == "recent":
            if not (
                RECENT_MEMORY_MIN_TTL_HOURS
                <= float(raw_ttl)
                <= RECENT_MEMORY_MAX_TTL_HOURS
            ):
                return _memory_error("invalid_ttl")
            ttl_hours = float(raw_ttl)
        else:
            ttl_hours = 0

        existing = self.store.active_memory(kind, key)
        if existing and existing["content"] != content and not replace_confirmed:
            candidate = MemoryConflictCandidate(
                kind, key, content, evidence, importance, activation
            )
            draft.memory_conflicts = [
                conflict
                for conflict in draft.memory_conflicts
                if (conflict.kind, conflict.key) != (kind, key)
            ]
            draft.memory_conflicts.append(candidate)
            draft.memories = [
                memory
                for memory in draft.memories
                if (memory.kind, memory.key) != (kind, key)
            ]
            return {
                "ok": True,
                "state": "conflict_pending",
                "existing": {
                    "kind": kind,
                    "key": key,
                    "content": existing["content"],
                },
                "candidate": {"content": content},
            }

        candidate = MemoryCandidate(
            kind,
            key,
            content,
            evidence,
            importance,
            replace_confirmed,
            activation,
            ttl_hours,
        )
        draft.memories = [
            memory
            for memory in draft.memories
            if (memory.kind, memory.key) != (kind, key)
        ]
        draft.memories.append(candidate)
        draft.memory_conflicts = [
            conflict
            for conflict in draft.memory_conflicts
            if (conflict.kind, conflict.key) != (kind, key)
        ]
        draft.forgotten_memories = [
            forgotten
            for forgotten in draft.forgotten_memories
            if (forgotten.kind, forgotten.key) != (kind, key)
        ]
        return {
            "ok": True,
            "state": "staged",
            "memory": {
                "kind": kind,
                "key": key,
                "content": content,
                "activation": activation,
                "ttl_hours": ttl_hours,
            },
        }

    def _forget(
        self,
        arguments: dict[str, Any],
        current_events: list[IncomingMessage],
        draft: TurnDraft,
    ) -> dict[str, Any]:
        kind = str(arguments.get("kind") or "").strip()
        key = str(arguments.get("key") or "").strip()
        evidence = str(arguments.get("evidence") or "").strip()
        if kind not in MEMORY_KINDS:
            return _memory_error("invalid_kind")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", key):
            return _memory_error("invalid_key")
        if (
            not evidence
            or len(evidence) > 500
            or not any(evidence in event.text for event in current_events)
        ):
            return _memory_error("evidence_not_in_current_input")
        if not self.store.has_memory(kind, key) and not any(
            (memory.kind, memory.key) == (kind, key) for memory in draft.memories
        ):
            return _memory_error("memory_not_found")
        draft.memories = [
            memory
            for memory in draft.memories
            if (memory.kind, memory.key) != (kind, key)
        ]
        draft.memory_conflicts = [
            conflict
            for conflict in draft.memory_conflicts
            if (conflict.kind, conflict.key) != (kind, key)
        ]
        draft.forgotten_memories = [
            forgotten
            for forgotten in draft.forgotten_memories
            if (forgotten.kind, forgotten.key) != (kind, key)
        ]
        draft.forgotten_memories.append(MemoryForgetCandidate(kind, key, evidence))
        return {"ok": True, "state": "staged", "memory": {"kind": kind, "key": key}}
