import unittest
from pathlib import Path
from types import SimpleNamespace

from momoi.builtin_tools import BUILTIN_TOOL_SPECS
from momoi.mcp_client import MCP_TOOL_POLICY
from momoi.models import ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.progress_announce import (
    ANNOUNCE_FIELD,
    ANNOUNCE_MARKER,
    announce_field,
    apply_tool_announce,
    decorate_tool_spec,
    initial_announce_error_message,
    should_announce,
    should_deliver_announce,
    take_announce_message,
)


class ProgressAnnounceTest(unittest.TestCase):
    def test_schema_policy_covers_sparse_owner_visible_locals(self) -> None:
        self.assertTrue(should_announce("curl", mcp=False))
        self.assertTrue(should_announce("goal_create", mcp=False))
        self.assertTrue(should_announce("goal_cancel", mcp=False))
        self.assertTrue(should_announce("reminder_create", mcp=False))
        self.assertTrue(should_announce("reminder_cancel", mcp=False))
        self.assertFalse(should_announce("goal_update", mcp=False))
        self.assertFalse(should_announce("goal_finish", mcp=False))
        self.assertFalse(should_announce("sleep", mcp=False))
        self.assertFalse(should_announce("read_file", mcp=False))
        self.assertFalse(should_announce("write_file", mcp=False))
        self.assertFalse(should_announce("list_dir", mcp=False))
        self.assertTrue(should_announce("mcp__brave-search__brave_web_search", mcp=True))
        self.assertTrue(should_announce("mcp__homeassistant__GetLiveContext", mcp=True))
        self.assertTrue(should_announce("mcp__homeassistant__HassTurnOn", mcp=True))

    def test_adds_say_to_owner_to_curl_schema(self) -> None:
        curl = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")
        decorated = decorate_tool_spec(curl)
        self.assertEqual(announce_field(curl), None)
        self.assertEqual(announce_field(decorated), ANNOUNCE_FIELD)
        self.assertNotIn(ANNOUNCE_FIELD, decorated["input_schema"]["required"])
        description = decorated["input_schema"]["properties"][ANNOUNCE_FIELD][
            "description"
        ]
        self.assertIn(ANNOUNCE_MARKER, description)
        self.assertIn("Soul's voice", description)
        self.assertIn("first external-work batch", description)
        self.assertIn("first such tool must include", description)
        self.assertIn("Later tool rounds may omit it", description)
        self.assertIn("not the tool's caption", description)
        self.assertIn("never narrate a retry", description)
        self.assertIn("promise an unverified outcome", description)
        self.assertIn("If this Turn has no tool result", description)
        self.assertIn("owner-visible result beat", description)
        self.assertIn("Soul-shaped", description)
        self.assertIn("new discovery or progress", description)
        self.assertIn("task need not be complete", description)
        self.assertIn("Omit it when no natural beat belongs", description)
        self.assertIn("keep provisional findings provisional", description)
        self.assertIn("never reopen the original request", description)
        self.assertIn("colon-ended label", description)
        self.assertNotIn(ANNOUNCE_FIELD, curl["input_schema"]["properties"])

    def test_initial_announce_error_explains_conditional_requirement(self) -> None:
        message = initial_announce_error_message(ANNOUNCE_FIELD)
        self.assertIn("first external-work tool batch", message)
        self.assertIn("send_message before it", message)
        self.assertIn("Later tool rounds may omit", message)

    def test_first_external_batch_requires_one_initial_acknowledgement(self) -> None:
        curl = next(spec for spec in BUILTIN_TOOL_SPECS if spec["name"] == "curl")
        tools = [decorate_tool_spec(curl)]
        missing = MomoiDaemon._missing_initial_work_announce(
            [ToolCall("first", "curl", {"url": "https://example.com"})],
            tools,
            owner_work_acknowledged=False,
            deliver=True,
        )
        self.assertEqual(missing, ("first", ANNOUNCE_FIELD))

        announced = MomoiDaemon._missing_initial_work_announce(
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
            deliver=True,
        )
        self.assertIsNone(announced)

        later_silent = MomoiDaemon._missing_initial_work_announce(
            [ToolCall("later", "curl", {"url": "https://example.com"})],
            tools,
            owner_work_acknowledged=True,
            deliver=True,
        )
        self.assertIsNone(later_silent)

        message_then_tool = MomoiDaemon._missing_initial_work_announce(
            [
                ToolCall("say", "send_message", {"messages": ["我先看看"]}),
                ToolCall("first", "curl", {"url": "https://example.com"}),
            ],
            tools,
            owner_work_acknowledged=False,
            deliver=True,
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
        text, error = take_announce_message(arguments, ANNOUNCE_FIELD)
        self.assertEqual(text, "我去查一下快递")
        self.assertIsNone(error)
        self.assertEqual(arguments, {"url": "https://example.com"})

    def test_take_announce_message_allows_silence(self) -> None:
        arguments = {"url": "https://example.com", ANNOUNCE_FIELD: "  "}
        text, error = take_announce_message(arguments, ANNOUNCE_FIELD)
        self.assertIsNone(text)
        self.assertIsNone(error)
        self.assertNotIn(ANNOUNCE_FIELD, arguments)

        arguments = {"url": "https://example.com"}
        text, error = take_announce_message(arguments, ANNOUNCE_FIELD)
        self.assertIsNone(text)
        self.assertIsNone(error)
        self.assertEqual(arguments, {"url": "https://example.com"})

    def test_owner_specs_advertise_say_to_owner_heartbeat_specs_do_not(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.channels = {"napcat": SimpleNamespace(name="napcat")}
        daemon.channel = daemon.channels["napcat"]
        daemon.mcp = SimpleNamespace(
            tool_specs=[
                {
                    "name": "mcp__brave-search__brave_web_search",
                    "description": "search",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]
        )
        daemon.config = SimpleNamespace(autonomy=SimpleNamespace(allowed_tools=["curl"]))
        owner = {spec["name"]: spec for spec in daemon._owner_tool_specs({})}
        heartbeat = {spec["name"]: spec for spec in daemon._self_directed_tool_specs()}
        self.assertEqual(announce_field(owner["curl"]), ANNOUNCE_FIELD)
        self.assertEqual(announce_field(owner["goal_create"]), ANNOUNCE_FIELD)
        self.assertEqual(announce_field(owner["reminder_create"]), ANNOUNCE_FIELD)
        self.assertEqual(announce_field(owner["goal_cancel"]), ANNOUNCE_FIELD)
        self.assertEqual(announce_field(owner["reminder_cancel"]), ANNOUNCE_FIELD)
        self.assertEqual(
            announce_field(owner["mcp__brave-search__brave_web_search"]),
            ANNOUNCE_FIELD,
        )
        self.assertIsNone(announce_field(owner["goal_update"]))
        self.assertIsNone(announce_field(owner["write_file"]))
        self.assertIsNone(announce_field(owner["read_file"]))
        self.assertIsNone(announce_field(heartbeat["curl"]))

    def test_harness_never_delivers_on_heartbeat_or_autonomous_turns(self) -> None:
        self.assertFalse(
            should_deliver_announce(heartbeat_turn=True, reply_wait_turn=False)
        )
        self.assertFalse(
            should_deliver_announce(heartbeat_turn=False, reply_wait_turn=True)
        )
        self.assertFalse(
            should_deliver_announce(
                heartbeat_turn=False, reply_wait_turn=False, autonomous_goal=True
            )
        )
        self.assertTrue(
            should_deliver_announce(heartbeat_turn=False, reply_wait_turn=False)
        )

    def test_heartbeat_strips_say_to_owner_without_delivering(self) -> None:
        arguments = {"url": "https://example.com", ANNOUNCE_FIELD: "我去搜一下"}
        text, error = apply_tool_announce(
            arguments,
            ANNOUNCE_FIELD,
            deliver=should_deliver_announce(
                heartbeat_turn=True, reply_wait_turn=False
            ),
        )
        self.assertIsNone(text)
        self.assertIsNone(error)
        self.assertEqual(arguments, {"url": "https://example.com"})

    def test_owner_turn_returns_optional_say_to_owner(self) -> None:
        arguments = {"url": "https://example.com", ANNOUNCE_FIELD: "我去搜一下"}
        text, error = apply_tool_announce(
            arguments,
            ANNOUNCE_FIELD,
            deliver=should_deliver_announce(
                heartbeat_turn=False, reply_wait_turn=False
            ),
        )
        self.assertEqual(text, "我去搜一下")
        self.assertIsNone(error)
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
