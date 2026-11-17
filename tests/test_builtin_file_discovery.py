import json
import tempfile
import unittest
from pathlib import Path

from momoi.runtime.agent.budget import ToolResultFitter
from momoi.tools.builtin import BuiltinTools


class BuiltinFileDiscoveryTest(unittest.TestCase):
    def test_glob_can_search_absolute_directory_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            (outside / "nested").mkdir(parents=True)
            (outside / "nested" / "a.py").write_text("a")
            (outside / "nested" / "b.py").write_text("b")
            (outside / ".hidden.py").write_text("hidden")
            tools = BuiltinTools(workspace)

            found = tools._glob_files({"path": str(outside), "pattern": "**/*.py", "max_results": 1})
            self.assertEqual(found["count"], 1)
            self.assertTrue(found["truncated"])
            self.assertTrue(found["matches"][0].startswith(str(outside.resolve())))

            hidden = tools._glob_files({"path": str(outside), "pattern": ".*.py", "include_hidden": True})
            self.assertEqual(hidden["matches"], [str((outside / ".hidden.py").resolve())])
            with self.assertRaises(ValueError):
                tools._glob_files({"path": str(outside), "pattern": "../*.py"})

    def test_read_lines_keep_original_offsets_when_result_is_fitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text("first\n" + "x" * 3000 + "\nlast\n")
            tools = BuiltinTools(Path(directory))
            read = tools._read_file({"path": "sample.txt", "start_line": 2})
            self.assertEqual(read["lines"][0]["line"], 2)
            self.assertNotIn("start_line", read)
            self.assertNotIn("end_line", read)
            self.assertEqual(read["content_offset"], 6)
            envelope = {**read, "provenance": {"source": "builtin", "tool": "read_file"}}
            fitted = json.loads(ToolResultFitter().fit(json.dumps(envelope), 800))
            self.assertEqual(fitted["lines"][0]["line"], 2)
            self.assertNotIn("start_line", fitted)
            self.assertNotIn("end_line", fitted)
            self.assertEqual(fitted["next_content_offset"], 6 + len(fitted["lines"][0]["text"]))
            resumed = tools._read_file({"path": "sample.txt", "content_offset": fitted["next_content_offset"]})
            self.assertEqual(fitted["lines"][0]["text"] + resumed["lines"][0]["text"], "x" * 3000 + "\n")


if __name__ == "__main__":
    unittest.main()


def test_planner_delegates_and_worker_owns_external_tools():
    from types import SimpleNamespace
    from momoi.runtime.agent.tool_surface import ToolSurface
    from momoi.runtime.tool_contracts.subagent import PLANNER_SUBAGENT_TOOL_NAMES, WORKER_SUBAGENT_TOOL_NAMES
    surface = ToolSurface(SimpleNamespace(tool_specs=[]), {'onebot': object()},
                          store=SimpleNamespace(transcript_enabled_tools=lambda: ['write_file']))
    planner = {spec['name'] for spec in surface.conversation_specs()}
    worker = {spec['name'] for spec in surface.worker_specs()}
    assert PLANNER_SUBAGENT_TOOL_NAMES <= planner
    assert PLANNER_SUBAGENT_TOOL_NAMES <= surface.permitted_names('owner')
    assert not planner & ({'exec', 'web_fetch', 'skill_load'} | WORKER_SUBAGENT_TOOL_NAMES)
    assert {'read_file', 'write_file'} <= planner
    assert {'web_fetch', 'read_file', 'skill_load'} | WORKER_SUBAGENT_TOOL_NAMES <= worker
    assert 'builtin_files' in surface.discovery_groups('worker')
    assert 'builtin_files' not in surface.discovery_groups()
    assert 'Worker 能力索引' in surface.tool_index()
