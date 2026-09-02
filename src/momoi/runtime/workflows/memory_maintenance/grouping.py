from collections.abc import Iterable, Mapping, Sequence

from ....storage import estimate_tokens
from .selection import _key_family, _key_terms, _normalized_content


def build_atomic_memory_groups(
    memories: Sequence[Mapping[str, object]],
    seed_ids: Iterable[int],
    forced_groups: Sequence[Sequence[int]] = (),
) -> list[list[int]]:
    rows = {int(row["id"]): row for row in memories if isinstance(row.get("id"), int)}
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
        identities.setdefault((kind, key), []).append(memory_id)
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
