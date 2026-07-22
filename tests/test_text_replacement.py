import unittest

from momoi.text_replacement import cyber_keyword_pre_hook


class TextReplacementTest(unittest.TestCase):
    def test_rephrases_moderation_keywords_without_changing_source(self) -> None:
        source = "CVE vulnerability漏洞exploit，AV女优在超市后门。"

        self.assertEqual(
            cyber_keyword_pre_hook(source),
            "C-V-E v-u-l-nerable漏-洞ex-ploit，A-V女-优在超市后-门。",
        )
        self.assertEqual(source, "CVE vulnerability漏洞exploit，AV女优在超市后门。")


if __name__ == "__main__":
    unittest.main()
