"""Controlled topic metadata. Catalog IDs and descriptions belong to the caller."""
from collections.abc import Mapping
from types import MappingProxyType
from typing import NotRequired, TypedDict
import unicodedata
import re
from re import _parser, _constants


TRIGGER_SELECTION_DESCRIPTION = (
    "触发词的定义：看到用户说这个词，就会想起来的事情。判断用户下次说什么时需要想起这条记忆，选择这样的日常聊天入口，而不是从记忆正文提取内容关键词或分类标签。"
    "通常只选1个；仅当第二个覆盖明显不同的聊天入口时才选2个，不凑数，无合适词可为空。"
    "优先从用户原话选择，结合所提供的附近对话和用户解释判断这个词的实际用法；不能只看记忆摘要。没有附近对话时，不臆造上下文。原话是理解用法的依据，不是必须照抄的词表。先考虑相关话题刚开始时用户会怎样说，不要等用户复述记忆结论、明确指出问题或使用专业分类名后才能想起。"
    "选择能覆盖相关日常说法的最简入口：细分类别、做法或状态若会漏掉同一话题的普通说法，优先其自然领域词；但不能扩大到无关领域。词脱离原对话后仍应指向适用话题，不用跨领域的抽象词代替具体主题。不要选‘小桃’‘老师’等跨话题通用称呼或虚词。"
    "允许自然口语、简称和拟声词，不要求逐字出现在证据中；不堆叠同义词，不推测原因、后果或无关场景。"
    "触发词应稳定：即使具体数值或当前状态变化，仍能让人想起这件事；不因某个词出现在原话中就照搬易变细节。选择所谈事情的稳定入口，例如体重记录用‘体重’，不用‘70kg’。禁止测量值、日期、时刻、数量、金额及其带单位形式，固定专名除外。"
)


