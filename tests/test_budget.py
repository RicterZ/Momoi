import json
import unittest
import math
from types import SimpleNamespace
from unittest.mock import patch

from momoi.runtime.agent.budget import (
    MemoryTextFitter,
    SectionBudgetAllocator,
    TextSizer,
    ToolResultFitter,
)
from momoi.runtime.turn_support import truncate_tool_result_json
from momoi.memory.text import estimate_tokens, truncate_tokens


class BudgetTests(unittest.TestCase):
    def test_text_sizer_preserves_character_weights(self):
        samples = ("", "a" * 10000, "中文" * 1000, "a你\x00\x7f\x80🙂é\ud800\udfff")
        for text in samples:
            with self.subTest(text_length=len(text)):
                ascii_chars = sum(ord(char) < 128 for char in text)
                expected = max(1, math.ceil(len(text) - ascii_chars + ascii_chars / 4))
                self.assertEqual(estimate_tokens(text), expected)

    def test_disabled_token_budget_skips_estimation_but_enforces_time(self):
        from momoi.runtime.agent.context_window import ContextWindow
        from momoi.runtime.turn_support import TurnBudgetExceeded
        config = SimpleNamespace(turn_max_seconds=0, turn_max_total_tokens=0)
        store = SimpleNamespace(turn_usage=lambda _: {"started_at": 0, "input": 0, "output": 0})
        window = ContextWindow(config, store, None)
        with patch("momoi.runtime.agent.context_window._estimate_context_tokens",
                   side_effect=AssertionError("disabled token budget must not estimate")):
            window.check_budget("turn", [], [], [])
            config.turn_max_seconds = 1
            with self.assertRaises(TurnBudgetExceeded):
                window.check_budget("turn", [], [], [])
        config.turn_max_seconds = 0
        config.turn_max_total_tokens = 1
        with self.assertRaises(TurnBudgetExceeded):
            window.check_budget("turn", [], [], [])

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
