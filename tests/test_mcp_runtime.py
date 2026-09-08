import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import atomic_write, bootstrap
from momoi.mcp.manager import MCPManager
from momoi.models import ToolCall
from momoi.runtime.agent.runtime_tools import enable_tools
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.runtime.supervisor import RuntimeSupervisor
from tests.test_dashboard_configuration import FakeDaemon, LLM


class MCPDaemon(FakeDaemon):
    def __init__(self, config):
        super().__init__(config)
        self.mcp = MCPManager(config.mcp_config, servers=config.mcp_servers)
        self.surface = ToolSurface(self.mcp, {})

    async def run(self, stop):
        try:
            async with self.mcp:
                self.ready.set()
                await stop.wait()
        finally:
            self.closed = True


class MCPRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        bootstrap(self.path)
        self.configuration = ConfigurationManager(self.path)
        snapshot = self.configuration.save_binding("llm", LLM, self.configuration.revision())
        self.configuration.save_runtime({"channels": {
            "primary": "napcat", "enabled": {"napcat": {"url": "ws://localhost", "owner_qq": "123"}},
        }}, snapshot["revision"])
        self.runtime = RuntimeSupervisor(self.configuration, factory=MCPDaemon)
        self.addAsyncCleanup(self.runtime._retire)
        self.opened = []
        self.closed = []
        test = self

        class Transport:
            def __init__(self, parameters):
                self.parameters = parameters

            async def __aenter__(self):
                if self.parameters.command == "missing":
                    raise FileNotFoundError("private connection details")
                return self.parameters, object()

            async def __aexit__(self, *_):
                pass

        class Session:
            def __init__(self, parameters, *_, **__):
                self.parameters = parameters
                self.owner = asyncio.current_task()

            async def __aenter__(self):
                test.opened.append(self)
                return self

            async def __aexit__(self, *_):
                test.assertIs(self.owner, asyncio.current_task())
                test.closed.append(self)

            async def initialize(self):
                pass

            async def list_tools(self, _):
                return SimpleNamespace(tools=[SimpleNamespace(
                    name="work", description="Do work", inputSchema={
                        "type": "object", "properties": {"value": {"type": (
                            "integer" if self.parameters.args == ["v2"] else "string"
                        )}},
                    },
                )], nextCursor=None)

            async def call_tool(self, name, arguments):
                test.assertIs(self.owner, asyncio.current_task())
                return SimpleNamespace(
                    isError=bool(arguments.get("fail")),
                    model_dump=lambda **_: {"content": [{"type": "text", "text": "result"}]},
                )

        for target, replacement in (("stdio_client", Transport), ("ClientSession", Session)):
            mock = patch(f"momoi.mcp.manager.{target}", replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def write(self, servers):
        atomic_write(self.configuration.mcp_path(), json.dumps({"mcpServers": servers}))

    async def test_replacement_rebuilds_all_connections_catalog_and_enable_schema(self):
        self.write({name: {"command": name} for name in ("stable", "changed", "removed")})
        await self.runtime.apply()
        first = self.runtime.daemon
        first_sessions = list(self.opened)
        tools = first.surface.conversation_specs()
        enabled = enable_tools(ToolCall("enable", "tool_enable", {"groups": ["removed"]}),
                               enable_tool_groups=first.surface.mcp_server_groups(), tools=tools,
                               tool_surface=first.surface)
        self.assertTrue(enabled["ok"])
        self.assertIn("mcp__removed__work", {spec["name"] for spec in tools})

        self.write({
            "stable": {"command": "stable"},
            "changed": {"command": "changed", "args": ["v2"], "description": "Updated description"},
            "new group": {"command": "new"},
            "disabled": {"command": "disabled", "disabled": True},
            "offline": {"command": "missing", "optional": True},
        })
        await self.runtime.apply()
        self.assertEqual(self.runtime.state, "running")
        self.assertTrue(first.closed)
        self.assertTrue(all(session in self.closed for session in first_sessions))
        current = self.runtime.daemon
        specs = {spec["name"]: spec for spec in current.surface.conversation_specs()}
        groups = specs["tool_enable"]["input_schema"]["properties"]["groups"]["items"]["enum"]
        self.assertEqual(groups, ["changed", "new group", "stable"])
        self.assertEqual(current.mcp.configs["changed"]["description"], "Updated description")
        self.assertNotIn("mcp__removed__work", specs)
        changed = current.surface.mcp_server_groups()["changed"][0]
        self.assertEqual(changed["input_schema"]["properties"]["value"]["type"], "integer")
        self.assertFalse(current.mcp.has_tool("mcp__removed__work"))
        self.assertTrue(current.mcp.has_tool("mcp__new_group__work"))
        self.assertEqual(len(self.opened), 6)  # Unchanged servers also reconnect.
        self.assertFalse((await current.mcp.call("mcp__removed__work", {}))["ok"])
        failed = await current.mcp.call("mcp__stable__work", {"fail": True})
        self.assertEqual(failed["error"], "mcp_tool_error")
        self.assertTrue((await current.mcp.call("mcp__stable__work", {}))["ok"])

        self.write({})
        await self.runtime.apply()
        empty = {spec["name"]: spec for spec in self.runtime.daemon.surface.conversation_specs()}
        self.assertNotIn("tool_enable", empty)
        schema = empty["heartbeat_begin"]["input_schema"]["properties"]["tool_groups"]
        self.assertEqual(schema["maxItems"], 0)
        self.assertNotIn("enum", schema["items"])

    async def test_invalid_file_preserves_runtime_and_connection_failure_restores_snapshot(self):
        self.write({"working": {"command": "working", "args": ["v2"]}})
        await self.runtime.apply()
        original = self.runtime.daemon
        revision = self.runtime.applied_revision
        for content in ('{', '{"mcpServers": {"bad": 1}}', '{"mcpServers": {"bad": {"command": "x", "args": false}}}'):
            atomic_write(self.configuration.mcp_path(), content)
            await self.runtime.apply()
            self.assertEqual(self.runtime.state, "error")
            self.assertIs(self.runtime.daemon, original)
            self.assertFalse(original.closed)
        self.configuration.mcp_path().unlink()
        await self.runtime.apply()
        self.assertIs(self.runtime.daemon, original)

        self.write({"broken": {"command": "missing"}})
        await self.runtime.apply()
        self.assertTrue(original.closed)
        self.assertTrue(self.runtime.status()["runtime_active"])
        self.assertEqual(self.runtime.applied_revision, revision)
        self.assertIn("previous configuration restored", self.runtime.error)
        self.assertNotIn("private connection details", self.runtime.error)
        restored = self.runtime.daemon
        self.assertTrue(restored.mcp.has_tool("mcp__working__work"))
        self.assertTrue((await restored.mcp.call("mcp__working__work", {}))["ok"])
        self.assertEqual(restored.config.mcp_servers["working"]["args"], ["v2"])
        # The rejected document remains editable; rollback does not overwrite it.
        self.assertIn("broken", json.loads(self.configuration.mcp_path().read_text())["mcpServers"])

        self.write({"fixed": {"command": "fixed"}})
        await self.runtime.apply()
        self.assertEqual(self.runtime.state, "running")
        self.assertEqual(self.runtime.applied_revision, self.configuration.revision())
        self.assertTrue(restored.closed)

    async def test_mcp_file_edit_uses_existing_supervisor_watcher(self):
        stop = asyncio.Event()
        task = asyncio.create_task(self.runtime.run(stop))

        async def applied():
            async with asyncio.timeout(5):
                while self.runtime.applied_revision != self.configuration.revision():
                    await asyncio.sleep(0.01)

        try:
            await applied()
            first = self.runtime.daemon
            self.write({"added": {"command": "added"}})
            await applied()
            self.assertTrue(first.closed)
            self.assertTrue(self.runtime.daemon.mcp.has_tool("mcp__added__work"))
        finally:
            stop.set()
            await task

    async def test_shutdown_resolves_active_and_queued_callers(self):
        manager = MCPManager(None, servers={"server": {"command": "server"}})
        entered = asyncio.Event()

        async def invoke(*_):
            entered.set()
            await asyncio.Event().wait()

        async with manager:
            with patch.object(manager, "_invoke", invoke):
                active = asyncio.create_task(manager.call("mcp__server__work", {}))
                await entered.wait()
                queued = asyncio.create_task(manager.call("mcp__server__work", {}))
                await asyncio.sleep(0)
                await manager.__aexit__()
                async with asyncio.timeout(2):
                    active_result, queued_result = await asyncio.gather(active, queued)
                self.assertEqual(active_result["error"], "mcp_stopped")
                self.assertTrue(active_result["ambiguous"])
                self.assertEqual(queued_result["error"], "mcp_stopped")
                self.assertFalse(queued_result["ambiguous"])

    async def test_cancelled_startup_closes_started_workers(self):
        manager = MCPManager(None, servers={"server": {"command": "server"}})
        entered = asyncio.Event()

        async def connect(*_):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(manager, "_connect", connect):
            starting = asyncio.create_task(manager.__aenter__())
            await entered.wait()
            starting.cancel()
            async with asyncio.timeout(2):
                await asyncio.gather(starting, return_exceptions=True)
        self.assertEqual(manager._workers, {})
