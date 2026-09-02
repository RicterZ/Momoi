import re
from collections.abc import Iterable, Mapping, Sequence


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
        token for token in re.findall(r"[a-z0-9][a-z0-9_-]+", text) if len(token) >= 2
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
    active_ids = {int(row["id"]) for row in memories if isinstance(row.get("id"), int)}
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