# Model-authored triggers share one contract; storage still accepts legacy lists.
TRIGGER_INPUT_SCHEMA = {
    "type": "array", "maxItems": 2, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 40},
    "examples": [["体重"], ["（$"], ["猫", "狗"], [r"\|$"]],
    "description": (
        TRIGGER_SELECTION_DESCRIPTION
        + "支持 Python 正则搜索（中文和 Unicode），例如‘（$’匹配中文左括号结尾；普通词仍可直接填写。正则元字符作为字面字符时需转义。"
        "不支持正则‘或者’分支：例如猫|狗应拆成数组 [\"猫\",\"狗\"]，因为多个触发词已经表示任一命中。符号 | 可单独填写，或用 \\|、[|] 匹配。禁止匹配空文本或几乎任意文本的宽泛规则，例如 .*、.+、^、$；它们会让无关聊天也注入记忆。"
        "为避免阻塞，只支持一个针对单个字符或字符组的重复；不支持嵌套重复、多个重复、前后查找或反向引用。单个重复最多匹配1000个字符。"
        "命中仅提供相关记忆，当前意图仍由上下文判断。工具名或内部流程名不是日常聊天入口；没有自然入口的执行规则可为空，不为每条记忆强配触发词。"
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


def validate_trigger_pattern(word):
    escaped = False
    in_class = False
    for char in word:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "|" and not in_class and word != "|":
            raise ValueError(f"触发正则 {word!r} 被拒绝：‘或者’分支重复了多个触发词的功能。请拆成多个触发词，例如猫|狗改为 [\"猫\",\"狗\"]；匹配符号 | 请使用 \\| 或 [|]，也可单独填写 |")
    if word == "|":
        return
    try:
        pattern = re.compile(word, re.IGNORECASE)
    except re.error:
        # Preserve existing literal phrases with unescaped punctuation.
        return
    repeats = 0

    def check(nodes):
        nonlocal repeats
        for operation, argument in nodes:
            if operation in {_constants.MAX_REPEAT, _constants.MIN_REPEAT, _constants.POSSESSIVE_REPEAT}:
                repeats += 1
                minimum, maximum, child = argument
                if (repeats > 1 or len(child) != 1
                        or child[0][0] not in {_constants.LITERAL, _constants.NOT_LITERAL, _constants.IN, _constants.ANY}
                        or minimum > 1000 or (maximum != _constants.MAXREPEAT and maximum > 1000)):
                    raise ValueError(f"触发正则 {word!r} 被拒绝：重复结构可能产生大量回溯。只支持一个针对单个字符或字符组的重复，例如 你好\\s*，不支持嵌套重复或多个重复")
            elif operation == _constants.SUBPATTERN:
                check(argument[3])
            elif operation == _constants.ATOMIC_GROUP:
                check(argument)
            elif operation in {_constants.ASSERT, _constants.ASSERT_NOT, _constants.GROUPREF, _constants.GROUPREF_EXISTS, _constants.BRANCH}:
                raise ValueError(f"触发正则 {word!r} 被拒绝：不支持前后查找、反向引用或分支，请改用具体词语和简单字符匹配")

    check(_parser.parse(word, re.IGNORECASE))
    if pattern.search("") is not None:
        raise ValueError(f"触发正则 {word!r} 过于宽泛：它能匹配空文本，任何消息都可能命中并注入记忆。请加入具体词语或字符，例如匹配中文左括号结尾使用 （$，不要使用 .*")
    probes = ("你好", "今天下雨", "hello", "12345", "abc中文", "🌸🐈", "。！？", " \n")
    if sum(pattern.search(text) is not None for text in probes) >= 5:
        raise ValueError(f"触发正则 {word!r} 过于宽泛：它能匹配大量无关文本，会导致无关聊天注入记忆。请限定具体词语、字符或句式，例如 （$ 或 ^你好$，不要使用 .+")


def validate_triggers(value):
    if (not isinstance(value, list) or len(value) > 8
            or any(not isinstance(word, str) or not word.strip() or word != word.strip()
                   or len(word) > 40 or any(ord(c) < 32 for c in word) for word in value)):
        raise ValueError("triggers must be at most eight nonempty phrases of at most 40 characters")
    if len({normalize_trigger(word) for word in value}) != len(value):
        raise ValueError("memory triggers must be distinct")
    for word in value:
        validate_trigger_pattern(word)
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


# Episode triggers recall facts from generated replies, not user-message entry points.
EPISODE_TRIGGER_INPUT_SCHEMA = {
    "type": "array", "maxItems": 2, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 40},
    "description": (
        "话题触发词用于匹配回复器已经生成的正文：当回复提及某个对象时，找回本话题中与该对象相关、可能需要补充的事实、约定或经历。"
        "根据整段话题的事实和最终澄清选择，不猜用户下次会怎么开口，不优先照抄用户最初含糊或已被纠正的叫法。"
        "建议返回空数组。只有存在值得再次想起的具体事实，并且能用明确对象指向它时，才填写最关键的一个词；仅有第二个独立且必要的对象时才填写第二个，最多两个。"
        "选择范围适中的触发词：回复只自然提到这个词时，本话题的关键事实应当仍然相关，而不是仅属于同一大类。"
        "先找出事实或约定究竟依附于哪个对象，再选该对象在日常表达中独立成立的简短名称。"
        "从窄到宽检查：去掉修饰后如果仍指向同一对象，就去掉；如果变成包含许多其他对象的系列、平台或类别，就不能再扩大。"
        "不要把发生的动作、购买状态、介绍性后缀或本次谈论时使用的完整长称一并塞进触发词；但若某个限定确实用于区分不同对象，则必须保留。"
        "对于作品、产品或项目，优先使用作品名、产品名或项目名本身，而不是系列名，也不是带重制版、预购、订单等尾缀的事件称呼；只有本话题的事实必须依赖该限定才能成立时才保留。只因原文一直使用带尾缀的长称，不足以保留尾缀。"
        "提交前检查：回复只说对象的自然简称时能否命中？回复在谈同系列的另一对象时是否会误命中？选择同时尽量避免漏匹配和误匹配的范围。"
        "不是为话题提取关键词或分类标签，不选双方昵称、通用称呼、情绪、泛化动作或顺带介绍的附属内容，不凑数，不扩写同义词和繁简体别名。"
        "触发词不能作为证据补造事实；保留事实中的归属、承诺、纠正和不确定性。"
        "只按普通文本包含匹配，忽略英文大小写和全半角差异，不支持正则表达式。"
    ),
}


def validate_episode_triggers(value):
    if (not isinstance(value, list) or len(value) > 2
            or any(not isinstance(word, str) or not word.strip() or word != word.strip()
                   or len(word) > 40 or any(ord(c) < 32 for c in word) for word in value)):
        raise ValueError("episode triggers must be at most two nonempty phrases of at most 40 characters")
    if len({normalize_trigger(word) for word in value}) != len(value):
        raise ValueError("episode triggers must be distinct")
    return list(value)
