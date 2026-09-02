import json

from .integrity import decode_stored_json
from .memory_values import estimate_tokens, truncate_tokens


REFLECTION_MEMORY_KINDS = {
    "owner_profile",
    "owner_preference",
    "world_knowledge",
    "self_insight",
    "relationship",
    "shared_experience",
    "practice",
    "tool_skill",
}

def _reflection_json(
    value: object,
    fallback: list[object] | dict[str, object],
    *,
    record_id: object,
    field: str,
) -> list[object] | dict[str, object]:
    return decode_stored_json(
        value,
        entity="reflection_material",
        record_id=record_id,
        field=field,
        expected_type=type(fallback),
        fallback=fallback,
    )

def _reflection_compact_value(value: object, limit: int = 240) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return " ".join(str(value or "").split())[:limit]

def _reflection_select_entries(
    entries: list[tuple[float, str, str, bool, bool]], token_budget: int
) -> list[tuple[float, str, str, bool, bool]]:
    """Keep a day-wide shape instead of selecting only the latest records."""
    if not entries or token_budget <= 0:
        return []
    ordered = sorted(entries)
    total = sum(estimate_tokens(f"[{label}]\n{content}") for _, label, content, _, _ in ordered)
    if total <= token_budget:
        return ordered
    selected: list[tuple[float, str, str, bool, bool]] = []
    selected_ids: set[int] = set()
    used = 0

    def add(index: int) -> None:
        nonlocal used
        if index in selected_ids:
            return
        entry = ordered[index]
        size = estimate_tokens(f"[{entry[1]}]\n{entry[2]}")
        if used + size > token_budget:
            return
        selected_ids.add(index)
        selected.append(entry)
        used += size

    head = max(1, len(ordered) // 5)
    tail = max(1, len(ordered) // 2)
    for index in range(head):
        add(index)
    for index in range(max(0, len(ordered) - tail), len(ordered)):
        add(index)
    for index, entry in enumerate(ordered):
        if entry[1] in {"OWNER", "EVENT", "RUNTIME FAILURE"} or entry[1].startswith("TOOL "):
            add(index)
    if not selected:
        entry = ordered[-1]
        selected = [
            (*entry[:2], truncate_tokens(entry[2], max(1, token_budget - 4)), *entry[3:])
        ]
    return sorted(selected)

