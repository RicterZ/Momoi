import math


class TextSizer:
    def estimate(self, text: str) -> int:
        ascii_chars = sum(ord(char) < 128 for char in text)
        return max(1, math.ceil((len(text) - ascii_chars) + ascii_chars / 4))


class MemoryTextFitter:
    def __init__(self, sizer: TextSizer = TextSizer()) -> None:
        self.sizer = sizer

    def truncate(self, text: str, token_budget: int) -> str:
        if token_budget <= 0:
            return ""
        if self.sizer.estimate(text) <= token_budget:
            return text
        marker = "…[truncated]"
        if self.sizer.estimate(marker) > token_budget:
            marker = ""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.sizer.estimate(text[:middle] + marker) <= token_budget:
                low = middle
            else:
                high = middle - 1
        return text[:low] + marker

    def excerpt(self, text: str, terms: set[str], token_budget: int) -> str:
        if token_budget <= 0:
            return ""
        if self.sizer.estimate(text) <= token_budget:
            return text
        folded = text.casefold()
        matches = [
            (folded.find(term.casefold()), term)
            for term in terms
            if term and folded.find(term.casefold()) >= 0
        ]
        if not matches:
            return self.truncate(text, token_budget)
        anchor = max(
            matches,
            key=lambda match: (
                sum(
                    len(term)
                    for position, term in matches
                    if abs(position - match[0]) <= 500
                ),
                len(match[1]),
                -match[0],
            ),
        )[0]
        marker = "…"
        marker_tokens = self.sizer.estimate(marker)
        left_budget = max(0, (token_budget - marker_tokens) // 3)
        left = text[:anchor]
        low, high = 0, len(left)
        while low < high:
            middle = (low + high) // 2
            if self.sizer.estimate(left[middle:]) <= left_budget:
                high = middle
            else:
                low = middle + 1
        prefix = left[low:]
        remaining = max(
            1,
            token_budget
            - self.sizer.estimate(prefix)
            - (marker_tokens if low else 0),
        )
        suffix = self.truncate(text[anchor:], remaining)
        return (marker if low else "") + prefix + suffix


TEXT_SIZER = TextSizer()
MEMORY_TEXT_FITTER = MemoryTextFitter(TEXT_SIZER)


def estimate_tokens(text: str) -> int:
    return TEXT_SIZER.estimate(text)

def truncate_tokens(text: str, token_budget: int) -> str:
    return MEMORY_TEXT_FITTER.truncate(text, token_budget)

def token_chunk(text: str, offset: int, token_budget: int) -> tuple[str, int | None]:
    if token_budget <= 0:
        raise ValueError("token budget must be positive")
    if offset < 0 or offset > len(text):
        raise ValueError("content offset is outside the message")
    remaining = text[offset:]
    if estimate_tokens(remaining) <= token_budget:
        return remaining, None
    marker = "…[continued]"
    if estimate_tokens(marker) >= token_budget:
        marker = ""
    low, high = 0, len(remaining)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(remaining[:middle] + marker) <= token_budget:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        low = 1
    return remaining[:low] + marker, offset + low
