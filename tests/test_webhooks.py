import asyncio
import json
import tempfile
import unittest
from pathlib import Path


from momoi.channel.napcat import NapCatConfig
from momoi.config import (
    AppConfig,
    LLMConfig,
    WebhookConfig,
)
from momoi.daemon import (
    MomoiDaemon,
)
from momoi.memory_tools import MemoryTools
from momoi.models import (
    AgentReply,
    IncomingMessage,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.store import Store
from momoi.webhooks import WebhookService, WorkflowError, bind_workflow, load_catalog


class WebhooksTest(unittest.TestCase):
    def test_webhook_catalog_binds_only_declared_inputs_to_named_exec(self) -> None:
        root = Path(__file__).resolve().parents[1] / "config.example"
        workflows, executors = load_catalog(
            root / "workflows", root / "workflow-executors.yaml"
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

    def test_incompatible_channel_executor_only_disables_its_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows_path = root / "workflows"
            workflows_path.mkdir()
            (root / "executors.yaml").write_text(
                """version: 1
executors:
  napcat-send:
    parameters: {}
    argv: [nap-msg, '${config.owner_qq}']
    env: {}
""",
                encoding="utf-8",
            )
            (workflows_path / "camera.yaml").write_text(
                """version: 1
id: camera-event
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

            with self.assertLogs("momoi.webhooks", level="WARNING") as logs:
                workflows, executors = load_catalog(
                    workflows_path,
                    root / "executors.yaml",
                    {"weixin_user_id"},
                )
            self.assertEqual(set(workflows), {"event-message"})
            self.assertEqual(executors, {})
            self.assertIn(
                "Skipping incompatible workflow executor napcat-send", logs.output[0]
            )
            self.assertTrue(any("camera-event" in item for item in logs.output))


class WebhooksAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_event_turn_can_curl_before_sending_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="contract\n{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                recent_raw_tokens=1000,
                recent_turns=2,
                memory_results=2,
                memory_tokens=1000,
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
            remembered = MemoryTools(daemon.store).execute(
                ToolCall(
                    "remember-packages",
                    "memory_remember",
                    {
                        "kind": "preference",
                        "key": "arrival.packages",
                        "content": "回家事件需要留意已到达的快递",
                        "evidence": event.text,
                        "importance": 0.8,
                    },
                ),
                [event],
                draft,
            )
            self.assertTrue(remembered["ok"])
            daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(
                    ["好，回家时我会留意。"],
                    {
                        "topic": "回家与快递",
                        "open_loops": [],
                        "pending_commitments": [],
                        "short_term_facts": [],
                    },
                ),
                draft,
            )

            class Provider:
                calls = 0
                tool_names: list[list[str]] = []
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
                        call = ToolCall(
                            "fetch-packages",
                            "curl",
                            {"url": "http://static.test/package_state.json"},
                        )
                    else:
                        self.assert_tool_result(messages)
                        call = ToolCall(
                            "notify-owner",
                            "send_message",
                            {"messages": ["有一个快递到了，取件码是 1234。"]},
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
                async def execute(self, call: ToolCall) -> dict[str, object]:
                    self.last_call = call
                    return {
                        "ok": True,
                        "status": 200,
                        "body": '{"arrived":[{"code":"1234"}]}',
                    }

            provider = Provider()
            tools = Tools()
            daemon.provider = provider  # type: ignore[assignment]
            daemon.builtin_tools = tools  # type: ignore[assignment]
            reply = await daemon._complete_webhook_message(
                "回家时检查快递状态并根据结果提醒我。"
            )
            self.assertEqual(reply, ["有一个快递到了，取件码是 1234。"])
            self.assertEqual(provider.calls, 2)
            self.assertEqual(provider.tool_names[0], ["send_message", "curl"])
            self.assertEqual(tools.last_call.name, "curl")
            system_text = json.dumps(provider.systems[0], ensure_ascii=False)
            context_text = json.dumps(provider.conversations[0], ensure_ascii=False)
            self.assertIn("natural soul", system_text)
            self.assertIn("Momoi webhook event contract", system_text)
            self.assertIn("以后回家时帮我留意快递", context_text)
            self.assertIn("好，回家时我会留意", context_text)
            self.assertIn("回家与快递", context_text)
            self.assertIn("回家事件需要留意已到达的快递", context_text)
            daemon.store.close()

    async def test_message_webhook_is_idempotent_and_waits_for_outbox_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1] / "config.example"
            store = Store(Path(directory) / "momoi.sqlite3")
            generated: list[str] = []

            async def generate(prompt: str) -> list[str]:
                generated.append(prompt)
                return ["门口好像有人，我提醒你看一下。"]

            service = WebhookService(
                WebhookConfig(
                    enabled=True,
                    token="test",
                    workflows=root / "workflows",
                    executors=root / "workflow-executors.yaml",
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
            self.assertEqual(rows[0].text, "门口好像有人，我提醒你看一下。")
            self.assertEqual(
                store.webhook_run(str(first["id"]))["state"], "waiting_delivery"
            )
            store.mark_sent(rows[0].id)
            await asyncio.wait_for(task, timeout=1)
            self.assertEqual(store.webhook_run(str(first["id"]))["state"], "succeeded")
            self.assertEqual(
                store._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
            )
            store.close()
