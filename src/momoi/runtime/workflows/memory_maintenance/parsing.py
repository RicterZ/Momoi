from collections.abc import Mapping
from typing import Any

from ....storage import MEMORY_ACTIVATIONS, memory_snapshot_fingerprint
from .contracts import MAINTENANCE_ACTIONS


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
            f"{path}: expected exactly event_id and quote; got keys {sorted(value)}"
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
            f"result: expected keys {sorted(expected_keys)}; got {sorted(value)}"
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
                return (
                    None,
                    f"{path}.content: maximum length is 2000; got {len(content)}",
                )
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
            f"result coverage: mutable ids have no decision: {sorted(missing_ids)}"
        )
    extra_ids = covered_ids - mutable_ids
    if extra_ids:
        return (
            None,
            f"result coverage: non-mutable ids were decided: {sorted(extra_ids)}",
        )

    return {
        "version": 1,
        "reviewed_ids": sorted(reviewed_ids),
        "completed_ids": sorted(reviewed_ids | changed_ids),
        "changes": parsed_changes,
        "regroup_requests": parsed_regroup,
        "summary": summary.strip(),
    }, None


MAINTENANCE_ACTIVATIONS = set(MEMORY_ACTIVATIONS)
