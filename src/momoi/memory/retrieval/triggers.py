"""Deterministic phrase recall, independent of embeddings and LLM selection."""
from ..metadata import normalize_trigger
from ..text import estimate_tokens


def triggered_memories(repository, text, *, limit, token_budget):
    texts = [text] if isinstance(text, str) else list(text)
    if any(not isinstance(value, str) for value in texts):
        raise ValueError("trigger input must contain only strings")
    if limit <= 0 or token_budget <= 0:
        return []
    texts = [normalize_trigger(value) for value in texts if value.strip()]
    if not texts:
        return []
    matches = []
    for row in repository.search_rows(activation="recall"):
        words = [word for word in row["meta"].get("triggers", [])
                 if word and any(normalize_trigger(word) in value for value in texts)]
        if words:
            matches.append({**row, "matched_triggers": words})
    matches.sort(key=lambda row: (
        max(len(normalize_trigger(word)) for word in row["matched_triggers"]),
        row["importance"], row["updated_at"], row["id"],
    ), reverse=True)
    selected = []
    for row in matches:
        cost = 64 + estimate_tokens(str(row["content"]) + row["key"] + " ".join(row["matched_triggers"]))
        if cost > token_budget:
            continue
        selected.append(row)
        token_budget -= cost
        if len(selected) >= limit:
            break
    return selected
