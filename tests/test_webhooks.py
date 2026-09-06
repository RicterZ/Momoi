from tests.support import seed_memory
from tests.support import provider_catalog
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig, WebhookConfig
from momoi.integrations.models import LLMConfig
from momoi.tools.memory import MemoryTools
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.runtime import (
    MomoiDaemon,
)
from momoi.runtime.context.rendering import assemble_recent_external_events
from momoi.storage import Store
from momoi.webhooks.catalog import WorkflowError, bind_workflow, load_catalog
from momoi.webhooks.service import WebhookService


class WebhooksTest(unittest.TestCase):
    def test_webhook_catalog_binds_only_declared_inputs_to_named_exec(self) -> None:
        root = Path(__file__).resolve().parents[1] / "config.example"
        workflows, executors = load_catalog(
            root / "workflows", root / "workflows" / "workflow-executors.yaml"
        )
        channel_variables = {
            "channel_url": "ws://napcat.test/ws",
            "owner_id": "20000",
        }
        target_url = "https://status.example.com/health"
        plan = bind_workflow(
            workflows["url-check-event"],
            executors,
            {"event_prompt": "服务已经恢复，请提醒我。", "target_url": target_url},
            channel_variables,
        )
        argv = plan["steps"][0]["argv"]
        self.assertEqual(argv.count(target_url), 1)
        self.assertEqual(argv[-1], target_url)
        self.assertEqual(plan["steps"][1]["prompt"], "服务已经恢复，请提醒我。")
        with self.assertRaisesRegex(WorkflowError, "allowed absolute URL"):
            bind_workflow(
                workflows["url-check-event"],
                executors,
                {"event_prompt": "测试", "target_url": "file:///etc/passwd"},
                channel_variables,
            )
        checked = bind_workflow(
            workflows["url-check-event"],
            executors,
            {"event_prompt": "检查完成", "target_url": "http://example.com/health"},
            channel_variables,
        )
        self.assertEqual(checked["steps"][0]["argv"][-1], "http://example.com/health")
        with self.assertRaisesRegex(WorkflowError, "unknown inputs"):
            bind_workflow(
                workflows["event-message"],
                executors,
                {"event_prompt": "测试", "command": "rm -rf /"},
                channel_variables,
            )

    def test_catalog_skips_executors_file_inside_workflows_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflows_path = Path(directory) / "workflows"
            workflows_path.mkdir()
            executors_path = workflows_path / "workflow-executors.yaml"
            executors_path.write_text(
                """version: 1
executors:
  echo-ok:
    parameters: {}
    argv: ['true']
    env: {}
""",
                encoding="utf-8",
            )
            (workflows_path / "event.yaml").write_text(
                """version: 1
id: event-message
inputs:
  event_prompt: {type: string, required: true}
steps:
  - id: notify
    uses: message
    prompt: '${inputs.event_prompt}'
""",
                encoding="utf-8",
            )
            with self.assertLogs("momoi.webhooks.catalog", level="INFO") as logs:
                workflows, executors = load_catalog(workflows_path, executors_path)
            self.assertEqual(set(workflows), {"event-message"})
            self.assertEqual(set(executors), {"echo-ok"})
            events = [record.momoi_event for record in logs.records]
            self.assertIn("workflow_loaded", events)
            self.assertIn("workflow_catalog_loaded", events)
            loaded = next(
                record
                for record in logs.records
                if record.momoi_event == "workflow_loaded"
            )
            self.assertEqual(loaded.momoi_fields["workflow_id"], "event-message")
            self.assertEqual(loaded.momoi_fields["steps"], 1)
            catalog = next(
                record
                for record in logs.records
                if record.momoi_event == "workflow_catalog_loaded"
            )
            self.assertEqual(catalog.momoi_fields["workflows"], 1)
            self.assertEqual(catalog.momoi_fields["executors"], 1)
            self.assertEqual(catalog.momoi_fields["workflow_ids"], "event-message")
            self.assertEqual(catalog.momoi_fields["executor_ids"], "echo-ok")

    def test_optional_incompatible_executor_and_workflow_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows_path = root / "workflows"
            workflows_path.mkdir()
            (root / "executors.yaml").write_text(
                """version: 1
executors:
  napcat-send:
    optional: true
    parameters: {}
    argv: [nap-msg, '${config.owner_qq}']
    env: {}
""",
                encoding="utf-8",
            )
            (workflows_path / "camera.yaml").write_text(
                """version: 1
id: camera-event
optional: true
inputs: {}
steps:
  - id: send
    uses: exec
    executor: napcat-send
    args: {}
""",
                encoding="utf-8",
            )
            (workflows_path / "event.yaml").write_text(
                """version: 1
id: event-message
inputs:
  event_prompt: {type: string, required: true}
steps:
  - id: notify
    uses: message
    prompt: '${inputs.event_prompt}'
""",
                encoding="utf-8",
            )

            with self.assertLogs("momoi.webhooks.catalog", level="WARNING") as logs:
                workflows, executors = load_catalog(
                    workflows_path,
                    root / "executors.yaml",
                    {"weixin_user_id"},
                )
            self.assertEqual(set(workflows), {"event-message"})
            self.assertEqual(executors, {})
            self.assertEqual(
                logs.records[0].momoi_event, "workflow_executor_skipped"
            )
            self.assertEqual(
                logs.records[0].momoi_fields["executor_id"], "napcat-send"
            )
            self.assertTrue(
                any(
                    getattr(record, "momoi_fields", {}).get("workflow_id")
                    == "camera-event"
                    for record in logs.records
                )
            )

    def test_required_incompatible_executor_rejects_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / "workflows"
            workflows.mkdir()
            executors = root / "executors.yaml"
            executors.write_text(
                """version: 1
executors:
  required-send:
    parameters: {}
    argv: [send, '${config.missing}']
    env: {}
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkflowError, "is incompatible"):
                load_catalog(workflows, executors, {"available"})


class WebhooksAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_turn_uses_normal_curl_and_end_turn_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="contract\n{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                soul_prompt="natural soul",
            )
            daemon = MomoiDaemon(config)
            event = IncomingMessage(
                "qq:1:webhook-context",
                "webhook-context",
                "以后回家时帮我留意快递",
                1,
                1,
            )
            daemon.store.add_event(event)
            draft = TurnDraft()
            seed_memory(daemon.store, event, kind="routine", key="routine.arrive_home.parcel",
                        content="回家事件需要留意已到达的快递")
            owner_turn_id = "owner-packages"
            daemon.store.begin_turn(owner_turn_id, "owner", [event.event_id])
            daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["好，回家时我会留意。"]),
                draft,
                turn_id=owner_turn_id,
            )
            owner_outbox_id = daemon.store._db.execute(
                "SELECT id FROM outbox WHERE turn_id=?", (owner_turn_id,)
            ).fetchone()["id"]
            daemon.store.mark_sent(int(owner_outbox_id))
            daemon.store.create_episode(
                "回家与快递",
                episode_id="arrival-packages",
                topics=["回家", "快递"],
            )
            daemon.store.link_turn_to_episode("arrival-packages", owner_turn_id)

            class Provider:
                calls = 0
                tool_names: list[list[str]] = []
                tools: list[dict[str, object]] = []
                systems: list[object] = []
                conversations: list[list[dict[str, object]]] = []

                async def complete(
                    self,
                    _: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **__: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    self.systems.append(_)
                    self.conversations.append(messages)
                    self.tool_names.append([str(tool["name"]) for tool in tools])
                    if self.calls == 1:
                        self.tools = tools
                    if self.calls == 1:
                        call = ToolCall(
                            "fetch-packages",
                            "curl",
                            {
                                "url": "http://static.test/package_state.json",
                            },
                        )
                    elif self.calls == 2:
                        self.assert_tool_result(messages)
                        call = ToolCall(
                            "notify-owner",
                            "send_bubbles",
                            {
                                "bubbles": ["有一个快递到了，取件码是 1234。"],
                            },
                        )
                    else:
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                            },
                        )
                    return ProviderResponse(
                        [
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                        ],
                        [call],
                    )

                @staticmethod
                def assert_tool_result(messages: list[dict[str, object]]) -> None:
                    result = messages[-1]["content"]  # type: ignore[index]
                    assert "1234" in str(result)

            class Tools:
                @staticmethod
                def has_tool(name: str) -> bool:
                    return name == "curl"

                @staticmethod
                def capability(_: ToolCall) -> str:
                    return "read"

                async def execute(self, call: ToolCall) -> dict[str, object]:
                    self.last_call = call
                    return {
                        "ok": True,
                        "status": 200,
                        "body": '{"arrived":[{"code":"1234"}]}',
                    }

            provider = Provider()
            provider.config = daemon.services.llm.config  # type: ignore[attr-defined]
            tools = Tools()
            daemon.provider = provider  # type: ignore[assignment]
            daemon.builtin_tools = tools  # type: ignore[assignment]
            daemon.tool_executor.builtin_tools = tools  # type: ignore[assignment]
            reply = await daemon._complete_webhook_turn(
                "回家时检查快递状态并根据结果提醒我。", "webhook:test:0"
            )
            self.assertEqual(reply.messages, [])
            self.assertEqual(provider.calls, 3)
            expected_surface = [
                str(tool["name"])
                for tool in daemon.tool_surface.conversation_specs()
            ]
            self.assertEqual(
                provider.tool_names[0],
                expected_surface,
            )
            self.assertTrue(
                all(names == expected_surface for names in provider.tool_names)
            )
            self.assertEqual(tools.last_call.name, "curl")
            self.assertEqual(
                tools.last_call.arguments,
                {"url": "http://static.test/package_state.json"},
            )
            outbox = [
                str(row["text"])
                for row in daemon.store._db.execute(
                    "SELECT text FROM outbox ORDER BY id"
                )
            ]
            self.assertIn("有一个快递到了，取件码是 1234。", outbox)
            system_text = json.dumps(provider.systems[0], ensure_ascii=False)
            context_text = json.dumps(provider.conversations[0], ensure_ascii=False)
            self.assertIn("natural soul", system_text)
            self.assertNotIn("Momoi webhook event contract", system_text)
            self.assertIn("<workflow_contract>", context_text)
            self.assertIn("Momoi webhook event contract", context_text)
            self.assertIn("follows the shared Style Card", context_text)
            self.assertIn("<current_webhook_task>", context_text)
            self.assertIn("<runtime_state>", context_text)
            self.assertNotIn("<recent_conversation>", context_text)
            self.assertNotIn("<recent_turns>", context_text)
            self.assertIn("<webhook_activity>", context_text)
            self.assertNotIn("<conversation_state>", context_text)
            historical = provider.conversations[0][1:3]
            self.assertEqual(
                [message["role"] for message in historical],
                ["user", "assistant"],
            )
            self.assertIn("以后回家时帮我留意快递", str(historical[0]["content"]))
            self.assertIn("好，回家时我会留意", str(historical[1]["content"]))
            self.assertLess(
                context_text.index("<runtime_state>"),
                context_text.index("<current_webhook_task>"),
            )
            self.assertNotIn("<owner_preferences>", context_text)
            self.assertRegex(context_text, r"\[\d{4}-\d{2}-\d{2}T")
            self.assertIn("以后回家时帮我留意快递", context_text)
            self.assertIn("好，回家时我会留意", context_text)
            daemon.store.close()

    async def test_message_webhook_is_idempotent_and_waits_for_outbox_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1] / "config.example"
            store = Store(Path(directory) / "momoi.sqlite3")
            generated: list[str] = []

            async def generate(prompt: str, turn_id: str) -> AgentReply:
                generated.append(prompt)
                store.begin_turn(turn_id, "webhook", [turn_id])
                return AgentReply(
                    ["门口好像有人，需要我继续帮你留意吗？"],
                    mood_update={
                        "state": "focused",
                        "intensity": 0.5,
                        "cause": "门口出现需要关注的动态",
                    },
                    reply_wait={
                        "wait": True,
                        "delay_minutes": 5,
                        "expected_information": "主人是否需要继续留意门口",
                        "reason": "门口动态需要主人决定是否继续观察",
                    },
                )

            service = WebhookService(
                WebhookConfig(
                    enabled=True,
                    token="test",
                    workflows=root / "workflows",
                    executors=root / "workflows" / "workflow-executors.yaml",
                ),
                {"channel_url": "ws://napcat.test/ws", "owner_id": "20000"},
                store,
                generate,
                lambda: None,
            )
            plan = bind_workflow(
                service.workflows["event-message"],
                service.executors,
                {"event_prompt": "门口检测到有人，请自然提醒我。"},
                service.channel_variables,
            )
            first, created = store.create_webhook_run(
                "event-message", "same-event", plan
            )
            repeated, repeated_created = store.create_webhook_run(
                "event-message", "same-event", plan
            )
            self.assertTrue(created)
            self.assertFalse(repeated_created)
            self.assertEqual(first["id"], repeated["id"])

            run = store.claim_webhook_run()
            self.assertIsNotNone(run)
            task = asyncio.create_task(service._execute(run, asyncio.Event()))
            for _ in range(20):
                rows = store.due_outbox()
                if rows:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(generated, ["门口检测到有人，请自然提醒我。"])
            self.assertEqual(rows[0].text, "门口好像有人，需要我继续帮你留意吗？")
            stored_wait = json.loads(
                store._db.execute(
                    "SELECT reply_expectation FROM outbox WHERE id=?", (rows[0].id,)
                ).fetchone()[0]
            )
            self.assertEqual(
                stored_wait["expected_information"], "主人是否需要继续留意门口"
            )
            self.assertEqual(stored_wait["delay_minutes"], 5)
            self.assertEqual(store.self_state()["mood_state"], "focused")
            self.assertEqual(
                store.webhook_run(str(first["id"]))["state"], "waiting_delivery"
            )
            webhook_turn_id = f"webhook:{first['id']}:0"
            self.assertEqual(
                [
                    str(row["role"])
                    for row in store._db.execute(
                        """SELECT role FROM messages
                           WHERE turn_id=? ORDER BY id""",
                        (webhook_turn_id,),
                    ).fetchall()
                ],
                ["event", "assistant"],
            )
            self.assertEqual(
                store._db.execute(
                    """SELECT content FROM messages
                       WHERE turn_id=? AND role='event'""",
                    (webhook_turn_id,),
                ).fetchone()[0],
                "门口检测到有人，请自然提醒我。",
            )
            self.assertEqual(
                store.record_webhook_event(
                    str(first["id"]),
                    webhook_turn_id,
                    "门口检测到有人，请自然提醒我。",
                ),
                int(
                    store._db.execute(
                        """SELECT id FROM messages
                           WHERE turn_id=? AND role='event'""",
                        (webhook_turn_id,),
                    ).fetchone()[0]
                ),
            )
            self.assertEqual(
                store._db.execute(
                    """SELECT COUNT(*) FROM messages
                       WHERE turn_id=? AND role='event'""",
                    (webhook_turn_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                store._db.execute(
                    """SELECT turn_id FROM messages
                       WHERE content='门口好像有人，需要我继续帮你留意吗？'"""
                ).fetchone()[0],
                webhook_turn_id,
            )
            episode = store.search_episodes("门口 | 继续留意", 3)[0]
            contents = [
                item["content"]
                for item in store.conversation_episode(str(episode["id"]))[
                    "messages"
                ]
            ]
            self.assertIn("门口检测到有人，请自然提醒我。", contents)
            self.assertIn("门口好像有人，需要我继续帮你留意吗？", contents)
            store.mark_sent(rows[0].id)
            await asyncio.wait_for(task, timeout=1)
            self.assertEqual(store.webhook_run(str(first["id"]))["state"], "succeeded")
            self.assertEqual(
                store._db.execute(
                    "SELECT state FROM turns WHERE id=?",
                    (webhook_turn_id,),
                ).fetchone()[0],
                "completed",
            )
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
            )
            store.close()

    async def test_message_webhook_can_finish_silently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1] / "config.example"
            store = Store(Path(directory) / "momoi.sqlite3")

            async def generate(_: str, __: str) -> AgentReply:
                return AgentReply([])

            service = WebhookService(
                WebhookConfig(
                    enabled=True,
                    token="test",
                    workflows=root / "workflows",
                    executors=root / "workflows" / "workflow-executors.yaml",
                ),
                {"channel_url": "ws://napcat.test/ws", "owner_id": "20000"},
                store,
                generate,
                lambda: None,
            )
            plan = bind_workflow(
                service.workflows["event-message"],
                service.executors,
                {"event_prompt": "没有变化时保持安静。"},
                service.channel_variables,
            )
            created, _ = store.create_webhook_run("event-message", "silent", plan)
            run = store.claim_webhook_run()
            await service._execute(run, asyncio.Event())

            self.assertEqual(
                store.webhook_run(str(created["id"]))["state"], "succeeded"
            )
            self.assertEqual(store.due_outbox(), [])
            self.assertEqual(
                store.webhook_step(str(created["id"]), 0)["result"],
                {"outbox_ids": []},
            )
            webhook_turn_id = f"webhook:{created['id']}:0"
            event = store._db.execute(
                """SELECT role, content, delivery_state FROM messages
                   WHERE turn_id=?""",
                (webhook_turn_id,),
            ).fetchall()
            self.assertEqual(len(event), 1)
            self.assertEqual(event[0]["role"], "event")
            self.assertEqual(event[0]["content"], "没有变化时保持安静。")
            self.assertEqual(event[0]["delivery_state"], "delivered")
            recent = store.recent_conversation_messages(5, 2000)
            self.assertEqual(recent, [])
            external = assemble_recent_external_events(store)
            self.assertIn("[webhook:event-message]", external)
            self.assertIn("event: 没有变化时保持安静。", external)
            found = store.search_episodes("没有变化", 3)
            self.assertTrue(found)
            self.assertIn(
                "没有变化时保持安静。",
                [
                    item["content"]
                    for item in store.conversation_episode(str(found[0]["id"]))[
                        "messages"
                    ]
                ],
            )
            searched = MemoryTools(store).execute(
                ToolCall(
                    "search-webhook",
                    "episode_search",
                    {"query": "没有变化"},
                ),
                [],
                TurnDraft(),
            )
            self.assertTrue(searched["ok"])
            self.assertGreater(searched["count"], 0)
            episode = found[0]
            self.assertRegex(
                str(episode["title"]), r"^Webhook event-message · \d{4}-\d{2}-\d{2}$"
            )
            store.close()

    async def test_webhook_events_share_one_episode_per_local_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1] / "config.example"
            store = Store(Path(directory) / "momoi.sqlite3")

            async def generate(_: str, __: str) -> AgentReply:
                return AgentReply([])

            service = WebhookService(
                WebhookConfig(
                    enabled=True,
                    token="test",
                    workflows=root / "workflows",
                    executors=root / "workflows" / "workflow-executors.yaml",
                ),
                {"channel_url": "ws://napcat.test/ws", "owner_id": "20000"},
                store,
                generate,
                lambda: None,
            )
            first_plan = bind_workflow(
                service.workflows["event-message"],
                service.executors,
                {"event_prompt": "门锁超时未关"},
                service.channel_variables,
            )
            second_plan = bind_workflow(
                service.workflows["event-message"],
                service.executors,
                {"event_prompt": "当前仓鼠窝温度为 28.0°C"},
                service.channel_variables,
            )
            store.create_webhook_run("event-message", "door", first_plan)
            store.create_webhook_run("event-message", "hamster", second_plan)
            store.create_webhook_run("url-check-event", "other", first_plan)
            first = store.claim_webhook_run()
            await service._execute(first, asyncio.Event())
            second = store.claim_webhook_run()
            await service._execute(second, asyncio.Event())
            other = store.claim_webhook_run()
            await service._execute(other, asyncio.Event())

            door = store.search_episodes("门锁超时", 3)
            hamster = store.search_episodes("仓鼠窝", 3)
            self.assertTrue(door)
            self.assertTrue(hamster)
            by_title = {str(item["title"]): item for item in door}
            event_title = next(
                title
                for title in by_title
                if title.startswith("Webhook event-message ")
            )
            other_title = next(
                title
                for title in by_title
                if title.startswith("Webhook url-check-event ")
            )
            self.assertEqual(by_title[event_title]["id"], hamster[0]["id"])
            self.assertNotEqual(by_title[event_title]["id"], by_title[other_title]["id"])
            contents = [
                item["content"]
                for item in store.conversation_episode(
                    str(by_title[event_title]["id"])
                )["messages"]
            ]
            self.assertIn("门锁超时未关", contents)
            self.assertIn("当前仓鼠窝温度为 28.0°C", contents)
            store.close()
