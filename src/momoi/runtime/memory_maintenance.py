import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..storage import (
    MEMORY_ACTIVATIONS,
    estimate_tokens,
    memory_snapshot_fingerprint,
)


MAINTENANCE_ACTIONS = {"replace", "merge", "retire"}
MEMORY_MAINTENANCE_RUN_VERSION = "v1"

_EVIDENCE_SCHEMA = {
    "type": "object",
    "description": (
        "Exact authenticated owner evidence. Copy both fields verbatim from "
        "<owner_evidence>; never rewrite a channel prefix or paraphrase the quote."
    ),
    "properties": {
        "event_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact event_id from <owner_evidence>. Example: "
                "napcat:2167634556:123456789."
            ),
        },
        "quote": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Exact contiguous substring of that event's content; no paraphrase."
            ),
        },
    },
    "required": ["event_id", "quote"],
    "additionalProperties": False,
}
_FINGERPRINT_SCHEMA = {
    "type": "string",
    "pattern": "^sha256:[0-9a-f]{64}$",
    "description": (
        "Copy the supplied snapshot_fingerprint verbatim, including the sha256: "
        "prefix. Example: sha256:0123456789abcdef... (64 lowercase hex digits)."
    ),
}

MEMORY_MAINTENANCE_FINISH_SPEC: dict[str, object] = {
    "name": "memory_maintenance_finish",
    "description": (
        "Submit the only terminal result for one confirmed-memory maintenance "
        "batch. Every mutable id must appear exactly once: in reviewed_ids when "
        "unchanged, as a target of one change, or in regroup anchor_ids. Changed or "
        "deferred ids must not also appear in reviewed_ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "version": {
                "type": "integer",
                "enum": [1],
                "description": "Protocol version; always 1.",
            },
            "reviewed_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 1},
                "description": (
                    "Mutable memory ids kept exactly unchanged. Do not include ids "
                    "targeted by replace/merge/retire or deferred by regroup. "
                    "Example: [3, 4, 9]."
                ),
            },
            "changes": {
                "type": "array",
                "description": (
                    "Validated state changes. Use replace for one row, merge for true "
                    "duplicates or facets of one concrete temporary event, and retire "
                    "only for explicit owner revocation. Example: [] when no changes."
                ),
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "description": (
                                "Replace one mutable row when correcting a fact, "
                                "removing turn-dependent wording, or moving it to the "
                                "right activation. `evidence` may be null only when "
                                "facts are unchanged (wording/classification only). "
                                "Example: replace memory 64 from always to recall with "
                                "the same content and evidence=null."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["replace"],
                                    "description": "Literal discriminator: replace.",
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "One id from <mutable_memories>; omit it from "
                                        "reviewed_ids."
                                    ),
                                },
                                "snapshot_fingerprint": {
                                    **_FINGERPRINT_SCHEMA,
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        "Complete final content for the surviving row. "
                                        "Do not broaden the supported facts."
                                    ),
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recent", "recall"],
                                    "description": (
                                        "Complete final activation. Never promote a "
                                        "non-always row to always. Topic-scoped game, "
                                        "device, tool and procedure rules are recall."
                                    ),
                                },
                                "expires_at": {
                                    "type": ["number", "null"],
                                    "description": (
                                        "Unix timestamp only for recent; otherwise "
                                        "null. Never invent an expiry."
                                    ),
                                },
                                "evidence": {
                                    "oneOf": [
                                        _EVIDENCE_SCHEMA,
                                        {"type": "null"},
                                    ],
                                    "description": (
                                        "Required exact owner evidence for any factual "
                                        "correction. Use null only if object, scope, "
                                        "conditions, duration and polarity are unchanged."
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        "Short audit reason explaining what changed "
                                        "and why."
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "memory_id",
                                "snapshot_fingerprint",
                                "content",
                                "activation",
                                "expires_at",
                                "evidence",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": (
                                "Merge two or more mutable rows that are true "
                                "duplicates or facets of one concrete temporary event. "
                                "Keep one survivor and absorb every source. Example: "
                                "survivor_id=194, source_ids=[195,196], activation="
                                "recent, expires_at=the latest source expiry."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["merge"],
                                    "description": "Literal discriminator: merge.",
                                },
                                "survivor_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "Mutable id to keep. It must not occur in "
                                        "source_ids or reviewed_ids."
                                    ),
                                },
                                "source_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "integer", "minimum": 1},
                                    "description": (
                                        "Other mutable ids absorbed by survivor_id. "
                                        "Each source is retired through superseded_by."
                                    ),
                                },
                                "snapshot_fingerprints": {
                                    "type": "object",
                                    "additionalProperties": {
                                        **_FINGERPRINT_SCHEMA,
                                    },
                                    "description": (
                                        "Exactly one entry for survivor_id and every "
                                        "source_id. Keys are decimal id strings; values "
                                        "are copied verbatim. Example: "
                                        "{\"194\":\"sha256:...\",\"195\":\"sha256:...\"}."
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                    "description": (
                                        "Complete final survivor content. For true "
                                        "duplicates keep overlapping claims; for facets "
                                        "of a temporary event rebuild only from cited "
                                        "owner evidence."
                                    ),
                                },
                                "activation": {
                                    "type": "string",
                                    "enum": ["always", "recent", "recall"],
                                    "description": (
                                        "Complete final activation derived from final "
                                        "content, not inherited. `always` is legal only "
                                        "when every merged row was already always."
                                    ),
                                },
                                "expires_at": {
                                    "type": ["number", "null"],
                                    "description": (
                                        "For a merged recent event use the latest "
                                        "applicable source expiry; otherwise null."
                                    ),
                                },
                                "evidence_event_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {"type": "string", "minLength": 1},
                                    "description": (
                                        "Exact event_id strings from <owner_evidence> "
                                        "supporting final content. Copy prefixes "
                                        "verbatim; never change napcat to qq."
                                    ),
                                },
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        "Short audit reason proving why these rows are "
                                        "one fact/event rather than merely related."
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "survivor_id",
                                "source_ids",
                                "snapshot_fingerprints",
                                "content",
                                "activation",
                                "expires_at",
                                "evidence_event_ids",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": (
                                "Retire one mutable fact only when an exact owner "
                                "quote explicitly revokes or contradicts it. Do not "
                                "retire duplicates (merge them) or expired recent "
                                "rows (runtime purges them)."
                            ),
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["retire"],
                                    "description": "Literal discriminator: retire.",
                                },
                                "memory_id": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "One mutable id to tombstone; omit it from "
                                        "reviewed_ids."
                                    ),
                                },
                                "snapshot_fingerprint": {
                                    **_FINGERPRINT_SCHEMA,
                                },
                                "evidence": _EVIDENCE_SCHEMA,
                                "reason": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 400,
                                    "description": (
                                        "Short audit reason naming the explicit owner "
                                        "revocation."
                                    ),
                                },
                            },
                            "required": [
                                "action",
                                "memory_id",
                                "snapshot_fingerprint",
                                "evidence",
                                "reason",
                            ],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "regroup_requests": {
                "type": "array",
                "description": (
                    "Use only when a mutable anchor needs a related id that is "
                    "currently read-only in context/directory. Do not regroup ids "
                    "already mutable; decide them now. Example: anchor_ids=[101], "
                    "include_ids=[114]."
                ),
                "items": {
                    "type": "object",
                    "description": (
                        "Defer mutable anchors until the listed external ids can be "
                        "promoted into one later mutable group."
                    ),
                    "properties": {
                        "anchor_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                "Mutable ids that must wait; omit them from "
                                "reviewed_ids and changes."
                            ),
                        },
                        "include_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                            "description": (
                                "Related ids found only in context/directory; they "
                                "must not already be mutable."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 400,
                            "description": "Why these ids require one later review group.",
                        },
                    },
                    "required": ["anchor_ids", "include_ids", "reason"],
                    "additionalProperties": False,
                },
            },
            "summary": {
                "type": "string",
                "maxLength": 500,
                "description": (
                    "Private audit summary of keeps, changes and deferrals. It is not "
                    "owner-visible. Example: 'Merged 13 into 39; kept 15 unchanged.'"
                ),
            },
        },
        "required": [
            "version",
            "reviewed_ids",
            "changes",
            "regroup_requests",
            "summary",
        ],
        "additionalProperties": False,
    },
}


