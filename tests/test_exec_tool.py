import asyncio
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from momoi.config.manager import ConfigurationManager, RevisionConflict
from momoi.config.models import ConfigError
from momoi.config.workspace import bootstrap
from momoi.models import ToolCall
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.tools.builtin import BuiltinTools
from momoi.webhooks.service import WebhookService


class ExecToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tools = BuiltinTools(self.root, exec_enabled=True)

    def call(self, command, **options):
        return ToolCall("test-exec", "exec", {"command": command, **options})

    async def test_disabled_by_default_at_catalog_and_dispatch(self):
        call = self.call("touch unexpected")
        result = await BuiltinTools(self.root).execute(call)
        self.assertEqual(result["error"], "tool_not_allowed")
        self.assertFalse((self.root / "unexpected").exists())
        mcp = SimpleNamespace(tool_specs=[], configs={})
        for enabled in (False, True):
            surface = ToolSurface(mcp, {}, exec_enabled=enabled)
            self.assertEqual("exec" in {item["name"] for item in surface.conversation_specs()}, enabled)
            for stage in ("owner", "heartbeat", "goal"):
                self.assertEqual("exec" in surface.permitted_names(stage), enabled)
            self.assertNotIn("exec", surface.permitted_names("webhook"))
        self.assertEqual(self.tools.capability(call), "external_effect")

    async def test_bash_working_directory_exit_status_and_bounded_output(self):
        result = await self.tools.execute(self.call("printf '%s' \"${BASH_VERSION:+bash}\"; pwd; printf error >&2; exit 7"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("bash", result["stdout_tail"])
        self.assertIn(str(self.root.resolve()), result["stdout_tail"])
        self.assertEqual(result["stderr_tail"], "error")
        result = await self.tools.execute(self.call("cat"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout_tail"], "")
        result = await self.tools.execute(self.call("printf '%020000d' 0"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout_tail"]), 16384)

    async def test_timeout_and_cancellation_stop_children(self):
        for cancel in (False, True):
            marker = self.root / f"survived-{cancel}"
            command = f"(sleep 1; touch {shlex.quote(str(marker))}) & wait"
            task = asyncio.create_task(self.tools.execute(self.call(command, timeout_seconds=0.1 if not cancel else 30)))
            if cancel:
                await asyncio.sleep(0.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                self.assertEqual((await task)["error"], "exec_timeout")
            await asyncio.sleep(1.1)
            self.assertFalse(marker.exists())

    async def test_invalid_timeout_and_empty_command_do_not_execute(self):
        for timeout in (True, "1", 0, 121, float("nan")):
            result = await self.tools.execute(self.call("touch unexpected", timeout_seconds=timeout))
            self.assertFalse(result["ok"])
        self.assertFalse((await self.tools.execute(self.call("")))["ok"])
        self.assertFalse((self.root / "unexpected").exists())

    async def test_timeout_cleans_children_after_shell_leader_exits(self):
        marker = self.root / "orphan-output"
        result = await self.tools.execute(self.call(
            f"(sleep 1; touch {shlex.quote(str(marker))}) & exit 0", timeout_seconds=0.1,
        ))
        self.assertEqual(result["error"], "exec_timeout")
        await asyncio.sleep(1.1)
        self.assertFalse(marker.exists())

    async def test_webhook_exec_keeps_argv_semantics_independent_of_agent_switch(self):
        # Executor commands remain configured argv, without Bash interpolation.
        service = object.__new__(WebhookService)
        step = {"argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch unexpected)"], "env": {}, "timeout_seconds": 5}
        state, result, error = await service._run_exec(step)
        self.assertEqual(state, "succeeded")
        self.assertEqual(result["stdout_tail"].strip(), "$(touch unexpected)")
        self.assertIsNone(error)
        step["argv"] = [sys.executable, "-c", "raise SystemExit(3)"]
        self.assertEqual((await service._run_exec(step))[2], "executor_exit_3")
        step["argv"] = [sys.executable, "-c", "import time; time.sleep(3)"]
        step["timeout_seconds"] = 0.1
        self.assertEqual((await service._run_exec(step))[::2], ("ambiguous", "executor_timeout"))


class ExecConfigTest(unittest.TestCase):
    def test_default_strict_boolean_and_partial_tools_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            bootstrap(path)
            manager = ConfigurationManager(path)
            self.assertFalse(manager.validate().exec_enabled)
            app = manager.read_app()
            app["tools"].update({"result_max_chars": 9999, "result_retention_days": 10})
            path.write_text(json.dumps(app))
            revision = manager.revision()
            result = manager.save_runtime({"tools": {"exec_enabled": True}}, revision)
            self.assertTrue(manager.validate().exec_enabled)
            self.assertEqual(result["app"]["tools"]["mcp_config"], "mcp.json")
            self.assertEqual(result["app"]["tools"]["result_max_chars"], 9999)
            with self.assertRaises(RevisionConflict):
                manager.save_runtime({"tools": {"exec_enabled": False}}, revision)
            for invalid in ("true", 1, None):
                with self.assertRaises(ConfigError):
                    manager.save_runtime({"tools": {"exec_enabled": invalid}}, manager.revision())
                self.assertTrue(manager.validate().exec_enabled)
            manager.save_runtime({"tools": {"exec_enabled": False}}, manager.revision())
            self.assertFalse(manager.validate().exec_enabled)
