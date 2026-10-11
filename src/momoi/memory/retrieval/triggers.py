"""Deterministic phrase recall, independent of embeddings and LLM selection."""
from ..metadata import normalize_trigger, validate_trigger_pattern
import re
from re import _parser, _compiler, _constants
from functools import lru_cache
from ..text import estimate_tokens


def triggered_memories(repository, text, *, limit, token_budget):
    texts = [text] if isinstance(text, str) else list(text)
    if any(not isinstance(value, str) for value in texts):
        raise ValueError("trigger input must contain only strings")
    if limit <= 0 or token_budget <= 0:
        return []
    texts = [value for value in texts if value.strip()]
    if not texts:
        return []
    matches = []
    for row in repository.search_rows(activation="recall"):
        words = [word for word in row["meta"].get("triggers", [])
                 if word and any(matches_trigger(word, value) for value in texts)]
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


def matches_trigger(pattern, text):
    if pattern == "|":
        return "|" in text
    try:
        validate_trigger_pattern(pattern)
    except ValueError:
        return False
    try:
        if bounded_pattern(pattern).search(text):
            return True
    except re.error:
        # Existing literal triggers may contain unescaped parentheses or brackets.
        return normalize_trigger(pattern) in normalize_trigger(text)
    if not any(char in pattern for char in r".\^$*+?{}[]|()"):
        return normalize_trigger(pattern) in normalize_trigger(text)
    return False


@lru_cache(maxsize=512)
def bounded_pattern(pattern):
    nodes = _parser.parse(pattern, re.IGNORECASE)

    def bound(items):
        for index, (operation, argument) in enumerate(items):
            if operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT, _constants.POSSESSIVE_REPEAT}:
                minimum, maximum, child = argument
                if maximum == _constants.MAXREPEAT:
                    items[index] = (operation, (minimum, max(minimum, 1000), child))
            elif operation == _constants.SUBPATTERN:
                bound(argument[3])
            elif operation == _constants.ATOMIC_GROUP:
                bound(argument)
    bound(nodes)
    return _compiler.compile(nodes, re.IGNORECASE)
