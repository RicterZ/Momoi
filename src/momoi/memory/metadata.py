"""Controlled topic metadata. Catalog IDs and descriptions belong to the caller."""
from collections.abc import Mapping
from types import MappingProxyType
from typing import NotRequired, TypedDict
import unicodedata


# Model-authored triggers share one contract; storage still accepts legacy lists.
TRIGGER_INPUT_SCHEMA = {
    "type": "array", "maxItems": 2, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 40},
    "description": (
        "触发词的定义：看到用户说这个词，就会想起来的事情。选择用户日常聊天会说、能让你想起这条记忆的字面词或短语，不是记忆分类标签。"
        "通常只选1个；仅当第二个覆盖明显不同的聊天入口时才选2个，不凑数，无合适词可为空。"
        "优先从用户原话选择，结合所提供的附近对话和用户解释判断这个词的实际用法；不能只看记忆摘要。没有附近对话时，不臆造上下文。没有合适的原话表达时，再选自然的日常领域词；不追求细分类别。"
        "允许直接相关的‘游戏’‘吃饭’等领域词，不要求只命中一条记忆；不要选‘小桃’‘老师’等跨话题通用称呼或虚词。"
        "允许自然口语、简称和拟声词，不要求逐字出现在证据中；不堆叠同义词，不推测原因、后果或无关场景。"
        "触发词应稳定：即使具体数值或当前状态变化，仍能让人想起这件事；不因某个词出现在原话中就照搬易变细节。选择所谈事情的稳定入口，例如体重记录用‘体重’，不用‘70kg’。禁止测量值、日期、时刻、数量、金额及其带单位形式，固定专名除外。不用正则。"
        "命中仅提供相关记忆，当前意图仍由上下文判断。"
    ),
}


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
