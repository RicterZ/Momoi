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


class BudgetTests(unittest.TestCase):
    def test_memory_text_boundaries(self):
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

    def test_tool_result_truncation_preserves_file_continuation_metadata(self):
        value = json.dumps(
            {
                "ok": True,
                "provenance": {"source": "builtin", "tool": "read_file"},
                "path": "/workspace/article.txt",
                "start_line": 10,
                "end_line": 30,
                "total_lines": 100,
                "sha256": "abc",
                "content_offset": 500,
                "next_content_offset": 5000,
                "content": "原文" * 5000,
            },
            ensure_ascii=False,
        )
        parsed = json.loads(truncate_tool_result_json(value, 1000))
        for key in (
            "path",
            "start_line",
            "total_lines",
            "sha256",
            "content_offset",
        ):
            self.assertEqual(parsed[key], json.loads(value)[key])
        self.assertEqual(
            parsed["next_content_offset"],
            parsed["content_offset"] + len(parsed["content"]),
        )
        self.assertLess(parsed["next_content_offset"], 5000)
        self.assertLessEqual(
            len(json.dumps(parsed, ensure_ascii=False)),
            1000,
        )

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
