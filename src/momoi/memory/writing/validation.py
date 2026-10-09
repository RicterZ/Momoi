"""Validate memory commands independently of model transport and owner authentication."""
import re
from typing import Any

from ..metadata import TagCatalog
from ..storage.records import MEMORY_ACTIVATIONS, MEMORY_KINDS


ALWAYS_MEMORY_KINDS = {"profile", "preference", "relationship"}


def parse_decisions(
    arguments: dict[str, Any],
    operations: list[dict[str, Any]],
    memories: dict[int, dict[str, Any]],
    evidence: dict[str, str],
    *, tags: TagCatalog,
) -> list[dict[str, Any]]:
    if (
        not isinstance(arguments, dict)
        or set(arguments) != {"decisions"}
        or not isinstance(arguments["decisions"], list)
        or not arguments["decisions"]
    ):
        raise ValueError("decisions must be a nonempty array")
    requests = {item["id"]: item for item in operations}
    resolved: set[str] = set()
    modified: set[int] = set()
    keys: set[tuple[str, str, str]] = set()
    for item in arguments["decisions"]:
        if not isinstance(item, dict):
            raise ValueError("each decision must be an object")
        action = item.get("action")
        expected = {"operation_ids", "action", "reason"}
        if action == "write":
            expected |= {"target_ids", "memory", "evidence"}
        elif action == "metadata":
            expected |= {"target_ids", "meta", "evidence"}
        elif action == "forget" or (action == "noop" and ({"target_ids", "evidence"} & set(item))):
            expected |= {"target_ids", "evidence"}
        elif action not in {"noop", "defer"}:
            raise ValueError("action must be write, metadata, forget, noop, or defer")
        if set(item) != expected:
            raise ValueError(f"{action} requires exactly {sorted(expected)}")
        ids = item["operation_ids"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(x, str) or x not in requests for x in ids)
        ):
            raise ValueError("operation_ids must identify supplied operations")
        if len(set(ids)) != len(ids) or resolved.intersection(ids):
            raise ValueError("each operation must be resolved exactly once")
        resolved.update(ids)
        reason = item["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError(
                "reason must be a nonempty string of at most 500 characters"
            )
        if action == "defer" or (action == "noop" and "target_ids" not in item):
            continue
        targets = item["target_ids"]
        if not isinstance(targets, list) or any(
            type(x) is not int or x not in memories for x in targets
        ):
            raise ValueError("target_ids must identify supplied current memories")
        if len(set(targets)) != len(targets) or modified.intersection(targets):
            raise ValueError("combine decisions that modify the same memory")
        modified.update(targets)
        target_keys = {
            (memories[target].get("meta", {}).get("scope", ""), memories[target]["kind"], memories[target]["key"]) for target in targets
        }
        if keys.intersection(target_keys):
            raise ValueError("combine decisions that modify the same kind/key")
        keys.update(target_keys)
        if action in {"forget", "metadata", "noop"} and not targets:
            raise ValueError(f"{action} requires at least one target")
        if action == "write" and all(requests[x]["type"] == "forget" for x in ids):
            raise ValueError("a forget request cannot create a memory")
        citations = item["evidence"]
        if not isinstance(citations, list) or not citations:
            raise ValueError("changes require exact owner evidence")
        cited: set[str] = set()
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"event_id", "quote"}:
                raise ValueError("evidence requires event_id and quote")
            event_id, quote = citation["event_id"], citation["quote"]
            if (
                not isinstance(event_id, str)
                or event_id not in evidence
                or not isinstance(quote, str)
                or not quote.strip()
                or quote not in evidence[event_id]
            ):
                raise ValueError(
                    "evidence must be an exact quote from a supplied owner event"
                )
            cited.add(event_id)
        if not {requests[x]["event_id"] for x in ids} <= cited:
            raise ValueError("cite the owner events supporting every resolved request")
        if action == "metadata":
            tags.validate(item["meta"])
            if "scope" in item["meta"] and any(
                item["meta"]["scope"] != memories[target].get("meta", {}).get("scope", "") for target in targets
            ):
                raise ValueError("metadata cannot change memory scope")
        if action != "write":
            continue
        memory = item["memory"]
        if not isinstance(memory, dict) or set(memory) - {"meta"} != {
            "kind",
            "key",
            "content",
            "activation",
            "expires_at",
        }:
            raise ValueError("write requires complete memory fields")
        if "meta" in memory:
            tags.validate(memory["meta"])
        if (
            memory["kind"] not in MEMORY_KINDS
            or memory["activation"] not in MEMORY_ACTIVATIONS
        ):
            raise ValueError("invalid memory kind or activation")
        if (
            memory["activation"] == "always"
            and memory["kind"] not in ALWAYS_MEMORY_KINDS
        ):
            raise ValueError(
                "always memory requires profile, preference, or relationship kind"
            )
        if not isinstance(memory["key"], str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,199}", memory["key"]
        ):
            raise ValueError("invalid memory key")
        scope = tags.validate(memory.get("meta", {}))["scope"]
        if (memory["activation"] == "scoped") != bool(scope):
            raise ValueError("scoped memory requires meta.scope; global memory requires empty scope")
        key = (scope, memory["kind"], memory["key"])
        if key in keys - target_keys:
            raise ValueError("combine writes to the same kind/key")
        keys.add(key)
        if (
            not isinstance(memory["content"], str)
            or not memory["content"].strip()
            or len(memory["content"]) > 2000
        ):
            raise ValueError("invalid memory content")
        if memory["expires_at"] is not None:
            raise ValueError("memories do not expire; temporary state belongs to current state")
    if resolved != set(requests):
        raise ValueError("resolve every operation exactly once")
    return arguments["decisions"]
