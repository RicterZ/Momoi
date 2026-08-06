import unittest

from momoi.text_replacement import TextReplacementHook


class TextReplacementTest(unittest.TestCase):
    def test_replaces_nested_text_without_mutating_images_or_source(self) -> None:
        hook = TextReplacementHook(((r"marker", "m-a-r-k-e-r"),))
        source = {
            "messages": [
                {"content": "marker"},
                {
                    "content": [
                        {"type": "text", "text": "marker"},
                        {
                            "type": "image",
                            "source": {"url": "https://example.test/image.jpg"},
                        },
                    ]
                },
            ]
        }

        replaced = hook.replace_strings(source)

        self.assertEqual(replaced["messages"][0]["content"], "m-a-r-k-e-r")
        self.assertEqual(replaced["messages"][1]["content"][0]["text"], "m-a-r-k-e-r")
        self.assertEqual(
            replaced["messages"][1]["content"][1],
            source["messages"][1]["content"][1],
        )
        self.assertEqual(source["messages"][0]["content"], "marker")


if __name__ == "__main__":
    unittest.main()
