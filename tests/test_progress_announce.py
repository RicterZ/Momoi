import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from momoi.tools.agenda import AGENDA_TOOL_SPECS
from momoi.tools.builtin import BUILTIN_TOOL_SPECS
from momoi.contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD
from momoi.mcp_client import MCP_TOOL_POLICY
from momoi.models import ToolCall
from momoi.runtime.agent.progress import (
    ANNOUNCE_FIELD,
    ANNOUNCE_DELIVERY_NOTE,
    announce_field,
    apply_tool_announce,
    decorate_tool_spec,
    initial_announce_error_message,
    missing_initial_work_announce,
    requests_owner_progress,
    take_announce_message,
)
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.runtime.tool_contracts.conversation import send_bubbles_tool_spec
from momoi.runtime.tool_contracts.runtime import tool_enable_spec
from momoi.storage import estimate_tokens


class ProgressAnnounceTest(unittest.TestCase):
    def test_tools_declare_owner_progress_without_a_name_registry(self) -> None:
        builtins = {spec["name"]: spec for spec in BUILTIN_TOOL_SPECS}
        agenda = {spec["name"]: spec for spec in AGENDA_TOOL_SPECS}
        self.assertTrue(requests_owner_progress(builtins["curl"]))
        self.assertTrue(requests_owner_progress(agenda["goal_create"]))
        self.assertTrue(requests_owner_progress(agenda["goal_cancel"]))
        self.assertFalse(requests_owner_progress(agenda["goal_update"]))
        self.assertFalse(requests_owner_progress(agenda["goal_finish"]))
        self.assertFalse(requests_owner_progress(builtins["sleep"]))
        self.assertFalse(requests_owner_progress(builtins["read_file"]))
        self.assertTrue(
            requests_owner_progress(
                {
                    "name": "mcp__demo__lookup",
                    OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
                    "input_schema": {"type": "object"},
                }
            )
        )

    def test_adds_say_to_owner_to_curl_schema(self) -> None:
        curl = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")
        decorated = decorate_tool_spec(curl)
        self.assertEqual(announce_field(curl), None)
        self.assertEqual(announce_field(decorated), ANNOUNCE_FIELD)
        self.assertNotIn(ANNOUNCE_FIELD, decorated["input_schema"]["required"])
        description = decorated["input_schema"]["properties"][ANNOUNCE_FIELD][
            "description"
        ]
        self.assertIn(ANNOUNCE_DELIVERY_NOTE, description)
        self.assertNotIn("Optional natural", description)
        self.assertIn("Conditionally required", description)
        self.assertIn("first external-work batch", description)
        self.assertIn("first tool unless send_bubbles", description)
        self.assertIn("Ordinary assistant content is discarded", description)
        self.assertIn("Later tool rounds may omit it", description)
        self.assertIn("evidence-backed", description)
        self.assertIn("tool caption", description)
        self.assertIn("retry narration", description)
        self.assertIn("request recap", description)
        self.assertIn("promise of success", description)
        self.assertLess(len(description), 700)
        self.assertNotIn(ANNOUNCE_FIELD, curl["input_schema"]["properties"])

    def test_send_bubbles_schema_stays_compact_without_losing_constraints(
        self,
    ) -> None:
        spec = send_bubbles_tool_spec(["napcat", "weixin"], "napcat")
        rendered = json.dumps(
            spec, ensure_ascii=False, separators=(",", ":")
        )
        self.assertIn("owner-visible", spec["description"])
        self.assertIn("file, video, audio, and record", spec["description"])
        self.assertEqual(
            spec["input_schema"]["properties"]["channel"]["enum"],
            ["napcat", "weixin"],
        )
        self.assertIn(
            "primary (napcat)",
            spec["input_schema"]["properties"]["channel"]["description"],
        )
        self.assertLess(estimate_tokens(rendered), 550)

    def test_tool_enable_catalog_uses_group_descriptions(self) -> None:
        spec = tool_enable_spec(
            {
                "demo": "Operate demo records.",
                "other": "Look up external records.",
            }
        )
        description = spec["description"]
        self.assertIn("demo: Operate demo records.", description)
        self.assertIn("other: Look up external records.", description)
        self.assertEqual(
            spec["input_schema"]["properties"]["groups"]["items"]["enum"],
            ["demo", "other"],
        )

    def test_initial_announce_error_explains_conditional_requirement(self) -> None:
        message = initial_announce_error_message(ANNOUNCE_FIELD)
        self.assertIn("first external-work tool batch", message)
        self.assertIn("send_bubbles before it", message)
        self.assertIn("Later tool rounds may omit", message)

    def test_first_external_batch_requires_one_initial_acknowledgement(self) -> None:
        curl = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")
        tools = [decorate_tool_spec(curl)]
        missing = missing_initial_work_announce(
            [ToolCall("first", "curl", {"url": "https://example.com"})],
            tools,
            owner_work_acknowledged=False,
        )
        self.assertEqual(missing, ("first", ANNOUNCE_FIELD))

        announced = missing_initial_work_announce(
            [
                ToolCall(
                    "first",
                    "curl",
                    {
                        "url": "https://example.com",
                        ANNOUNCE_FIELD: "嗯，我看看",
                    },
                )
            ],
            tools,
            owner_work_acknowledged=False,
        )
        self.assertIsNone(announced)

        later_silent = missing_initial_work_announce(
            [ToolCall("later", "curl", {"url": "https://example.com"})],
            tools,
            owner_work_acknowledged=True,
        )
        self.assertIsNone(later_silent)

        message_then_tool = missing_initial_work_announce(
            [
                ToolCall("say", "send_bubbles", {"bubbles": ["我先看看"]}),
                ToolCall("first", "curl", {"url": "https://example.com"}),
            ],
            tools,
            owner_work_acknowledged=False,
        )
        self.assertIsNone(message_then_tool)

    def test_keeps_native_message_argument(self) -> None:
        spec = {
            "name": "mcp__demo__post",
            "description": "demo",
            "input_schema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        }
        decorated = decorate_tool_spec(spec)
        self.assertEqual(announce_field(decorated), ANNOUNCE_FIELD)
        self.assertEqual(decorated["input_schema"]["required"], ["message"])
        self.assertEqual(
            decorated["input_schema"]["properties"]["message"],
            {"type": "string"},
        )

    def test_take_announce_message_strips_field(self) -> None:
        arguments = {"url": "https://example.com", ANNOUNCE_FIELD: "我去查一下快递"}
        text = take_announce_message(arguments, ANNOUNCE_FIELD)
        self.assertEqual(text, "我去查一下快递")
        self.assertEqual(arguments, {"url": "https://example.com"})

    def test_take_announce_message_allows_silence(self) -> None:
        arguments = {"url": "https://example.com", ANNOUNCE_FIELD: "  "}
        text = take_announce_message(arguments, ANNOUNCE_FIELD)
        self.assertIsNone(text)
        self.assertNotIn(ANNOUNCE_FIELD, arguments)

        arguments = {"url": "https://example.com"}
        text = take_announce_message(arguments, ANNOUNCE_FIELD)
        self.assertIsNone(text)
        self.assertEqual(arguments, {"url": "https://example.com"})

    def test_only_owner_specs_advertise_say_to_owner(self) -> None:
        channels = {"napcat": SimpleNamespace(name="napcat")}
        mcp = SimpleNamespace(
            tool_specs=[
                {
                    "name": "mcp__brave-search__brave_web_search",
                    OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
                    "description": "search",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]
        )
        config = SimpleNamespace(autonomy=SimpleNamespace(allowed_tools=["curl"]))
        surface = ToolSurface(config, mcp, channels, "napcat")
        owner = {
            spec["name"]: spec
            for spec in surface.owner_specs()
        }
        mcp = {
            spec["name"]: spec
            for spec in surface.mcp_server_groups()["brave-search"]
        }
        heartbeat = {spec["name"]: spec for spec in surface.self_directed_specs()}
        self.assertEqual(announce_field(owner["curl"]), ANNOUNCE_FIELD)
        self.assertEqual(announce_field(owner["goal_create"]), ANNOUNCE_FIELD)
        self.assertEqual(announce_field(owner["goal_cancel"]), ANNOUNCE_FIELD)
        self.assertEqual(
            announce_field(mcp["mcp__brave-search__brave_web_search"]),
            ANNOUNCE_FIELD,
        )
        self.assertIsNone(announce_field(owner["goal_update"]))
        self.assertIsNone(announce_field(owner["write_file"]))
        self.assertIsNone(announce_field(owner["read_file"]))
        self.assertIsNone(announce_field(heartbeat["curl"]))

    def test_announce_hook_extracts_owner_progress(self) -> None:
        arguments = {"url": "https://example.com", ANNOUNCE_FIELD: "我去搜一下"}
        text = apply_tool_announce(
            arguments,
            ANNOUNCE_FIELD,
        )
        self.assertEqual(text, "我去搜一下")
        self.assertEqual(arguments, {"url": "https://example.com"})

    def test_prompts_do_not_teach_announce_field(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "momoi" / "prompts"
        texts = [
            (root / "system.md").read_text(encoding="utf-8"),
            (root / "style_card.md").read_text(encoding="utf-8"),
            (root / "heartbeat.md").read_text(encoding="utf-8"),
            MCP_TOOL_POLICY,
        ]
        for text in texts:
            self.assertNotIn("HassTurnOn", text)
            self.assertNotIn(ANNOUNCE_FIELD, text)
            self.assertNotIn("owner_progress", text)
            self.assertNotIn("require a `message`", text)
            self.assertNotIn("Each MCP tool requires `message`", text)
