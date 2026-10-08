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
        self.runtime = RuntimeSupervisor(self.configuration, factory=MCPDaemon, poll_interval_seconds=0.01)
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
        servers = {name: {"description": f"{name} tools", **config} for name, config in servers.items()}
        atomic_write(self.configuration.mcp_path(), json.dumps({"mcpServers": servers}))

    async def test_mcp_reload_keeps_runtime_and_rebuilds_catalog_and_enable_schema(self):
        self.write({name: {"command": name} for name in ("stable", "changed", "removed")})
        await self.runtime.apply()
        first = self.runtime.daemon
        first_sessions = list(self.opened)
        tools = first.surface.conversation_specs()
        enabled = enable_tools(ToolCall("enable", "tool_enable", {"tools": ["mcp__removed__work"]}),
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
        self.assertFalse(first.closed)
        self.assertTrue(all(session in self.closed for session in first_sessions))
        current = self.runtime.daemon
        self.assertIs(current, first)
        specs = {spec["name"]: spec for spec in current.surface.conversation_specs()}
        index = current.surface.tool_index()
        for name in ("changed", "new group", "stable"):
            self.assertIn(f"- {name}:", index)
        self.assertIn("Updated description", index)
        self.assertNotIn("- removed:", index)
        self.assertNotIn("mcp__", index)
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
        self.assertIn("tool_search", empty)
        self.assertNotIn("tool_groups", empty["heartbeat_begin"]["input_schema"]["properties"])

    async def test_invalid_file_preserves_runtime_and_connection_failure_allows_startup(self):
        self.write({"working": {"command": "working", "args": ["v2"]}})
        await self.runtime.apply()
        original = self.runtime.daemon
        for content in ('{', '{"mcpServers": {"bad": 1}}', '{"mcpServers": {"bad": {"command": "x", "args": false}}}'):
            atomic_write(self.configuration.mcp_path(), content)
            await self.runtime.apply()
            self.assertEqual(self.runtime.state, "error")
            self.assertIs(self.runtime.daemon, original)
            self.assertFalse(original.closed)
        self.configuration.mcp_path().unlink()
        await self.runtime.apply()
        self.assertIs(self.runtime.daemon, original)

        self.write({
            "before": {"command": "working"},
            "broken": {"command": "missing"},
            "after": {"command": "working"},
        })
        await self.runtime.apply()
        self.assertFalse(original.closed)
        self.assertTrue(self.runtime.status()["runtime_active"])
        self.assertEqual(self.runtime.state, "running")
        self.assertEqual(self.runtime.applied_revision, self.configuration.revision())
        self.assertEqual(self.runtime.error, "")
        restored = self.runtime.daemon
        self.assertEqual(set(restored.surface.mcp_server_groups()), {"before", "after"})
        for name in ("before", "after"):
            self.assertTrue((await restored.mcp.call(f"mcp__{name}__work", {}))["ok"])
        self.assertNotIn("broken", restored.mcp._workers)
        self.assertNotIn("broken", restored.mcp._queues)

        self.write({"fixed": {"command": "fixed"}})
        await self.runtime.apply()
        self.assertEqual(self.runtime.state, "running")
        self.assertEqual(self.runtime.applied_revision, self.configuration.revision())
        self.assertFalse(restored.closed)

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
            self.assertFalse(first.closed)
            self.assertTrue(self.runtime.daemon.mcp.has_tool("mcp__added__work"))
        finally:
            stop.set()
            await task

    async def test_explicit_reload_validates_and_reports_each_server(self):
        self.write({"before": {"command": "working"}})
        await self.runtime.apply()
        manager = self.runtime.daemon.mcp
        before = list(self.opened)
        self.configuration.mcp_path().write_text('{')
        result = await manager.reload()
        self.assertEqual(result["error"], "invalid_mcp_config")
        self.assertTrue((await manager.call("mcp__before__work", {}))["ok"])
        self.assertFalse(any(session in self.closed for session in before))
        self.write({"new": {"command": "working"}, "broken": {"command": "missing"}})
        result = await manager.reload()
        self.assertEqual(result["error"], "mcp_connect_failed")
        servers = {item["name"]: item for item in result["servers"]}
        self.assertEqual(servers["new"]["tools"], ["mcp__new__work"])
        self.assertTrue(servers["new"]["connected"])
        self.assertFalse(servers["broken"]["connected"])
        self.assertIn("FileNotFoundError", servers["broken"]["error"])
        self.assertNotIn("private connection details", str(result))
        self.assertTrue(all(session in self.closed for session in before))
        self.assertEqual(manager.generation, 1)

    async def test_agent_installs_reloads_and_calls_new_tool_in_same_turn(self):
        from momoi.runtime import MomoiDaemon
        from momoi.runtime.agent import TurnExecutionSpec
        from momoi.models import AgentReply, IncomingMessage, ProviderResponse, TurnDraft
        daemon = MomoiDaemon(self.configuration.validate())
        self.addCleanup(daemon.store.close)
        event = IncomingMessage("install", "install", "安装工具", 1, 1)
        daemon.store.add_event(event)
        turn_id = daemon._turn_id(event.event_id)
        daemon.store.begin_turn(turn_id, "owner", [event.event_id])
        test = self

        class Provider:
            calls = 0

            async def complete(self, system, messages, tools, **_):
                self.calls += 1
                names = {spec["name"] for spec in tools}
                if self.calls == 1:
                    test.assertIn("mcp-install", str(system))
                    test.assertNotIn("skill_search", names)
                    call = ToolCall("load-workflow", "skill_load", {"name": "mcp-install"})
                elif self.calls == 2:
                    test.assertIn("PowerShell", str(messages))
                    test.assertIn("tools/mcp", str(messages))
                    test.assertNotIn("mcp_reload", names)
                    call = ToolCall("enable-reload", "tool_enable", {"tools": ["mcp_reload"]})
                elif self.calls == 3:
                    test.write({"added": {"command": "working", "description": "查询新增数据"}})
                    call = ToolCall("reload", "mcp_reload", {})
                elif self.calls == 4:
                    test.assertTrue(any("查询新增数据" in str(item) for item in messages))
                    call = ToolCall("find", "tool_search", {"query": "查询新增数据"})
                elif self.calls == 5:
                    test.assertIn("mcp__added__work", str(messages))
                    call = ToolCall("enable-new", "tool_enable", {"tools": ["mcp__added__work"]})
                elif self.calls == 6:
                    test.assertIn("mcp__added__work", names)
                    call = ToolCall("use-new", "mcp__added__work", {})
                else:
                    test.assertEqual(self.calls, 7)
                    test.assertIn("result", str(messages))
                    call = ToolCall("end", "end_turn", {
                        "reply_wait": {"wait": False},
                        "mood": {"decision": "unchanged"},
                    })
                return ProviderResponse([], [call])

        provider = Provider()
        from tests.support import with_owner_recall
        daemon.provider = with_owner_recall(provider)
        async with daemon.mcp:
            result = await daemon._run_tool_loop(
                daemon._system(), [{"role": "user", "content": "安装工具"}],
                daemon.tool_surface.conversation_specs(), [event], TurnDraft(),
                execution=TurnExecutionSpec("owner", permitted_tools=daemon.tool_surface.permitted_names("owner")),
                source_event_id=event.event_id, turn_id=turn_id, delivery_channel=daemon.channel,
            )
        self.assertIsInstance(result, AgentReply)
        self.assertEqual(provider.calls, 7)

    async def test_reload_waits_for_inflight_call(self):
        self.write({"before": {"command": "working"}})
        await self.runtime.apply()
        manager = self.runtime.daemon.mcp
        entered, release = asyncio.Event(), asyncio.Event()

        async def invoke(*_):
            entered.set()
            await release.wait()
            return {"ok": True, "value": "completed"}

        with patch.object(manager, "_invoke", invoke):
            active = asyncio.create_task(manager.call("mcp__before__work", {}))
            await entered.wait()
            self.write({"after": {"command": "working"}})
            reloading = asyncio.create_task(manager.reload())
            await asyncio.sleep(0)
            self.assertFalse(reloading.done())
            release.set()
            async with asyncio.timeout(2):
                self.assertEqual((await active)["value"], "completed")
                self.assertTrue((await reloading)["ok"])
        self.assertTrue(manager.has_tool("mcp__after__work"))

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


def test_brave_web_search_is_visible_without_enabling_other_group_tools():
    web = {"name": "mcp__brave-search__brave_web_search", "description": "Search",
           "input_schema": {"type": "object", "properties": {}}}
    local = {**web, "name": "mcp__brave-search__brave_local_search"}
    mcp = SimpleNamespace(tool_specs=[local, web], configs={},
                          tool_group=lambda name: "brave-search")
    surface = ToolSurface(mcp, {})
    tools = surface.conversation_specs()
    names = [spec["name"] for spec in tools]
    assert names.count(web["name"]) == 1
    assert local["name"] not in names
    for stage in ("owner", "heartbeat", "goal"):
        assert web["name"] in surface.permitted_names(stage)
    added = surface.append_visible(tools, surface.mcp_server_groups()["brave-search"])
    assert added == [local["name"]]
    assert sum(spec["name"] == web["name"] for spec in tools) == 1

    mcp.tool_specs = []
    assert web["name"] not in {spec["name"] for spec in surface.conversation_specs()}


def test_reload_is_deferred_and_discoverable_before_first_install(tmp_path):
    from momoi.runtime.agent.runtime_tools import search_tools
    manager = MCPManager(tmp_path / "mcp.json", servers={})
    surface = ToolSurface(manager, {})
    tools = surface.conversation_specs()
    assert "mcp_reload" not in {spec["name"] for spec in tools}
    assert "builtin_mcp_management" in surface.tool_index()
    groups = surface.discovery_groups()
    found = search_tools(ToolCall("find", "tool_search", {"query": "mcp_reload"}),
                         enable_tool_groups=groups, tool_surface=surface)
    assert found["tools"][0]["name"] == "mcp_reload"
    result = enable_tools(ToolCall("enable", "tool_enable", {"tools": ["mcp_reload"]}),
                          enable_tool_groups=groups, tools=tools, tool_surface=surface)
    assert result["ok"]
    assert "mcp_reload" in {spec["name"] for spec in tools}
    assert "mcp_reload" in surface.permitted_names("owner")
    assert "mcp_reload" not in surface.permitted_names("webhook")


def test_description_is_required_even_for_disabled_servers():
    from momoi.mcp.config import parse_mcp_servers
    import pytest
    for disabled in (False, True):
        for description in (None, "", "   ", "x" * 501):
            server = {"command": "test", "disabled": disabled}
            if description is not None:
                server["description"] = description
            with pytest.raises(ValueError, match="description must be"):
                parse_mcp_servers(json.dumps({"mcpServers": {"test": server}}))
