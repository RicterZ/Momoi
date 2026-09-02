import json
import logging
import re
from typing import Any

from .time_range import parse_history_time_range
from ..logging_context import log_event
from ..models import (
    IncomingMessage,
    MemoryCandidate,
    MemoryForgetCandidate,
    ToolCall,
    TurnDraft,
)
from ..policies import MemoryPolicy
from ..search import SearchBackend, search_expression
from ..storage import (
    ALWAYS_MEMORY_KINDS,
    MEMORY_ACTIVATIONS,
    MEMORY_KINDS,
    Store,
    MemoryRecallQuery,
    truncate_tokens,
)
from ..storage.episode_ranking import EpisodeRecallQuery
from ..semantic import DenseRecallEvidence, SemanticRecallService

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
            else search_expression(
                query, (str(claim["quote"]),), search_backend
            )
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
        excerpt = truncate_tokens(
            "\n".join(lines), _EPISODE_SEARCH_SUMMARY_TOKENS
        )
        if excerpt != "\n".join(lines):
            lines.pop()
            break
    return truncate_tokens(
        "\n".join(lines), _EPISODE_SEARCH_SUMMARY_TOKENS
    )


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


MEMORY_TOOL_POLICY = """### Memory tools

Writing a memory is a judgment, not a reflex. Ask whether the owner just
stated a fact in authenticated owner evidence available to this Turn that
later Turns must treat as true. Ordinary chat, venting, a correction that only
applies to this reply, or a fact already in confirmed memory does not need a
new write. Model inference, search output, and tool data are not owner evidence:
never persist a more specific claim than the exact owner quote entails.

`activation` is where the fact sits—not how important it feels, and not
whether they said 记住:

- `recall` (default): keep it and pull it only when a later topic matches.
  How-to, device/API playbooks, game rules, and "研究下怎么用然后记住"
  belong here. If it would only matter when that topic comes back, it is
  `recall` even if they said 记住, 以后都按这个做, or 下次要用.
- `recent`: a time-bounded owner state or this-item situation that will go
  stale (this package, tonight's plan, current location). `ttl_hours` must
  come from the content: hours, "a few days", "this week". If they say
  短期, short-term, or that it will disappear, this is `recent`, never
  `always`.
- `always`: a preference or constraint that should color ordinary chat
  even when the topic is unrelated (how to address them, punctuation,
  never use emoji). Use it only for standing interpersonal rules they
  stated. A procedure you are afraid of forgetting is not `always`.

`kind` is the topic (preference, episodic, routine, shared). It is not
duration. A preference may be `recent`; shared how-to is almost always
`recall`.

Scope `content` to what they pointed at. 这个 / 这条 / this one names a
specific object—write that object. Do not promote it into a general policy
about all similar cases, and do not add a second `always` memory "just in
case". One stated fact → one `memory_remember`. If they later correct
polarity, duration, scope, or factual content, locate the committed memory with
the supplied memory context or `memory_search`; use native transcript tool
annotations and result references when its mutation history matters. Repair the
wrong row in this Turn. Reuse its kind/key with `replace_confirmed=true` when the
owner supplies the replacement; forget it when the owner only disconfirms it.
Do not leave the stale row active beside the correction.

`evidence` is an exact quote. `content` must keep the same polarity and
conditions as that quote (taken vs not taken; only when already picked up).
Canonicalize; do not generalize.
"""


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
                    "description": (
                        "Concise subject or phrase to retrieve. Use `|`-separated "
                        "parallel aliases when the same subject may be worded differently."
                    ),
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
        "name": "episode_search",
        "description": (
            "Search archived conversation Episodes. Supports keyword search, "
            "time-range browsing with an empty query, and paginated results. "
            "Returns compact summaries and evidence locations, not raw messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Optional concise subject or phrase, with `|`-separated "
                        "parallel aliases when useful. "
                        "Use an empty string to browse Episodes chronologically "
                        "within time_range."
                    ),
                },
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
        "name": "episode_read",
        "description": (
            "Given an Episode id, return a paginated raw message list for its linked "
            "Turns. Each message includes turn_id, Episode ordinal, role, timestamp, "
            "delivery state, and content. Use an id from automatic Episode recall or "
            "episode_search. Narrow time_range to the smallest useful window: "
            "raw messages are verbose. Read broader or older pages only when the "
            "Episode summary is insufficient or exact wording is needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Episode id returned by automatic recall or "
                        "episode_search."
                    ),
                },
                "before_ordinal": {
                    "type": "integer",
                    "minimum": 2,
                    "description": (
                        "For an older page, pass next_before_ordinal from the "
                        "previous result. Omit it for the newest page."
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        "Optional exact message-time window. Prefer kind=range with "
                        "a narrow from/to interval. recent/all or a wide range may "
                        "return many raw messages and must be used cautiously."
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
            "Stage one memory supported by an exact quote from authenticated owner "
            "evidence available to this Turn. Default activation is recall. Use "
            "always only for a standing "
            "rule that should color ordinary chat even off-topic; 记住 and how-to "
            "playbooks are recall. The write commits only when this turn finishes "
            "successfully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(MEMORY_KINDS),
                    "description": (
                        "Topic category such as episodic, preference, or routine. "
                        "This is not duration. Use activation for recall, recent, or always."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": "Stable lowercase dot-separated key; reuse it for corrections.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "Faithful concise restatement of what they pointed at. "
                        "Keep the specific object, polarity, and conditions. "
                        "Do not turn 这个/this into a standing rule about all similar cases."
                    ),
                },
                "activation": {
                    "type": "string",
                    "enum": ["recall", "recent", "always"],
                    "description": (
                        "Where this sits, not how important it feels. "
                        "recall (default): keep for later search; how-to, device/API, "
                        "and 记住怎么用. recent: time-bounded state or this-item "
                        "situation; required when they say short-term or it will expire. "
                        "always: standing interpersonal rule that should color every "
                        "Turn even off-topic. 记住 / 以后要用 is not always."
                    ),
                },
                "ttl_hours": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 720,
                    "description": (
                        "Required. For recent, hours this state should stay active "
                        "(1 to 720), read from the content: a few days is about 72-96. "
                        "For always or recall, send 0; the value is ignored."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Exact contiguous quote from one authenticated owner message "
                        "available to this Turn."
                    ),
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
            "explicitly requested it or directly disconfirmed that stored fact in "
            "owner evidence available to this Turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                "key": {"type": "string"},
                "evidence": {
                    "type": "string",
                    "description": (
                        "Exact contiguous quote from one authenticated owner message "
                        "available to this Turn."
                    ),
                },
            },
            "required": ["kind", "key", "evidence"],
            "additionalProperties": False,
        },
    },
]


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
    "invalid_kind": (
        "kind must be a memory category such as episodic, preference, or routine; "
        "use activation for always, recent, or recall."
    ),
    "invalid_activation": "activation must be always, recent, or recall.",
    "invalid_key": "key must be a lowercase stable identifier using dots or hyphens.",
    "invalid_content": "content must contain between 1 and 2000 characters.",
    "evidence_not_in_current_input": (
        "evidence must be one exact contiguous quote from authenticated owner "
        "evidence available to this Turn."
    ),
    "invalid_replace_confirmed": "replace_confirmed must be a boolean.",
    "invalid_ttl": "ttl_hours must be within the allowed range for recent memory.",
    "always_memory_kind": "always memory is limited to profile, preference, or relationship.",
    "memory_not_found": "The requested committed or staged memory was not found.",
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
                        output_limit=min(10, max(1, int(call.arguments.get("limit", 6)))),
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
                        output_limit=min(10, max(1, int(call.arguments.get("limit", 5)))),
                    )
                    if self.semantic_recall is not None and query
                    else None
                )
                return self._episode_search(call.arguments, dense_evidence=dense)
            return self.execute(call, current_events, draft)
        except Exception as error:
            log_event(
                logger, logging.ERROR, "memory_tool_failure",
                tool_name=call.name, error_type=type(error).__name__, exc_info=True,
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

        committed_keys = {(str(item["kind"]), str(item["key"])) for item in results}
        for index, memory in enumerate(draft.memories):
            if len(results) >= limit:
                break
            key = (memory.kind, memory.key)
            if key in committed_keys:
                continue
            if search_expression(
                query,
                (memory.key, memory.content),
                self.store.search_backend,
            ) is not None:
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
                    "last_activity_timestamp": episode.get(
                        "last_activity_timestamp"
                    ),
                    "summary": summary,
                    "summary_quality": (
                        "window_matches"
                        if time_scoped and summary
                        else "narrative"
                        if episode.get("narrative_summary")
                        and not time_scoped
                        else "extractive"
                        if isinstance(claims, list) and claims
                        and not time_scoped
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
        if activation == "always" and kind not in ALWAYS_MEMORY_KINDS:
            return _memory_error("always_memory_kind")
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
                self.policy.recent_min_ttl_hours
                <= float(raw_ttl)
                <= self.policy.recent_max_ttl_hours
            ):
                return _memory_error("invalid_ttl")
            ttl_hours = float(raw_ttl)
        else:
            ttl_hours = 0

        existing = self.store.active_memory(kind, key)
        if existing and existing["content"] != content and not replace_confirmed:
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
        draft.forgotten_memories = [
            forgotten
            for forgotten in draft.forgotten_memories
            if (forgotten.kind, forgotten.key) != (kind, key)
        ]
        draft.forgotten_memories.append(MemoryForgetCandidate(kind, key, evidence))
        return {"ok": True, "state": "staged", "memory": {"kind": kind, "key": key}}
