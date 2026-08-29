import json
import os
import tempfile
import unittest
from pathlib import Path

from momoi.builtin_tools import BuiltinTools
from momoi.runtime.tool_result_store import ToolResultStore


class ToolResultStoreTest(unittest.TestCase):
    def test_chunks_reconstruct_exact_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolResultStore(Path(directory) / "tool-results")
            content = json.dumps(
                {
                    "items": [
                        {"id": index, "text": f"第{index}条\\n" + "x" * 300}
                        for index in range(20)
                    ]
                },
                ensure_ascii=False,
            )
            result_ref = store.save(content)
            cursor = None
            chunks: list[str] = []
            while True:
                result = store.read(
                    result_ref,
                    cursor,
                    max_chars=1000,
                    provenance={"source": "runtime", "tool": "read_tool_result"},
                )
                self.assertTrue(result["ok"], result)
                self.assertLessEqual(
                    len(json.dumps(result, ensure_ascii=False)), 1000
                )
                chunks.append(result["content"])
                cursor = result["next_cursor"]
                if not result["has_more"]:
                    break
            self.assertEqual("".join(chunks), content)
            self.assertGreater(len(chunks), 1)

    def test_cursor_is_bound_to_result_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolResultStore(Path(directory) / "tool-results")
            first_ref = store.save("a" * 3000)
            second_ref = store.save("b" * 3000)
            first = store.read(
                first_ref,
                None,
                max_chars=1000,
                provenance={"source": "runtime", "tool": "read_tool_result"},
            )
            wrong = store.read(
                second_ref,
                first["next_cursor"],
                max_chars=1000,
                provenance={"source": "runtime", "tool": "read_tool_result"},
            )
            self.assertFalse(wrong["ok"])
            self.assertEqual(wrong["error"], "invalid_tool_result_cursor")

    def test_cleanup_removes_only_expired_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ToolResultStore(
                Path(directory) / "tool-results", retention_days=1
            )
            old_ref = store.save("old")
            current_ref = store.save("current")
            old_path = store.root / f"{old_ref}.json"
            os.utime(old_path, (1, 1))
            self.assertEqual(store.cleanup(now=2 * 24 * 60 * 60), 1)
            self.assertFalse(old_path.exists())
            self.assertTrue((store.root / f"{current_ref}.json").exists())

    def test_builtin_file_tools_cannot_see_private_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "tool-results"
            store = ToolResultStore(private)
            result_ref = store.save("secret")
            tools = BuiltinTools(root, private_roots=(private,))
            listed = tools._list_dir({"path": ".", "include_hidden": True})
            self.assertNotIn(
                "tool-results", [item["name"] for item in listed["entries"]]
            )
            with self.assertRaisesRegex(PermissionError, "runtime-private"):
                tools._read_file({"path": f"tool-results/{result_ref}.json"})


if __name__ == "__main__":
    unittest.main()
