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

    def test_rephrases_nested_payload_text_but_not_images(self) -> None:
        source = {
            "messages": [
                {"content": "AV女优在超市后门"},
                {
                    "content": [
                        {"type": "text", "text": "CVE漏洞"},
                        {
                            "type": "image",
                            "source": {"url": "https://example.test/后门.jpg"},
                        },
                    ]
                },
            ]
        }

        replaced = cyber_keyword_pre_hook.replace_strings(source)

        self.assertEqual(replaced["messages"][0]["content"], "A-V女-优在超市后-门")
        self.assertEqual(replaced["messages"][1]["content"][0]["text"], "C-V-E漏-洞")
        self.assertEqual(
            replaced["messages"][1]["content"][1],
            source["messages"][1]["content"][1],
        )
        self.assertEqual(source["messages"][0]["content"], "AV女优在超市后门")


if __name__ == "__main__":
    unittest.main()
