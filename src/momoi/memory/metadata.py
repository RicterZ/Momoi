"""Controlled topic metadata. Catalog IDs and descriptions belong to the caller."""
from collections.abc import Mapping
from types import MappingProxyType
from typing import NotRequired, TypedDict
import unicodedata


class MemoryMeta(TypedDict):
    tags: list[str]
    scope: str
    triggers: NotRequired[list[str]]


class MemoryFilters(TypedDict, total=False):
    tags_any: list[str]
    kinds: list[str]
    scope: str


def validate_scope(scope):
    if not isinstance(scope, str) or len(scope) > 200 or scope != scope.strip() or any(ord(c) < 32 for c in scope):
        raise ValueError("invalid memory scope")
    return scope


def normalize_trigger(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def validate_triggers(value):
    if (not isinstance(value, list) or len(value) > 8
            or any(not isinstance(word, str) or not word.strip() or word != word.strip()
                   or len(word) > 40 or any(ord(c) < 32 for c in word) for word in value)):
        raise ValueError("triggers must be at most eight nonempty phrases of at most 40 characters")
    if len({normalize_trigger(word) for word in value}) != len(value):
        raise ValueError("memory triggers must be distinct")
    return list(value)


def inherited_triggers(records):
    words = {}
    for record in records:
        for word in record.get("meta", {}).get("triggers", []):
            words.setdefault(normalize_trigger(word), word)
    return validate_triggers(list(words.values()))


class TagCatalog:
    def __init__(self, tags: Mapping[str, str] | None = None) -> None:
        values = dict(tags or {})
        if any(not isinstance(key, str) or not key.strip()
               or not isinstance(description, str) or not description.strip()
               for key, description in values.items()):
            raise ValueError("tag IDs and descriptions must be nonempty strings")
        self.tags = MappingProxyType(values)

    @staticmethod
    def _values(values, allowed, name):
        if (not isinstance(values, list)
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(set(values)) != len(values)):
            raise ValueError(f"{name} must be an array of distinct predefined values")
        return sorted(values)

    def validate(self, meta: object) -> MemoryMeta:
        if not isinstance(meta, dict) or set(meta) - {"tags", "scope", "triggers"}:
            raise ValueError("memory meta only accepts tags, scope and triggers")
        tags = self._values(meta.get("tags", []), self.tags, "tags")
        if len(tags) > 3:
            raise ValueError("memory accepts at most three tags")
        result = {"tags": tags, "scope": validate_scope(meta.get("scope", ""))}
        if "triggers" in meta:
            result["triggers"] = validate_triggers(meta["triggers"])
        return result

    def filters(self, filters: object) -> MemoryFilters:
        from .storage.records import MEMORY_KINDS

        if filters is None:
            return {"tags_any": [], "kinds": [], "scope": ""}
        if not isinstance(filters, dict) or set(filters) - {"tags_any", "kinds", "scope"}:
            raise ValueError("memory filters only accept tags_any, kinds and scope")
        return {
            "tags_any": self._values(filters.get("tags_any", []), self.tags, "tags_any"),
            "kinds": self._values(filters.get("kinds", []), MEMORY_KINDS, "kinds"),
            "scope": validate_scope(filters.get("scope", "")),
        }
