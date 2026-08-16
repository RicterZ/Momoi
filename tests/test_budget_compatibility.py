import json
import unittest

from momoi.runtime.turns import _truncate_tool_result_json
from momoi.storage.memory import estimate_tokens, excerpt_tokens, truncate_tokens


class BudgetCompatibilityTests(unittest.TestCase):
    def test_memory_text_boundaries_remain_compatible(self):
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens("abcd你"), 2)
        self.assertEqual(truncate_tokens("abc", 0), "")
        self.assertEqual(truncate_tokens("abc", 1), "abc")
        self.assertEqual(truncate_tokens("abcd", 1), "abcd")
        self.assertEqual(truncate_tokens("abcde", 1), "abcd")
        self.assertEqual(truncate_tokens("你好", 2), "你好")
        self.assertEqual(
            excerpt_tokens("前文 关键字 后文" * 20, {"关键字"}, 5), "… 关键字"
        )

    def test_tool_result_truncation_preserves_envelope(self):
        rendered = _truncate_tool_result_json(
            json.dumps(
                {
                    "ok": False,
                    "error": "upstream_error",
                    "message": "specific reason",
                    "provenance": {"source": "mcp"},
                    "result": {"content": "x" * 5000},
                }
            ),
            1000,
        )
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


if __name__ == "__main__":
    unittest.main()