def memory_maintenance_correction(error: str) -> str:
    return "Fix this exact validation error and resubmit the complete batch: " + error


def _normalized_content(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


_GENERIC_KEY_TERMS = {
    "owner",
    "profile",
    "preference",
    "relationship",
    "shared",
    "episodic",
    "routine",
    "life",
    "work",
    "emotional",
}


def _key_terms(value: object) -> set[str]:
    return {
        term
        for term in re.split(r"[._-]+", str(value or "").casefold())
        if len(term) >= 2 and term not in _GENERIC_KEY_TERMS
    }


def _key_family(value: object) -> tuple[str, ...]:
    parts = tuple(part for part in str(value or "").casefold().split(".") if part)
    return parts[:3] if len(parts) >= 3 else parts


def _maintenance_terms(value: object) -> set[str]:
    text = str(value or "").casefold()
    terms = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]+", text)
        if len(token) >= 2
    }
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        if len(chunk) <= 3:
            terms.add(chunk)
            continue
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return terms


def select_daily_memory_seed_ids(
    memories: Sequence[Mapping[str, object]],
    owner_evidence: Sequence[Mapping[str, object]],
    changed_memory_ids: Iterable[int],
) -> set[int]:
    active_ids = {
        int(row["id"])
        for row in memories
        if isinstance(row.get("id"), int)
    }
    selected = {
        int(memory_id)
        for memory_id in changed_memory_ids
        if int(memory_id) in active_ids
    }
    evidence_terms: set[str] = set()
    for item in owner_evidence:
        evidence_terms.update(_maintenance_terms(item.get("content")))
    if not evidence_terms:
        return selected
    for row in memories:
        memory_id = row.get("id")
        if not isinstance(memory_id, int):
            continue
        terms = _maintenance_terms(row.get("key")) | _maintenance_terms(
            row.get("content")
        )
        overlap = terms & evidence_terms
        if any(len(term) >= 4 for term in overlap) or len(overlap) >= 1:
            selected.add(memory_id)
    return selected


