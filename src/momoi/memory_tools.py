import re
from typing import Any

from .models import (
    IncomingMessage,
    MemoryCandidate,
    MemoryConflictCandidate,
    MemoryForgetCandidate,
    ToolCall,
    TurnDraft,
)
from .storage import MEMORY_ACTIVATIONS, MEMORY_KINDS, Store, lexical_units

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
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
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
                        "recent for a current time-bounded thread; recall for everything else."
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
            "required": ["kind", "key", "content", "evidence", "activation"],
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
  Use them when factual memory is insufficient to reconstruct an older episode.
- `memory_remember` stages durable memory for this Turn. When the user explicitly
  says to remember something, states a stable preference/relationship/routine,
  or corrects an existing fact, call it before the final reply. Set `activation`
  to `always` only for a rule that should affect every response, `recent` for a
  current bounded thread, and `recall` by default.
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


class MemoryTools:
    def __init__(self, store: Store) -> None:
        self.store = store

    def execute(
        self,
        call: ToolCall,
        current_events: list[IncomingMessage],
        draft: TurnDraft,
    ) -> dict[str, Any]:
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
        return {"ok": False, "error": "tool_not_allowed"}

    def _search(self, arguments: dict[str, Any], draft: TurnDraft) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query_required"}
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
        if not query:
            return {"ok": False, "error": "query_required"}
        try:
            limit = min(10, max(1, int(arguments.get("limit", 5))))
        except (TypeError, ValueError):
            limit = 5
        results = self.store.search_episodes(query, limit)
        return {"ok": True, "count": len(results), "results": results}

    def _conversation_read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        episode_id = arguments.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id.strip():
            return {"ok": False, "error": "invalid_episode_id"}
        before_ordinal = arguments.get("before_ordinal")
        if before_ordinal is not None and (
            isinstance(before_ordinal, bool)
            or not isinstance(before_ordinal, int)
            or before_ordinal < 2
        ):
            return {"ok": False, "error": "invalid_before_ordinal"}
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
            return {"ok": False, "error": "invalid_message_cursor"}
        if message_id is None and "content_offset" in arguments:
            return {"ok": False, "error": "message_id_required"}
        if message_id is not None:
            try:
                message = self.store.conversation_message(
                    episode_id.strip(), message_id, content_offset
                )
            except ValueError:
                return {"ok": False, "error": "invalid_content_offset"}
            if message is None:
                return {"ok": False, "error": "message_not_found"}
            return {"ok": True, "message": message}
        episode = self.store.conversation_episode(
            episode_id.strip(), before_ordinal=before_ordinal
        )
        if episode is None:
            return {"ok": False, "error": "episode_not_found"}
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
            return {"ok": False, "error": "invalid_kind"}
        if activation not in MEMORY_ACTIVATIONS:
            return {"ok": False, "error": "invalid_activation"}
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", key):
            return {"ok": False, "error": "invalid_key"}
        if not content or len(content) > 2000:
            return {"ok": False, "error": "invalid_content"}
        if (
            not evidence
            or len(evidence) > 500
            or not any(evidence in event.text for event in current_events)
        ):
            return {"ok": False, "error": "evidence_not_in_current_input"}
        try:
            importance = min(1.0, max(0.0, float(arguments.get("importance", 0.5))))
        except (TypeError, ValueError):
            importance = 0.5
        replace_confirmed = arguments.get("replace_confirmed", False)
        if not isinstance(replace_confirmed, bool):
            return {"ok": False, "error": "invalid_replace_confirmed"}

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
            kind, key, content, evidence, importance, replace_confirmed, activation
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
            return {"ok": False, "error": "invalid_kind"}
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", key):
            return {"ok": False, "error": "invalid_key"}
        if (
            not evidence
            or len(evidence) > 500
            or not any(evidence in event.text for event in current_events)
        ):
            return {"ok": False, "error": "evidence_not_in_current_input"}
        if not self.store.has_memory(kind, key) and not any(
            (memory.kind, memory.key) == (kind, key) for memory in draft.memories
        ):
            return {"ok": False, "error": "memory_not_found"}
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
