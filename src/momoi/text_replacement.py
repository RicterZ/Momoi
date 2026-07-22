import re
from collections.abc import Iterable


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


cyber_keyword_pre_hook = TextReplacementHook(
    (
        (r"(?<![a-z])CVEs?(?![a-z])", "C-V-E"),
        (r"(?<![a-z])vulnerab(?:le|ility|ilities)(?![a-z])", "v-u-l-nerable"),
        (r"(?<![a-z])exploit[a-z]*(?![a-z])", "ex-ploit"),
        (r"(?<![a-z])AV女优", "A-V女-优"),
        ("漏洞", "漏-洞"),
        ("后门", "后-门"),
    )
)
