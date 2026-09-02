import json
import math
from collections.abc import Callable

from ...observability.values import safe_preview


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


class ToolResultFitter:
    def fit(self, value: str, limit: int) -> str:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = {
                "ok": False,
                "error": "tool_result_truncated",
                "message": safe_preview(value, max(100, limit // 2)),
            }
        if not isinstance(parsed, dict):
            parsed = {"ok": True, "value": parsed}
        provenance = parsed.get("provenance")
        if (
            parsed.get("ok") is True
            and isinstance(provenance, dict)
            and provenance.get("tool") == "read_file"
            and isinstance(parsed.get("content"), str)
        ):
            return self._fit_read_file(parsed, value, limit)
        preserved = {
            key: parsed[key]
            for key in (
                "ok",
                "error",
                "message",
                "provenance",
                "path",
                "start_line",
                "end_line",
                "total_lines",
                "sha256",
                "content_offset",
                "next_content_offset",
            )
            if key in parsed
        }
        if "message" in preserved:
            preserved["message"] = safe_preview(
                preserved["message"], max(100, limit // 3)
            )
        omitted = {
            key: item
            for key, item in parsed.items()
            if key not in preserved and key not in {"truncated", "original_chars"}
        }
        preserved.update(
            {
                "truncated": True,
                "original_chars": len(value),
                "content": safe_preview(omitted, max(100, limit // 2)),
            }
        )
        return json.dumps(preserved, ensure_ascii=False, default=str)

    @staticmethod
    def _fit_read_file(parsed: dict[str, object], value: str, limit: int) -> str:
        content = str(parsed["content"])
        content_offset = int(parsed.get("content_offset") or 0)
        start_line = int(parsed.get("start_line") or 1)
        base = {
            key: parsed[key]
            for key in (
                "ok",
                "error",
                "message",
                "provenance",
                "path",
                "start_line",
                "end_line",
                "total_lines",
                "sha256",
                "content_offset",
                "next_content_offset",
            )
            if key in parsed
        }
        base.update({"truncated": True, "original_chars": len(value)})

        def candidate(length: int) -> dict[str, object]:
            visible = content[:length]
            result = {
                **base,
                "content": visible,
                "next_content_offset": content_offset + len(visible),
                "end_line": start_line + visible.count("\n"),
            }
            if not visible or visible.endswith("\n"):
                result["end_line"] = int(result["end_line"]) - 1
            return result

        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            rendered = json.dumps(
                candidate(middle), ensure_ascii=False, default=str
            )
            if len(rendered) <= limit:
                low = middle
            else:
                high = middle - 1
        return json.dumps(candidate(low), ensure_ascii=False, default=str)


class SectionBudgetAllocator:
    def __init__(self, sizer: TextSizer = TextSizer()) -> None:
        self.sizer = sizer

    def select(
        self,
        candidates: list[tuple[str, list[dict[str, object]]]],
        identity: Callable[[dict[str, object]], object],
        render: Callable[[dict[str, object]], str],
        snapshot: Callable[[dict[str, object]], dict[str, object]],
        merge: Callable[[dict[str, object], dict[str, object]], None],
        max_results: int,
        token_budget: int,
    ) -> list[dict[str, object]]:
        selected: dict[object, dict[str, object]] = {}
        used = 0
        rounds = max((len(rows) for _, rows in candidates), default=0)
        for index in range(rounds):
            for unit_id, rows in candidates:
                if index >= len(rows):
                    continue
                row = rows[index]
                key = identity(row)
                existing = selected.get(key)
                if existing is not None:
                    unit_ids = existing["unit_ids"]
                    if unit_id not in unit_ids:
                        unit_ids.append(unit_id)
                    merge(existing, row)
                    continue
                if index > 0 and len(selected) >= max_results:
                    continue
                size = self.sizer.estimate(render(row))
                if used + size > token_budget:
                    continue
                item = snapshot(row)
                item["unit_ids"] = [unit_id]
                selected[key] = item
                used += size
        return list(selected.values())


TEXT_SIZER = TextSizer()
MEMORY_TEXT_FITTER = MemoryTextFitter(TEXT_SIZER)
TOOL_RESULT_FITTER = ToolResultFitter()
SECTION_BUDGET_ALLOCATOR = SectionBudgetAllocator(TEXT_SIZER)
