import re
from collections.abc import Iterable
from typing import Any


class TextReplacementHook:
    def __init__(self, rules: Iterable[tuple[str, str]]) -> None:
        self._rules = tuple(
            (re.compile(pattern, re.IGNORECASE), replacement)
            for pattern, replacement in rules
        )

    def __call__(self, text: str) -> str:
        for pattern, replacement in self._rules:
            text = pattern.sub(replacement, text)
        return text

    def replace_strings(self, value: Any) -> Any:
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.replace_strings(item) for item in value]
        if isinstance(value, dict):
            if value.get("type") in ("image", "image_url"):
                return value
            return {key: self.replace_strings(item) for key, item in value.items()}
        return value


cyber_keyword_pre_hook = TextReplacementHook(
    (
        (r"(?<![a-z])CVEs?(?![a-z])", "C-V-E"),
        (r"(?<![a-z])vulnerab(?:le|ility|ilities)(?![a-z])", "v-u-l-nerable"),
        (r"(?<![a-z])exploit[a-z]*(?![a-z])", "ex-ploit"),
        (r"(?<![a-z])AV女优", "A-V女-优"),
        ("漏洞", "漏-洞"),
        ("后门", "后-门"),
        ("情色", "情-色"),
    )
)
