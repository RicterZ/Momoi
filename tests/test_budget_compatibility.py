import json
import unittest

from momoi.runtime.budget import (
    MemoryTextFitter,
    SectionBudgetAllocator,
    TextSizer,
    ToolResultFitter,
)
from momoi.runtime.turn_support import truncate_tool_result_json
from momoi.storage.memory import estimate_tokens, truncate_tokens


class BudgetCompatibilityTests(unittest.TestCase):
    def test_memory_text_boundaries_remain_compatible(self):
        sizer = TextSizer()
        fitter = MemoryTextFitter(sizer)
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens("abcd你"), 2)
        self.assertEqual(sizer.estimate("abcd你"), estimate_tokens("abcd你"))
        self.assertEqual(truncate_tokens("abc", 0), "")
        self.assertEqual(truncate_tokens("abc", 1), "abc")
        self.assertEqual(truncate_tokens("abcd", 1), "abcd")
        self.assertEqual(truncate_tokens("abcde", 1), "abcd")
        self.assertEqual(truncate_tokens("你好", 2), "你好")
        self.assertEqual(fitter.truncate("abcde", 1), truncate_tokens("abcde", 1))
        self.assertEqual(
            fitter.excerpt("前文 关键字 后文" * 20, {"关键字"}, 5),
            "… 关键字",
        )

    def test_tool_result_truncation_preserves_envelope(self):
        value = json.dumps(
            {
                "ok": False,
                "error": "upstream_error",
                "message": "specific reason",
                "provenance": {"source": "mcp"},
                "result": {"content": "x" * 5000},
            }
        )
        rendered = truncate_tool_result_json(value, 1000)
        parsed = json.loads(rendered)
        self.assertEqual(
            {key: parsed[key] for key in ("ok", "error", "message", "provenance")},
            {
                "ok": False,
                "error": "upstream_error",
                "message": "specific reason",
                "provenance": {"source": "mcp"},
            },
        )
        self.assertTrue(parsed["truncated"])
        self.assertGreater(parsed["original_chars"], 5000)
        self.assertEqual(ToolResultFitter().fit(value, 1000), rendered)

    def test_section_allocator_keeps_round_robin_order(self):
        rows = [
            ("u1", [{"id": "a", "text": "aa"}, {"id": "c", "text": "cc"}]),
            ("u2", [{"id": "b", "text": "bb"}, {"id": "d", "text": "dd"}]),
        ]
        selected = SectionBudgetAllocator().select(
            rows,
            lambda row: row["id"],
            lambda row: str(row["text"]),
            dict,
            lambda _target, _source: None,
            4,
            4,
        )
        self.assertEqual([item["id"] for item in selected], ["a", "b", "c", "d"])
        self.assertEqual(
            [item["unit_ids"] for item in selected],
            [["u1"], ["u2"], ["u1"], ["u2"]],
        )


if __name__ == "__main__":
    unittest.main()