def filter_owner_evidence_for_memories(
    owner_evidence: Sequence[Mapping[str, object]],
    memories: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    memory_terms: set[str] = set()
    for memory in memories:
        memory_terms.update(_key_terms(memory.get("key")))
        memory_terms.update(_maintenance_terms(memory.get("content")))
        memory_terms.update(_maintenance_terms(memory.get("evidence_quote")))
    if not memory_terms:
        return []
    return [
        item
        for item in owner_evidence
        if memory_terms & _maintenance_terms(item.get("content"))
    ]


def build_atomic_memory_groups(
    memories: Sequence[Mapping[str, object]],
    seed_ids: Iterable[int],
    forced_groups: Sequence[Sequence[int]] = (),
) -> list[list[int]]:
    rows = {
        int(row["id"]): row
        for row in memories
        if isinstance(row.get("id"), int)
    }
    seeds = {int(memory_id) for memory_id in seed_ids if int(memory_id) in rows}
    if not seeds:
        return []

    parent = {memory_id: memory_id for memory_id in seeds}

    def find(memory_id: int) -> int:
        root = memory_id
        while parent[root] != root:
            root = parent[root]
        while parent[memory_id] != memory_id:
            next_id = parent[memory_id]
            parent[memory_id] = root
            memory_id = next_id
        return root

    def add(memory_id: int) -> None:
        if memory_id not in parent:
            parent[memory_id] = memory_id

    def union(left: int, right: int) -> None:
        add(left)
        add(right)
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    identities: dict[tuple[str, str], list[int]] = {}
    contents: dict[str, list[int]] = {}
    families: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    key_term_frequency: dict[str, int] = {}
    for memory_id, row in rows.items():
        kind = str(row.get("kind") or "")
        key = str(row.get("key") or "")
        identities.setdefault(
            (kind, key), []
        ).append(memory_id)
        family = _key_family(key)
        if family:
            families.setdefault((kind, family), []).append(memory_id)
        content = _normalized_content(row.get("content"))
        if content:
            contents.setdefault(content, []).append(memory_id)
        for term in _key_terms(key):
            key_term_frequency[term] = key_term_frequency.get(term, 0) + 1

    max_term_frequency = max(4, len(rows) // 5)

    for seed_id in sorted(seeds):
        row = rows[seed_id]
        identity_matches = identities.get(
            (str(row.get("kind") or ""), str(row.get("key") or "")), []
        )
        family_matches = families.get(
            (
                str(row.get("kind") or ""),
                _key_family(row.get("key")),
            ),
            [],
        )
        content_matches = contents.get(_normalized_content(row.get("content")), [])
        key_terms = {
            term
            for term in _key_terms(row.get("key"))
            if key_term_frequency.get(term, 0) <= max_term_frequency
        }
        key_matches = [
            related_id
            for related_id, related in rows.items()
            if related_id != seed_id
            and str(related.get("kind") or "") == str(row.get("kind") or "")
            and len(
                key_terms
                & {
                    term
                    for term in _key_terms(related.get("key"))
                    if key_term_frequency.get(term, 0) <= max_term_frequency
                }
            )
            >= 2
        ]
        for related_id in {
            *identity_matches,
            *family_matches,
            *content_matches,
            *key_matches,
        }:
            union(seed_id, related_id)
    for group in forced_groups:
        ids = [int(memory_id) for memory_id in group if int(memory_id) in rows]
        if not ids:
            continue
        add(ids[0])
        for memory_id in ids[1:]:
            union(ids[0], memory_id)

    groups: dict[int, list[int]] = {}
    for memory_id in parent:
        groups.setdefault(find(memory_id), []).append(memory_id)
    return sorted(
        (sorted(group) for group in groups.values()),
        key=lambda group: (group[0], len(group)),
    )


def pack_memory_groups(
    groups: Sequence[Sequence[int]],
    memories: Mapping[int, Mapping[str, object]],
    token_budget: int,
    *,
    max_groups: int = 12,
) -> list[list[int]]:
    if token_budget <= 0:
        raise ValueError("token budget must be positive")
    if max_groups <= 0:
        raise ValueError("max_groups must be positive")
    batches: list[list[int]] = []
    current: list[int] = []
    used = 0
    group_count = 0
    for group in groups:
        ids = [int(memory_id) for memory_id in group]
        size = sum(
            estimate_tokens(
                f"{memory_id} {memories[memory_id].get('kind')} "
                f"{memories[memory_id].get('key')} "
                f"{memories[memory_id].get('content')}"
            )
            for memory_id in ids
        )
        if current and (used + size > token_budget or group_count >= max_groups):
            batches.append(current)
            current = []
            used = 0
            group_count = 0
        current.extend(ids)
        used += size
        group_count += 1
    if current:
        batches.append(current)
    return batches


def _memory_block(memory: Mapping[str, object], *, compact: bool = False) -> str:
    content = " ".join(str(memory.get("content") or "").split())
    if compact and len(content) > 240:
        content = content[:237].rstrip() + "..."
    fields = [
        f"memory_id={memory['id']}",
        f"kind={memory.get('kind') or 'unknown'}",
        f"key={memory.get('key') or 'unknown'}",
        f"activation={memory.get('activation') or 'unknown'}",
    ]
    if not compact:
        fields.extend(
            [
                f"snapshot_fingerprint={memory_snapshot_fingerprint(memory)}",
                f"updated_at={memory.get('updated_at') or 'unknown'}",
                f"expires_at={memory.get('expires_at') or 'none'}",
                f"evidence_quote={json.dumps(str(memory.get('evidence_quote') or ''), ensure_ascii=False)}",
            ]
        )
    return " ".join(fields) + "\ncontent=" + content


def render_memory_maintenance_request(
    *,
    mutable_memories: Sequence[Mapping[str, object]],
    context_memories: Sequence[Mapping[str, object]],
    memory_directory: Sequence[Mapping[str, object]],
    owner_evidence: Sequence[Mapping[str, object]],
    topic_context: str = "",
) -> str:
    def section(name: str, body: str) -> str:
        return f"<{name}>\n{body or '(none)'}\n</{name}>"

    evidence = "\n\n".join(
        f"event_id={item.get('event_id')} at={item.get('occurred_at') or 'unknown'}\n"
        f"{item.get('content') or ''}"
        for item in owner_evidence
    )
    return "\n\n".join(
        (
            section(
                "mutable_memories",
                "\n\n".join(_memory_block(item) for item in mutable_memories),
            ),
            section(
                "context_memories",
                "\n\n".join(_memory_block(item) for item in context_memories),
            ),
            section(
                "memory_directory",
                "\n".join(
                    _memory_block(item, compact=True) for item in memory_directory
                ),
            ),
            section("owner_evidence", evidence),
            section("topic_context", topic_context),
        )
    )


def _parse_evidence(
    value: object,
    owner_evidence: Mapping[str, str],
    path: str,
) -> tuple[dict[str, str] | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, f"{path}: expected an evidence object; got {value!r}"
    if set(value) != {"event_id", "quote"}:
        return None, (
            f"{path}: expected exactly event_id and quote; "
            f"got keys {sorted(value)}"
        )
    event_id = value.get("event_id")
    quote = value.get("quote")
    if not isinstance(event_id, str):
        return None, f"{path}.event_id: expected string; got {event_id!r}"
    if not isinstance(quote, str):
        return None, f"{path}.quote: expected string; got {quote!r}"
    if not quote.strip():
        return None, f"{path}.quote: expected non-empty exact owner quote"
    if event_id not in owner_evidence:
        return None, (
            f"{path}.event_id: unknown {event_id!r}; expected one of "
            f"{sorted(owner_evidence)}"
        )
    if quote not in owner_evidence[event_id]:
        return None, (
            f"{path}.quote: {quote!r} is not an exact contiguous substring of "
            f"owner_evidence[{event_id!r}]"
        )
    return {"event_id": event_id, "quote": quote.strip()}, None


def parse_memory_maintenance_result(
    value: object,
    *,
    mutable_memories: Mapping[int, Mapping[str, object]],
    context_ids: set[int],
    directory_ids: set[int],
    owner_evidence: Mapping[str, str],
) -> tuple[dict[str, Any] | None, str | None]:
    expected_keys = {
        "version",
        "reviewed_ids",
        "changes",
        "regroup_requests",
        "summary",
    }
    if not isinstance(value, dict):
        return None, f"result: expected object; got {value!r}"
    if set(value) != expected_keys:
        return None, (
            "result: expected keys "
            f"{sorted(expected_keys)}; got {sorted(value)}"
        )
    if value.get("version") != 1:
        return None, f"version: expected 1; got {value.get('version')!r}"
    summary = value.get("summary")
    reviewed = value.get("reviewed_ids")
    changes = value.get("changes")
    regroup = value.get("regroup_requests")
    if not isinstance(summary, str):
        return None, f"summary: expected string; got {summary!r}"
    if len(summary) > 500:
        return None, f"summary: maximum length is 500; got {len(summary)}"
    if not isinstance(reviewed, list):
        return None, f"reviewed_ids: expected array; got {reviewed!r}"
    if not isinstance(changes, list):
        return None, f"changes: expected array; got {changes!r}"
    if not isinstance(regroup, list):
        return None, f"regroup_requests: expected array; got {regroup!r}"

    mutable_ids = set(mutable_memories)
    reviewed_ids: set[int] = set()
    for index, memory_id in enumerate(reviewed):
        path = f"reviewed_ids[{index}]"
        if isinstance(memory_id, bool):
            return None, f"{path}: expected integer memory id; got boolean"
        if not isinstance(memory_id, int):
            return None, f"{path}: expected integer memory id; got {memory_id!r}"
        if memory_id not in mutable_ids:
            return None, (
                f"{path}: id {memory_id} is not mutable; "
                f"mutable ids are {sorted(mutable_ids)}"
            )
        if memory_id in reviewed_ids:
            return None, f"{path}: duplicate id {memory_id}"
        reviewed_ids.add(memory_id)

    deferred_ids: set[int] = set()
    parsed_regroup: list[dict[str, object]] = []
    for request_index, item in enumerate(regroup):
        path = f"regroup_requests[{request_index}]"
        if not isinstance(item, dict):
            return None, f"{path}: expected object; got {item!r}"
        expected = {"anchor_ids", "include_ids", "reason"}
        if set(item) != expected:
            return None, (
                f"{path}: expected keys {sorted(expected)}; got {sorted(item)}"
            )
        anchors = item.get("anchor_ids")
        includes = item.get("include_ids")
        reason = item.get("reason")
        if not isinstance(anchors, list):
            return None, f"{path}.anchor_ids: expected array; got {anchors!r}"
        if not anchors:
            return None, f"{path}.anchor_ids: expected at least one mutable id"
        if not isinstance(includes, list):
            return None, f"{path}.include_ids: expected array; got {includes!r}"
        if not includes:
            return None, f"{path}.include_ids: expected at least one external id"
        if not isinstance(reason, str):
            return None, f"{path}.reason: expected string; got {reason!r}"
        if not reason.strip():
            return None, f"{path}.reason: expected non-empty string"
        if len(reason) > 400:
            return None, f"{path}.reason: maximum length is 400; got {len(reason)}"
        anchor_set: set[int] = set()
        for index, memory_id in enumerate(anchors):
            item_path = f"{path}.anchor_ids[{index}]"
            if isinstance(memory_id, bool):
                return None, f"{item_path}: expected integer; got boolean"
            if not isinstance(memory_id, int):
                return None, f"{item_path}: expected integer; got {memory_id!r}"
            if memory_id not in mutable_ids:
                return None, (
                    f"{item_path}: id {memory_id} is not mutable; "
                    f"mutable ids are {sorted(mutable_ids)}"
                )
            if memory_id in anchor_set:
                return None, f"{item_path}: duplicate id {memory_id}"
            if memory_id in deferred_ids:
                return None, f"{item_path}: id {memory_id} is already deferred"
            anchor_set.add(memory_id)
        include_set: set[int] = set()
        allowed_includes = directory_ids | context_ids
        for index, memory_id in enumerate(includes):
            item_path = f"{path}.include_ids[{index}]"
            if isinstance(memory_id, bool):
                return None, f"{item_path}: expected integer; got boolean"
            if not isinstance(memory_id, int):
                return None, f"{item_path}: expected integer; got {memory_id!r}"
            if memory_id not in allowed_includes:
                return None, (
                    f"{item_path}: unknown id {memory_id}; available directory/context "
                    f"ids are {sorted(allowed_includes)}"
                )
            if memory_id in mutable_ids:
                return None, (
                    f"{item_path}: id {memory_id} is already mutable; decide it now "
                    "instead of regrouping"
                )
            if memory_id in include_set:
                return None, f"{item_path}: duplicate id {memory_id}"
            include_set.add(memory_id)
        deferred_ids |= anchor_set
        parsed_regroup.append(
            {
                "anchor_ids": sorted(anchor_set),
                "include_ids": sorted(include_set),
                "reason": reason.strip(),
            }
        )
    parsed_changes: list[dict[str, object]] = []
    changed_ids: set[int] = set()
    for change_index, item in enumerate(changes):
        path = f"changes[{change_index}]"
        if not isinstance(item, dict):
            return None, f"{path}: expected object; got {item!r}"
        action_value = item.get("action")
        if action_value not in MAINTENANCE_ACTIONS:
            return None, (
                f"{path}.action: expected one of {sorted(MAINTENANCE_ACTIONS)}; "
                f"got {action_value!r}"
            )
        action = str(item["action"])
        reason = item.get("reason")
        if not isinstance(reason, str):
            return None, f"{path}.reason: expected string; got {reason!r}"
        if not reason.strip():
            return None, f"{path}.reason: expected non-empty string"
        if len(reason) > 400:
            return None, f"{path}.reason: maximum length is 400; got {len(reason)}"
        if action == "replace":
            required = {
                "action",
                "memory_id",
                "snapshot_fingerprint",
                "content",
                "activation",
                "expires_at",
                "evidence",
                "reason",
            }
            memory_id_value = item.get("memory_id")
            if isinstance(memory_id_value, bool):
                return None, f"{path}.memory_id: expected integer; got boolean"
            if not isinstance(memory_id_value, int):
                return None, (
                    f"{path}.memory_id: expected integer; got {memory_id_value!r}"
                )
            target_ids = {memory_id_value}
        elif action == "merge":
            required = {
                "action",
                "survivor_id",
                "source_ids",
                "snapshot_fingerprints",
                "content",
                "activation",
                "expires_at",
                "evidence_event_ids",
                "reason",
            }
            survivor_value = item.get("survivor_id")
            if isinstance(survivor_value, bool):
                return None, f"{path}.survivor_id: expected integer; got boolean"
            if not isinstance(survivor_value, int):
                return None, (
                    f"{path}.survivor_id: expected integer; got {survivor_value!r}"
                )
            source_ids = item.get("source_ids")
            if not isinstance(source_ids, list):
                return None, f"{path}.source_ids: expected array; got {source_ids!r}"
            if not source_ids:
                return None, f"{path}.source_ids: expected at least one source id"
            parsed_sources: list[int] = []
            for source_index, source_id in enumerate(source_ids):
                source_path = f"{path}.source_ids[{source_index}]"
                if isinstance(source_id, bool):
                    return None, f"{source_path}: expected integer; got boolean"
                if not isinstance(source_id, int):
                    return None, f"{source_path}: expected integer; got {source_id!r}"
                if source_id == survivor_value:
                    return None, (
                        f"{source_path}: survivor id {survivor_value} cannot also "
                        "be a source"
                    )
                if source_id in parsed_sources:
                    return None, f"{source_path}: duplicate source id {source_id}"
                parsed_sources.append(source_id)
            target_ids = {survivor_value, *parsed_sources}
        else:
            required = {
                "action",
                "memory_id",
                "snapshot_fingerprint",
                "evidence",
                "reason",
            }
            memory_id_value = item.get("memory_id")
            if isinstance(memory_id_value, bool):
                return None, f"{path}.memory_id: expected integer; got boolean"
            if not isinstance(memory_id_value, int):
                return None, (
                    f"{path}.memory_id: expected integer; got {memory_id_value!r}"
                )
            target_ids = {memory_id_value}
        actual_keys = set(item)
        if actual_keys != required:
            missing = sorted(required - actual_keys)
            extra = sorted(actual_keys - required)
            return None, f"{path}: missing keys {missing}; unexpected keys {extra}"
        for memory_id in sorted(target_ids):
            if memory_id not in mutable_ids:
                return None, (
                    f"{path}: target id {memory_id} is not mutable; "
                    f"mutable ids are {sorted(mutable_ids)}"
                )
            if memory_id in changed_ids:
                return None, f"{path}: target id {memory_id} is changed more than once"
            if memory_id in reviewed_ids:
                return None, (
                    f"{path}: target id {memory_id} also appears in reviewed_ids; "
                    "remove it from reviewed_ids"
                )
            if memory_id in deferred_ids:
                return None, (
                    f"{path}: target id {memory_id} is also deferred for regrouping"
                )
        changed_ids |= target_ids

        if action in {"replace", "merge"}:
            content = item.get("content")
            activation = item.get("activation")
            expires_at = item.get("expires_at")
            if not isinstance(content, str):
                return None, f"{path}.content: expected string; got {content!r}"
            if not content.strip():
                return None, f"{path}.content: expected non-empty string"
            if len(content) > 2000:
                return None, f"{path}.content: maximum length is 2000; got {len(content)}"
            if activation not in MAINTENANCE_ACTIVATIONS:
                return None, (
                    f"{path}.activation: expected one of "
                    f"{sorted(MAINTENANCE_ACTIVATIONS)}; got {activation!r}"
                )
            if isinstance(expires_at, bool):
                return None, f"{path}.expires_at: expected number or null; got boolean"
            if expires_at is not None:
                if not isinstance(expires_at, (int, float)):
                    return None, (
                        f"{path}.expires_at: expected number or null; "
                        f"got {expires_at!r}"
                    )
        if action == "replace":
            memory_id = int(item["memory_id"])
            expected_fingerprint = memory_snapshot_fingerprint(
                mutable_memories[memory_id]
            )
            actual_fingerprint = item.get("snapshot_fingerprint")
            if actual_fingerprint != expected_fingerprint:
                return None, (
                    f"{path}.snapshot_fingerprint: expected "
                    f"{expected_fingerprint!r}; got {actual_fingerprint!r}"
                )
            if item.get("activation") == "always":
                current_activation = mutable_memories[memory_id].get("activation")
                if current_activation != "always":
                    return None, (
                        f"{path}.activation: cannot promote memory {memory_id} from "
                        f"{current_activation!r} to 'always'; use 'recall'"
                    )
            evidence, error = _parse_evidence(
                item.get("evidence"), owner_evidence, f"{path}.evidence"
            )
            if error:
                return None, error
            parsed = dict(item)
            parsed["content"] = str(item["content"]).strip()
            parsed["evidence"] = evidence
        elif action == "merge":
            survivor_id = item.get("survivor_id")
            source_ids = item.get("source_ids")
            evidence_event_ids = item.get("evidence_event_ids")
            fingerprints = item.get("snapshot_fingerprints")
            assert isinstance(survivor_id, int)
            assert isinstance(source_ids, list)
            if not isinstance(fingerprints, dict):
                return None, (
                    f"{path}.snapshot_fingerprints: expected object; "
                    f"got {fingerprints!r}"
                )
            expected_keys = {str(memory_id) for memory_id in target_ids}
            if set(fingerprints) != expected_keys:
                return None, (
                    f"{path}.snapshot_fingerprints: expected keys "
                    f"{sorted(expected_keys)}; got {sorted(fingerprints)}"
                )
            for memory_id in sorted(target_ids):
                fingerprint_path = f"{path}.snapshot_fingerprints.{memory_id}"
                expected_fingerprint = memory_snapshot_fingerprint(
                    mutable_memories[memory_id]
                )
                actual_fingerprint = fingerprints[str(memory_id)]
                if actual_fingerprint != expected_fingerprint:
                    return None, (
                        f"{fingerprint_path}: expected {expected_fingerprint!r}; "
                        f"got {actual_fingerprint!r}"
                    )
            if item.get("activation") == "always":
                for memory_id in sorted(target_ids):
                    current_activation = mutable_memories[memory_id].get("activation")
                    if current_activation != "always":
                        return None, (
                            f"{path}.activation: cannot merge memory {memory_id} "
                            f"from {current_activation!r} into 'always'; use 'recall'"
                        )
            if not isinstance(evidence_event_ids, list):
                return None, (
                    f"{path}.evidence_event_ids: expected array; "
                    f"got {evidence_event_ids!r}"
                )
            if not evidence_event_ids:
                return None, f"{path}.evidence_event_ids: expected at least one id"
            seen_event_ids: set[str] = set()
            for evidence_index, event_id in enumerate(evidence_event_ids):
                evidence_path = f"{path}.evidence_event_ids[{evidence_index}]"
                if not isinstance(event_id, str):
                    return None, f"{evidence_path}: expected string; got {event_id!r}"
                if event_id in seen_event_ids:
                    return None, f"{evidence_path}: duplicate event id {event_id!r}"
                if event_id not in owner_evidence:
                    return None, (
                        f"{evidence_path}: unknown {event_id!r}; expected one of "
                        f"{sorted(owner_evidence)}"
                    )
                seen_event_ids.add(event_id)
            parsed = dict(item)
            parsed["source_ids"] = sorted(source_ids)
            parsed["evidence_event_ids"] = sorted(evidence_event_ids)
            parsed["content"] = str(item["content"]).strip()
        else:
            memory_id = int(item["memory_id"])
            if item.get("snapshot_fingerprint") != memory_snapshot_fingerprint(
                mutable_memories[memory_id]
            ):
                expected_fingerprint = memory_snapshot_fingerprint(
                    mutable_memories[memory_id]
                )
                return None, (
                    f"{path}.snapshot_fingerprint: expected "
                    f"{expected_fingerprint!r}; "
                    f"got {item.get('snapshot_fingerprint')!r}"
                )
            evidence, error = _parse_evidence(
                item.get("evidence"), owner_evidence, f"{path}.evidence"
            )
            if error:
                return None, error
            if evidence is None:
                return None, f"{path}.evidence: retire requires owner evidence"
            parsed = dict(item)
            parsed["evidence"] = evidence
        parsed["reason"] = reason.strip()
        parsed_changes.append(parsed)

    overlap = reviewed_ids & deferred_ids
    if overlap:
        return None, (
            "result coverage: ids appear as both unchanged and deferred: "
            f"{sorted(overlap)}"
        )
    overlap = reviewed_ids & changed_ids
    if overlap:
        return None, (
            "result coverage: ids appear as both unchanged and changed: "
            f"{sorted(overlap)}"
        )
    overlap = deferred_ids & changed_ids
    if overlap:
        return None, (
            "result coverage: ids appear as both deferred and changed: "
            f"{sorted(overlap)}"
        )
    covered_ids = reviewed_ids | deferred_ids | changed_ids
    missing_ids = mutable_ids - covered_ids
    if missing_ids:
        return None, (
            "result coverage: mutable ids have no decision: "
            f"{sorted(missing_ids)}"
        )
    extra_ids = covered_ids - mutable_ids
    if extra_ids:
        return None, f"result coverage: non-mutable ids were decided: {sorted(extra_ids)}"

    return {
        "version": 1,
        "reviewed_ids": sorted(reviewed_ids),
        "completed_ids": sorted(reviewed_ids | changed_ids),
        "changes": parsed_changes,
        "regroup_requests": parsed_regroup,
        "summary": summary.strip(),
    }, None


MAINTENANCE_ACTIVATIONS = set(MEMORY_ACTIVATIONS)
