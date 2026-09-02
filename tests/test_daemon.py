import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import (
    AppConfig,
    EpisodeAnnealingConfig,
    HeartbeatConfig,
    LLMConfig,
    NotificationConfig,
)
from momoi.runtime import (
    END_TURN_TOOL_SPEC,
    SEND_BUBBLES_TOOL_SPEC,
    heartbeat_end_turn_tool_spec,
    owner_end_turn_tool_spec,
    MomoiDaemon,
)
from momoi.runtime.jobs import AutonomousJob
from momoi.runtime.agent import TurnExecutionSpec
from momoi.runtime.agent.context_window import ContextWindow
from momoi.runtime.agent.tool_surface import ToolSurface
from momoi.runtime.tool_contracts.conversation import (
    ACTIVITY_DECISION_SCHEMA,
    CHANNEL_BUBBLE_SCHEMA,
    MOOD_UPDATE_SCHEMA,
)
from momoi.models import (
    AgentReply,
    IncomingMessage,
    OwnerInputStatus,
    ProviderResponse,
    ToolCall,
    TurnDraft,
)
from momoi.llm.errors import (
    ProviderError,
)
from momoi.runtime.turn_support import (
    OWNER_PROMPT_PATH,
    SYSTEM_PROMPT_PATH,
    STYLE_CARD_SYSTEM_PROMPT,
)
from momoi.runtime.agent.protocol import (
    OWNER_BUBBLE_REQUEST_REMINDER,
    owner_request_messages,
)
from momoi.runtime.parsing import parse_mood_decision, parse_mood_update
from momoi.runtime.dispatch.delivery import message_gap_bounds
from momoi.runtime.turn_support import REPLY_WAIT_SYSTEM_PROMPT
from momoi.storage import estimate_tokens
from tests.support import (
    recall_response,
    with_owner_recall,
)


def context_window(config: object) -> ContextWindow:
    return ContextWindow(
        config,
        None,
        SimpleNamespace(refit=lambda _value, max_chars: None),
    )


class DaemonTest(unittest.TestCase):
    def test_system_contract_allows_only_native_tool_calls_globally(self) -> None:
        contract = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("The assistant has no text output channel", contract)
        self.assertIn("Every Turn advances only through", contract)
        self.assertIn("call it with the exact bubbles", contract)
        self.assertIn("DSML", contract)
        self.assertNotIn("Owner Turn state machine", contract)
        self.assertNotIn("recall first", contract)

    def test_owner_contract_owns_recall_state_machine(self) -> None:
        contract = OWNER_PROMPT_PATH.read_text(encoding="utf-8")

        self.assertIn("# Owner Turn contract", contract)
        self.assertIn("Call `recall` first and alone", contract)
        self.assertIn("If owner-visible bubbles are warranted", contract)
        self.assertIn("otherwise do not call it", contract)
        self.assertIn("call `end_turn` alone", contract)

    def test_owner_request_bubble_reminder_is_wire_only(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "tool_result", "content": "{}"},
                    {"type": "text", "text": "last"},
                ],
            }
        ]

        request = owner_request_messages(messages, remind_bubbles=True)

        self.assertEqual(messages[0]["content"][-1]["text"], "last")
        self.assertEqual(request[0]["content"][0]["text"], "first")
        self.assertEqual(
            request[0]["content"][-1]["text"],
            f"last\n\n{OWNER_BUBBLE_REQUEST_REMINDER}",
        )
        self.assertFalse(OWNER_BUBBLE_REQUEST_REMINDER.startswith("["))
        self.assertFalse(OWNER_BUBBLE_REQUEST_REMINDER.endswith("]"))
        self.assertIn("call send_bubbles with them", OWNER_BUBBLE_REQUEST_REMINDER)

    def test_owner_request_bubble_reminder_follows_tool_results(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "content": "{}"}],
            }
        ]

        request = owner_request_messages(messages, remind_bubbles=True)

        self.assertEqual(len(messages[0]["content"]), 1)
        self.assertEqual(
            request[0]["content"][-1],
            {"type": "text", "text": OWNER_BUBBLE_REQUEST_REMINDER},
        )

    def test_message_gap_scales_with_length_within_bounds(self) -> None:
        self.assertEqual(message_gap_bounds("短句"), (4.0, 5.0))
        self.assertEqual(message_gap_bounds("中等长度" * 8), (5.0, 6.0))
        self.assertEqual(message_gap_bounds("长消息" * 30), (6.0, 7.0))

    def test_system_tool_policies_follow_available_tools(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(
            system_prompt="{{SOUL}}\n{{CAPABILITY_POLICIES}}",
            soul_prompt="Test soul",
            soul_prompt_path=None,
        )
        daemon._loaded_workspace_prompts = {}
        daemon.mcp = SimpleNamespace(tool_specs=[])
        daemon.store = SimpleNamespace(emotion_context=lambda token_budget=4000: "")

        base = daemon._system()
        specialized = daemon._system_with_tool_policies(
            base, [{"name": "send_bubbles"}, {"name": "memory_remember"}]
        )
        owner = daemon._system_with_tool_policies(
            base,
            [
                {"name": "memory_search"},
                {"name": "curl"},
                {"name": "goal_create"},
            ],
        )

        self.assertEqual(len(specialized), 2)
        self.assertEqual(specialized[0], base[0])
        self.assertIn("Memory tools", specialized[1]["text"])
        self.assertIn("judgment, not a reflex", specialized[1]["text"])
        self.assertEqual(len(owner), 2)
        self.assertNotIn("Memory tools", owner[1]["text"])
        self.assertNotIn("Built-in runtime tools", owner[1]["text"])
        self.assertIn("Agenda tools", owner[1]["text"])

    def test_heartbeat_self_directed_tools_allow_configured_mcp_effects(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(
            autonomy=SimpleNamespace(
                allowed_tools=(
                    "list_dir",
                    "mcp__homeassistant*",
                )
            )
        )
        daemon.mcp = SimpleNamespace(
            tool_specs=[
                {"name": "mcp__homeassistant__GetLiveContext"},
                {"name": "mcp__homeassistant__HassTurnOn"},
            ]
        )
        surface = ToolSurface(daemon.config, daemon.mcp, {}, "napcat")
        names = {spec["name"] for spec in surface.self_directed_specs()}
        self.assertEqual(
            names,
            {
                "list_dir",
                "mcp__homeassistant__GetLiveContext",
                "mcp__homeassistant__HassTurnOn",
            },
        )

    def test_emotion_catalog_is_cached_system_block(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(
            system_prompt="You are Momoi.",
            soul_prompt="Soul",
            soul_prompt_path=None,
        )
        daemon._loaded_workspace_prompts = {}
        daemon.mcp = SimpleNamespace(tool_specs=[])
        daemon.store = SimpleNamespace(
            emotion_context=lambda token_budget=4000: "- slug=hello meaning=hi"
        )

        blocks = daemon._system()
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(blocks[1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("<emotion_catalog>", blocks[1]["text"])
        self.assertIn("slug=hello", blocks[1]["text"])
        self.assertNotIn("<emotion_catalog>", blocks[0]["text"])

    def test_workspace_prompts_hot_reload_between_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            soul = root / "SOUL.md"
            heartbeat = root / "HEARTBEAT.md"
            soul.write_text("Old soul")
            heartbeat.write_text("Old heartbeat")
            daemon = object.__new__(MomoiDaemon)
            daemon.config = SimpleNamespace(
                system_prompt="{{SOUL}}\n{{CAPABILITY_POLICIES}}",
                soul_prompt="Old soul",
                soul_prompt_path=soul,
                heartbeat_prompt="Old heartbeat",
                heartbeat_prompt_path=heartbeat,
            )
            daemon._loaded_workspace_prompts = {}
            daemon.mcp = SimpleNamespace(tool_specs=[])
            daemon.store = SimpleNamespace(emotion_context=lambda token_budget=4000: "")

            self.assertIn("Old soul", daemon._system()[0]["text"])
            self.assertIn("Old heartbeat", daemon._heartbeat_system_prompt())
            soul.write_text("New soul")
            heartbeat.write_text("New heartbeat")
            self.assertIn("New soul", daemon._system()[0]["text"])
            rendered = daemon._heartbeat_system_prompt()
            self.assertIn("New heartbeat", rendered)
            self.assertLess(
                rendered.index("Call `heartbeat_begin` first"),
                rendered.index("# Workspace heartbeat guidance"),
            )
            self.assertNotIn("heartbeat_handoff", rendered)
            self.assertIn("A `rest` activity is complete", rendered)
            self.assertIn("follow the shared Style Card", rendered)
            self.assertNotIn("<recalled_turns>", rendered)
            heartbeat.unlink()
            self.assertNotIn(
                "# Workspace heartbeat guidance", daemon._heartbeat_system_prompt()
            )

            daemon._loaded_workspace_prompts = {}
            heartbeat.write_text("偶尔整理自己的摄影兴趣。")
            with self.assertLogs("momoi.runtime.prompt_renderer", level="INFO") as logs:
                self.assertIn("摄影兴趣", daemon._workspace_heartbeat_guidance())
            self.assertTrue(
                any("workspace_prompt_loaded" in message for message in logs.output)
            )
            heartbeat.unlink()
            with self.assertLogs("momoi.runtime.prompt_renderer", level="INFO") as logs:
                self.assertEqual(daemon._workspace_heartbeat_guidance(), "")
            self.assertTrue(
                any("workspace_prompt_missing" in message for message in logs.output)
            )

            daemon._loaded_workspace_prompts = {}
            with self.assertLogs("momoi.runtime.prompt_renderer", level="INFO") as logs:
                self.assertIn("New soul", daemon._workspace_soul())
            self.assertTrue(
                any("workspace_prompt_loaded" in message for message in logs.output)
            )
            soul.write_text("Changed soul")
            with self.assertLogs("momoi.runtime.prompt_renderer", level="INFO") as logs:
                self.assertIn("Changed soul", daemon._workspace_soul())
            self.assertTrue(
                any("workspace_prompt_loaded" in message for message in logs.output)
            )
            self.assertEqual(daemon._workspace_soul(), "Changed soul")
            self.assertEqual(
                daemon._loaded_workspace_prompts["soul"],
                f"{soul}\0Changed soul",
            )

    def test_shared_style_card_is_injected(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(
            system_prompt="{{STYLE_CARD}}",
            soul_prompt="Test soul",
            soul_prompt_path=None,
        )
        daemon._loaded_workspace_prompts = {}
        daemon.mcp = SimpleNamespace(tool_specs=[])
        daemon.store = SimpleNamespace(emotion_context=lambda token_budget=4000: "")

        self.assertEqual(daemon._system()[0]["text"], STYLE_CARD_SYSTEM_PROMPT)

    def test_style_card_allows_standalone_non_propositional_speech(self) -> None:
        self.assertIn(
            "items passed to\n`send_bubbles.bubbles`", STYLE_CARD_SYSTEM_PROMPT
        )
        self.assertIn(
            "producing\nbubbles means calling `send_bubbles`",
            STYLE_CARD_SYSTEM_PROMPT,
        )
        self.assertIn("never creates a text output path", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn(
            "standalone sticker or reaction image", STYLE_CARD_SYSTEM_PROMPT
        )
        self.assertIn("quiet end", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn(
            "Do not add a bubble merely to prove it was noticed",
            STYLE_CARD_SYSTEM_PROMPT,
        )
        self.assertIn(
            "only accepts or closes a beat that already landed",
            STYLE_CARD_SYSTEM_PROMPT,
        )
        self.assertIn(
            "merely plausible\n  or warm extra bubble",
            STYLE_CARD_SYSTEM_PROMPT,
        )
        self.assertIn("ordinary social chat", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("one short utterance", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("its own bubble, in\n  the order it comes", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn(
            "begin with the immediate conversational beat",
            STYLE_CARD_SYSTEM_PROMPT,
        )
        self.assertIn("Form describes the whole bubble", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("consists only of affect", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("starts a thought", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("even if\n  it begins expressively", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("not a half-beat", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("the two half-beat forms", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("Bubble boundaries express timing", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("produces a\n  wordless reaction", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("before, between, or after", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("exactly where that impulse occurs", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("require separate semantic jobs", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("freely mix fragments", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("preserve it as its own", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("rewriting it into a fuller", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("not complete, justify, or balance", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("completeness as a choice", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("do not repair it", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("impulse genuinely arrives that way", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn(
            "open\n  with what you actually have to say", STYLE_CARD_SYSTEM_PROMPT
        )
        self.assertIn("## Result beats", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("before the task is complete", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("novel and relevant to the owner", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("stand alone as one chat bubble", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("exploratory misses may stay silent", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("dramatic about your feeling", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("intermediate findings provisional", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("## Emotional presence", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("not a neutral answering surface", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("sulky", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("ask for comfort", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn("remain unresolved", STYLE_CARD_SYSTEM_PROMPT)
        self.assertNotIn("Use one short beat by default", STYLE_CARD_SYSTEM_PROMPT)
        self.assertIn(
            "explicit request genuinely depends on its content",
            STYLE_CARD_SYSTEM_PROMPT,
        )

    def test_reply_wait_prompt_explains_the_current_state_machine(self) -> None:
        system = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "momoi"
            / "prompts"
            / "system.md"
        ).read_text(encoding="utf-8")
        wait_schema = END_TURN_TOOL_SPEC["input_schema"]["properties"]["reply_wait"]
        self.assertIn("genuinely expected", wait_schema["description"])
        self.assertIn("one follow-up Turn", wait_schema["description"])
        self.assertIn("<interrupted_reply_expectation>", system)
        self.assertIn("describes a cancelled wait", system)
        self.assertIn("follow-up must be sent now", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("reconsider contact", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("Send a brief", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("native transcript", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertNotIn("<reply_timeline>", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("Continue strictly after its", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("never answer, confirm, or", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("<followup>", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("After the `send_bubbles` result", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("alone on the next step", REPLY_WAIT_SYSTEM_PROMPT)
        self.assertIn("`reply_wait.wait` to false", REPLY_WAIT_SYSTEM_PROMPT)

    def test_mood_update_parser_accepts_open_state_labels(self) -> None:
        mood, error = parse_mood_update(
            {
                "state": "angry",
                "intensity": 0.8,
                "cause": "test",
            }
        )
        self.assertEqual(mood["state"], "angry")
        self.assertIsNone(error)
        mood, error = parse_mood_update(
            {
                "state": "very angry!",
                "intensity": 0.8,
                "cause": "test",
            }
        )
        self.assertIsNone(mood)
        self.assertEqual(error, "invalid_mood_update")
        state_schema = MOOD_UPDATE_SCHEMA["properties"]["state"]
        self.assertNotIn("enum", state_schema)
        self.assertEqual(state_schema["pattern"], "^[a-z][a-z0-9_-]{0,31}$")

    def test_mood_decision_is_explicit_in_terminal_tools(self) -> None:
        mood, error = parse_mood_decision({"decision": "unchanged"})
        self.assertIsNone(mood)
        self.assertIsNone(error)
        mood, error = parse_mood_decision(
            {
                "decision": "updated",
                "state": "excited",
                "intensity": 0.8,
                "cause": "完成新能力接入",
            }
        )
        self.assertEqual(mood["state"], "excited")
        self.assertIsNone(error)
        self.assertEqual(
            parse_mood_decision(None),
            (None, "invalid_mood_decision"),
        )
        self.assertEqual(
            parse_mood_decision('{"decision": "unchanged"}'),
            (None, "invalid_mood_decision"),
        )
        self.assertIn("mood", END_TURN_TOOL_SPEC["input_schema"]["required"])
        self.assertIn("reply_wait", END_TURN_TOOL_SPEC["input_schema"]["required"])
        self.assertNotIn("continuity", END_TURN_TOOL_SPEC["input_schema"]["properties"])
        self.assertNotIn("delivery", END_TURN_TOOL_SPEC["input_schema"]["properties"])
        self.assertNotIn("expects_reply", END_TURN_TOOL_SPEC["input_schema"]["properties"])
        self.assertNotIn("segments", END_TURN_TOOL_SPEC["input_schema"]["properties"])
        self.assertNotIn("forward", END_TURN_TOOL_SPEC["input_schema"]["properties"])
        self.assertNotIn(
            "delivery", SEND_BUBBLES_TOOL_SPEC["input_schema"]["properties"]
        )
        self.assertIn("owner-visible", SEND_BUBBLES_TOOL_SPEC["description"])
        self.assertIn(
            "Produces owner-visible bubbles", SEND_BUBBLES_TOOL_SPEC["description"]
        )
        self.assertIn(
            "assistant text delivers none",
            SEND_BUBBLES_TOOL_SPEC["description"],
        )
        self.assertIn("next step", SEND_BUBBLES_TOOL_SPEC["description"])
        self.assertIn("must stand alone", SEND_BUBBLES_TOOL_SPEC["description"])
        bubble_description = CHANNEL_BUBBLE_SCHEMA["oneOf"][0]["description"]
        self.assertIn("item in send_bubbles.bubbles", bubble_description)
        self.assertIn("means calling send_bubbles", bubble_description)
        self.assertIn("non-empty", bubble_description)
        self.assertIn("blank lines", bubble_description)
        self.assertIn("emotion://<listed-slug>", bubble_description)
        self.assertIn("standalone reaction image", bubble_description)
        wait_shapes = END_TURN_TOOL_SPEC["input_schema"]["properties"]["reply_wait"][
            "oneOf"
        ]
        wait_schema = END_TURN_TOOL_SPEC["input_schema"]["properties"]["reply_wait"]
        self.assertIn("another scheduler owns the work", wait_schema["description"])
        self.assertIn("one follow-up Turn", wait_schema["description"])
        self.assertEqual(
            [shape["properties"]["wait"]["enum"][0] for shape in wait_shapes],
            [False, True],
        )
        self.assertEqual(
            wait_shapes[1]["properties"]["delay_minutes"]["minimum"], 1
        )
        self.assertEqual(
            wait_shapes[1]["properties"]["delay_minutes"]["maximum"], 10
        )
        self.assertIn("conversational Turn", END_TURN_TOOL_SPEC["description"])
        terminal_properties = END_TURN_TOOL_SPEC["input_schema"]["properties"]
        for visible_field in ("message", "messages", "text", "content", "delivery"):
            self.assertNotIn(visible_field, terminal_properties)
        self.assertFalse(END_TURN_TOOL_SPEC["input_schema"]["additionalProperties"])
        heartbeat_end_turn = heartbeat_end_turn_tool_spec()
        self.assertEqual(heartbeat_end_turn["name"], "end_turn")
        self.assertIn("heartbeat", heartbeat_end_turn["input_schema"]["required"])
        self.assertIn("mood", heartbeat_end_turn["input_schema"]["required"])
        self.assertNotIn(
            "continue_waiting_for_reply",
            heartbeat_end_turn["input_schema"]["properties"]["heartbeat"]["properties"],
        )
        owner_end_turn = owner_end_turn_tool_spec()
        self.assertIn("activity", owner_end_turn["input_schema"]["required"])
        self.assertNotIn("heartbeat", owner_end_turn["input_schema"]["properties"])
        activity_shapes = ACTIVITY_DECISION_SCHEMA["oneOf"]
        self.assertEqual(
            [shape["properties"]["decision"]["enum"][0] for shape in activity_shapes],
            ["unchanged", "updated"],
        )
        self.assertEqual(
            set(activity_shapes[1]["required"]),
            {"decision", "text", "result"},
        )
        self.assertIn("Replace", activity_shapes[1]["description"])
        owner_description = owner_end_turn["description"]
        self.assertIn("Terminal action", owner_description)
        self.assertIn("on the next step", owner_description)
        self.assertIn("Correct it only when", owner_description)
        self.assertIn("is not a conflict", owner_description)
        self.assertIn("without a conflict, leave both unchanged", owner_description)
        self.assertNotIn("Momoi", owner_description)

    def test_context_budget_drops_old_history_and_truncates_tool_results(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(max_input_tokens=5000)
        messages = [
            {"role": "user", "content": "旧历史" * 4000},
            {"role": "user", "content": "当前消息"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "large-result",
                        "name": "read_file",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "large-result",
                        "content": "结果" * 12000,
                    }
                ],
            },
        ]
        remaining = context_window(daemon.config).fit(
            [{"type": "text", "text": "system"}], messages, [], 1
        )
        estimated = estimate_tokens(json.dumps(messages, ensure_ascii=False))
        self.assertEqual(remaining, 0)
        self.assertEqual(messages[0]["content"], "当前消息")
        self.assertLessEqual(estimated, 5000)

    def test_context_budget_compacts_at_configured_ratio(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(
            max_input_tokens=10000,
            context_compaction_ratio=0.5,
        )
        messages = [
            {"role": "user", "content": "旧历史" * 2000},
            {"role": "user", "content": "当前消息"},
        ]

        remaining = context_window(daemon.config).fit(
            [{"type": "text", "text": "system"}], messages, [], 1
        )

        self.assertEqual(remaining, 0)
        self.assertEqual(messages, [{"role": "user", "content": "当前消息"}])

    def test_context_budget_breaks_expanding_compression_and_keeps_going(
        self,
    ) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(max_input_tokens=100)
        expanding = "bad" * 400
        compressible = "good" * 400
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": expanding},
                    {"type": "tool_result", "content": compressible},
                ],
            }
        ]

        def truncate(value: str, _limit: int) -> str:
            return value + "!" if value.startswith("bad") else '{"ok":true}'

        with (
            patch(
                "momoi.runtime.agent.context_window.truncate_tool_result_json",
                side_effect=truncate,
            ) as truncator,
            self.assertLogs("momoi.runtime.turns", level="WARNING") as logs,
        ):
            remaining = context_window(daemon.config).fit(
                [{"type": "text", "text": "system"}],
                messages,
                [],
                0,
            )

        self.assertEqual(remaining, 0)
        self.assertEqual(messages[0]["content"][0]["content"], expanding)
        self.assertEqual(
            messages[0]["content"][1]["content"], '{"ok":true}'
        )
        self.assertEqual(truncator.call_count, 2)
        self.assertTrue(
            any(
                "tool_result_truncation_stalled" in message
                for message in logs.output
            )
        )
        self.assertTrue(
            any("llm_context_oversize" in message for message in logs.output)
        )

    def test_context_budget_breaks_real_tool_result_expansion(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(max_input_tokens=100)
        result = json.dumps(
            {
                "ok": False,
                "error": "mcp_tool_error",
                "truncated": False,
                "provenance": {
                    "source": "mcp",
                    "tool": "mcp__brave-search__brave_web_search",
                },
                "result": {
                    "content": [
                        {"type": "text", "text": "x" * 389},
                    ],
                    "isError": True,
                },
                "message": "x" * 390,
            }
        )
        messages = [
            {
                "role": "user",
                "content": [{"type": "tool_result", "content": result}],
            }
        ]

        with self.assertLogs("momoi.runtime.turns", level="WARNING") as logs:
            remaining = context_window(daemon.config).fit(
                [{"type": "text", "text": "system"}],
                messages,
                [],
                0,
            )

        self.assertEqual(remaining, 0)
        self.assertEqual(messages[0]["content"][0]["content"], result)
        self.assertTrue(
            any(
                "tool_result_truncation_stalled" in message
                for message in logs.output
            )
        )

    def test_context_budget_caps_compression_attempts(self) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(max_input_tokens=100)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "界" * 1100},
                ],
            }
        ]
        with (
            patch(
                "momoi.runtime.agent.context_window.truncate_tool_result_json",
                side_effect=lambda value, _limit: value[:-1],
            ) as truncator,
            self.assertLogs("momoi.runtime.turns", level="WARNING") as logs,
        ):
            context_window(daemon.config).fit(
                [{"type": "text", "text": "system"}],
                messages,
                [],
                0,
            )

        self.assertEqual(truncator.call_count, 16)
        self.assertEqual(len(messages[0]["content"][0]["content"]), 1084)
        self.assertTrue(
            any(
                "tool_result_truncation_stalled" in message
                for message in logs.output
            )
        )

    def test_context_budget_allows_uncompressible_single_turn_oversize(
        self,
    ) -> None:
        daemon = object.__new__(MomoiDaemon)
        daemon.config = SimpleNamespace(max_input_tokens=100)
        messages = [{"role": "user", "content": "current" * 1000}]

        with self.assertLogs("momoi.runtime.turns", level="WARNING") as logs:
            remaining = context_window(daemon.config).fit(
                [{"type": "text", "text": "system"}],
                messages,
                [],
                0,
            )

        self.assertEqual(remaining, 0)
        self.assertEqual(messages[0]["content"], "current" * 1000)
        self.assertTrue(
            any(
                "llm_context_oversize" in message for message in logs.output
            )
        )


class DaemonAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_input_status_extends_open_owner_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 0.1, 1, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            stop = asyncio.Event()
            completed = asyncio.Event()
            captured: list[IncomingMessage] = []

            async def complete(batch: list[IncomingMessage], *_: object) -> None:
                captured.extend(batch)
                completed.set()
                stop.set()

            daemon._complete_batch_turn = complete  # type: ignore[method-assign]
            worker = asyncio.create_task(daemon._agent_worker(stop))
            first = IncomingMessage(
                "napcat:first", "first", "第一条", 1, 1, channel="napcat"
            )
            await daemon._receive(first)
            first_deadline = daemon._owner_quiet_until["napcat"]
            await asyncio.sleep(0.075)
            await daemon._receive(OwnerInputStatus("napcat"))
            extended_deadline = daemon._owner_quiet_until["napcat"]
            self.assertGreater(extended_deadline, first_deadline)
            self.assertLess(extended_deadline - first_deadline, 0.075)
            await asyncio.sleep(
                max(0.0, first_deadline - asyncio.get_running_loop().time() + 0.01)
            )
            self.assertFalse(completed.is_set())
            await asyncio.wait_for(completed.wait(), timeout=0.2)
            await worker

            self.assertEqual(captured, [first])
            daemon.store.close()

    async def test_input_status_holds_stale_reply_for_owner_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 0.05, 1, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            stale_reply_started = asyncio.Event()
            finish_stale_reply = asyncio.Event()
            stale_reply_cancelled = asyncio.Event()

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _: object,
                    messages: list[dict[str, object]],
                    __: object,
                    **___: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    if provider_self.calls == 1:
                        stale_reply_started.set()
                        try:
                            await finish_stale_reply.wait()
                        except asyncio.CancelledError:
                            stale_reply_cancelled.set()
                            raise
                        text = "只回应第一条"
                        tool_name = "send_bubbles"
                        arguments = {"bubbles": [text]}
                    elif provider_self.calls == 2:
                        self.assertIn(
                            "第二条", json.dumps(messages, ensure_ascii=False)
                        )
                        text = "合并两条后回复"
                        tool_name = "send_bubbles"
                        arguments = {"bubbles": [text]}
                    else:
                        tool_name = "end_turn"
                        arguments = {
                            "reply_wait": {"wait": False},
                            "mood": {"decision": "unchanged"},
                            "activity": {"decision": "unchanged"},
                        }
                    call = ToolCall(
                        f"end_turn-{provider_self.calls}",
                        tool_name,
                        arguments,
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

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            first = IncomingMessage(
                "napcat:first", "first", "第一条", 1, 1, channel="napcat"
            )
            daemon.store.add_event(first)
            turn_id = daemon._turn_id(first.event_id)
            turn = asyncio.create_task(
                daemon._complete_batch_turn([first], asyncio.Event(), turn_id)
            )

            await stale_reply_started.wait()
            await daemon._receive(OwnerInputStatus("napcat"))
            await asyncio.sleep(0.01)
            self.assertFalse(stale_reply_cancelled.is_set())
            second = IncomingMessage(
                "napcat:second", "second", "第二条", 2, 2, channel="napcat"
            )
            await daemon._receive(second)
            await asyncio.wait_for(stale_reply_cancelled.wait(), timeout=1)
            await asyncio.wait_for(turn, timeout=1)

            self.assertEqual(provider.calls, 3)
            self.assertEqual(
                [row.text for row in daemon.store.due_outbox()], ["合并两条后回复"]
            )
            self.assertTrue(daemon.episode_annealing_requested.is_set())
            self.assertEqual(daemon.store.pending_events(), [])
            daemon.store.close()

    async def test_owner_updates_interrupt_after_tool_and_before_end_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            tool_started = asyncio.Event()
            finish_tool = asyncio.Event()
            stale_end_turn_started = asyncio.Event()
            finish_stale_end_turn = asyncio.Event()
            stale_end_turn_cancelled = asyncio.Event()

            async def execute_tool(call: ToolCall) -> dict[str, object]:
                self.assertEqual(call.name, "read_file")
                tool_started.set()
                await finish_tool.wait()
                return {"ok": True, "content": "旧地址天气"}

            daemon.builtin_tools.execute = execute_tool  # type: ignore[method-assign]

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _: object,
                    messages: list[dict[str, object]],
                    __: object,
                    **___: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    if provider_self.calls == 1:
                        call = ToolCall(
                            "weather-read", "read_file", {"path": "weather.txt"}
                        )
                    elif provider_self.calls == 2:
                        self.assertIn(
                            "地址改成上海", json.dumps(messages, ensure_ascii=False)
                        )
                        stale_end_turn_started.set()
                        try:
                            await finish_stale_end_turn.wait()
                        except asyncio.CancelledError:
                            stale_end_turn_cancelled.set()
                            raise
                        call = ToolCall(
                            "stale-message",
                            "send_bubbles",
                            {"bubbles": ["上海天气晴"]},
                        )
                    elif provider_self.calls == 3:
                        rendered = json.dumps(messages, ensure_ascii=False)
                        self.assertIn("不用查天气了", rendered)
                        call = ToolCall(
                            "final-message",
                            "send_bubbles",
                            {"bubbles": ["收到，不查了"]},
                        )
                    else:
                        call = ToolCall(
                            "final-end_turn",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            initial = IncomingMessage("qq:turn:1", "1", "查旧地址天气", 1, 1)
            first_update = IncomingMessage("qq:turn:2", "2", "地址改成上海", 2, 2)
            second_update = IncomingMessage(
                "qq:turn:3", "3", "不用查天气了，只告诉我你收到了", 3, 3
            )
            daemon.store.add_event(initial)
            turn_id = daemon._turn_id(initial.event_id)
            turn = asyncio.create_task(
                daemon._complete_batch_turn([initial], asyncio.Event(), turn_id)
            )

            await tool_started.wait()
            await daemon._receive(first_update)
            finish_tool.set()
            await stale_end_turn_started.wait()
            await daemon._receive(second_update)
            await asyncio.wait_for(stale_end_turn_cancelled.wait(), timeout=1)
            await turn

            self.assertEqual(provider.calls, 4)
            self.assertTrue(daemon.incoming.empty())
            self.assertEqual(
                [row.text for row in daemon.store.due_outbox()], ["收到，不查了"]
            )
            stored = daemon.store._db.execute(
                "SELECT content, source_event_ids_json FROM messages WHERE role='user'"
            ).fetchone()
            self.assertIn("地址改成上海", stored["content"])
            self.assertIn("不用查天气了", stored["content"])
            self.assertEqual(
                json.loads(stored["source_event_ids_json"]),
                [initial.event_id, first_update.event_id, second_update.event_id],
            )
            stored_turn = daemon.store._db.execute(
                "SELECT source_ids_json FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(
                json.loads(stored_turn["source_ids_json"]),
                [initial.event_id, first_update.event_id, second_update.event_id],
            )
            plans = daemon.store._db.execute(
                """SELECT revision, state FROM context_plans
                   WHERE turn_id=? ORDER BY revision""",
                (turn_id,),
            ).fetchall()
            self.assertEqual(
                [(row["revision"], row["state"]) for row in plans],
                [(1, "superseded"), (2, "superseded"), (3, "recalled")],
            )
            self.assertEqual(daemon.store.pending_events(), [])
            daemon.store.close()

    async def test_episode_annealing_is_cancelled_for_new_owner_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 0.01, 1, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                    episode_annealing=EpisodeAnnealingConfig(
                        idle_seconds=0.05,
                        max_seconds=1,
                    ),
                )
            )
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def anneal(**_: object) -> bool:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            daemon._run_episode_annealing_once = anneal  # type: ignore[method-assign]
            daemon._touch_owner_activity("napcat")
            worker = asyncio.create_task(
                daemon._episode_annealing_worker(asyncio.Event())
            )
            daemon.episode_annealing_requested.set()
            await asyncio.sleep(0.01)
            self.assertFalse(started.is_set())
            await started.wait()

            await daemon._receive(
                IncomingMessage(
                    "owner-update",
                    "owner-update",
                    "新消息优先",
                    1,
                    1,
                    channel="napcat",
                )
            )
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            for _ in range(10):
                if daemon._active_annealing is None:
                    break
                await asyncio.sleep(0)
            self.assertIsNone(daemon._active_annealing)
            self.assertEqual((await daemon.incoming.get()).text, "新消息优先")

            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
            daemon.store.close()

    async def test_failed_episode_annealing_does_not_delay_other_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 0.01, 1, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            stop = asyncio.Event()
            second_run = asyncio.Event()
            calls = 0

            async def ready(_stop: asyncio.Event) -> bool:
                return False

            async def anneal(**_: object) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("first candidate failed")
                second_run.set()
                stop.set()
                return False

            daemon._wait_for_episode_annealing_ready = ready  # type: ignore[method-assign]
            daemon._run_episode_annealing_once = anneal  # type: ignore[method-assign]
            worker = asyncio.create_task(daemon._episode_annealing_worker(stop))
            daemon.episode_annealing_requested.set()

            await asyncio.wait_for(second_run.wait(), timeout=1)
            await asyncio.wait_for(worker, timeout=1)

            self.assertEqual(calls, 2)
            daemon.store.close()

    async def test_manual_heartbeat_command_queues_once_even_when_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            command = IncomingMessage(
                "qq:manual-heartbeat",
                "manual-heartbeat",
                "/heartbeat",
                1,
                1,
                channel="napcat",
            )
            await daemon._receive(command)
            await daemon._receive(command)

            self.assertEqual(await daemon.autonomous.get(), AutonomousJob.heartbeat())
            self.assertEqual(daemon._manual_heartbeat_channel, "napcat")
            self.assertTrue(daemon.autonomous.empty())
            self.assertEqual(daemon.store.pending_events(), [])
            self.assertIsNotNone(daemon.store.self_state()["heartbeat_claimed_at"])
            daemon.store.close()

    async def test_manual_reflect_command_queues_current_day_even_when_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                    timezone="Asia/Shanghai",
                )
            )
            command = IncomingMessage(
                "qq:manual-reflect",
                "manual-reflect",
                "/reflect",
                1,
                1,
                channel="napcat",
            )
            await daemon._receive(command)
            await daemon._receive(command)

            local_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            queued = await daemon.autonomous.get()
            self.assertEqual(queued, AutonomousJob.reflection(local_date))
            self.assertTrue(daemon.autonomous.empty())
            self.assertEqual(daemon.store.pending_events(), [])
            reflection = daemon.store.reflection(local_date)
            self.assertIsNotNone(reflection)
            self.assertEqual(reflection["state"], "running")

            daemon.store._db.execute(
                """UPDATE reflections SET state='completed', claimed_at=NULL
                   WHERE local_date=?""",
                (local_date,),
            )
            daemon.store._db.commit()
            await daemon._receive(
                IncomingMessage(
                    "qq:manual-reflect-again",
                    "manual-reflect-again",
                    "/reflect",
                    2,
                    2,
                    channel="napcat",
                )
            )
            self.assertEqual(
                await daemon.autonomous.get(), AutonomousJob.reflection(local_date)
            )
            self.assertEqual(daemon.store.reflection(local_date)["state"], "running")
            daemon.store.close()

    async def test_manual_tidy_command_queues_one_persistent_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            command = IncomingMessage(
                "qq:manual-tidy",
                "manual-tidy",
                "/tidy",
                1,
                1,
                channel="napcat",
            )
            await daemon._receive(command)
            await daemon._receive(command)

            queued = await daemon.autonomous.get()
            self.assertEqual(queued.kind, "memory_maintenance")
            self.assertTrue(daemon.autonomous.empty())
            self.assertEqual(
                daemon.store.pending_memory_maintenance_turn(), queued.id
            )
            self.assertTrue(
                daemon.store.claim_memory_maintenance_turn(queued.id)
            )
            self.assertEqual(
                daemon.store.recover_memory_maintenance_turns(),
                [queued.id],
            )
            self.assertEqual(daemon.store.pending_events(), [])
            daemon.store.close()

    async def test_reply_heartbeat_turn_uses_reply_check_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.store._db.execute(
                """UPDATE self_state SET next_heartbeat_at=1660,
                   pending_reply_turn_id='question',
                   pending_reply_expectation='主人是否回复',
                   pending_reply_next_check_at=1060 WHERE id=1"""
            )
            self.assertIsNotNone(
                daemon.store.claim_due_heartbeat(
                    daemon.config.heartbeat,
                    daemon.config.notifications,
                    now=1060,
                )
            )

            with patch.object(
                daemon, "_complete_reply_wait", new_callable=AsyncMock
            ) as complete:
                await daemon._complete_heartbeat_turn(asyncio.Event())

            self.assertEqual(
                complete.await_args.args[0],
                daemon._turn_id("reply-followup", 1060.0),
            )
            daemon.store.close()



    async def test_reply_followup_requires_message_and_cannot_rearm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.store._db.execute(
                """UPDATE self_state SET pending_reply_turn_id='question',
                   pending_reply_expectation='主人对问题的回答',
                   pending_reply_since=1000,
                   pending_reply_last_reason='这个问题需要老师决定',
                   pending_reply_delay_minutes=1,
                   pending_reply_channel='napcat',
                   pending_reply_next_check_at=1060 WHERE id=1"""
            )
            daemon.store.begin_turn(
                "mandatory-followup",
                "reply_followup",
                ["reply-followup:1060"],
            )

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _system: object,
                    _messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    if provider_self.calls == 1:
                        call = ToolCall(
                            "early-end_turn",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                            },
                        )
                    elif provider_self.calls == 2:
                        call = ToolCall(
                            "required-message",
                            "send_bubbles",
                            {"bubbles": ["老师还没回答我呢"]},
                        )
                    elif provider_self.calls == 3:
                        call = ToolCall(
                            "rearm",
                            "end_turn",
                            {
                                "reply_wait": {
                                    "wait": True,
                                    "delay_minutes": 2,
                                    "expected_information": "老师的回答",
                                    "reason": "还想再等一次",
                                },
                                "mood": {"decision": "unchanged"},
                            },
                        )
                    else:
                        call = ToolCall(
                            "close",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                            },
                        )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            await daemon._complete_reply_wait(
                "mandatory-followup",
                "napcat",
                owner_event_revision=0,
            )

            self.assertEqual(provider.calls, 4)
            self.assertIsNotNone(daemon.store.pending_owner_reply())
            outbox = daemon.store.due_outbox()
            self.assertEqual([row.text for row in outbox], ["老师还没回答我呢"])
            daemon.store.mark_sent(outbox[0].id)
            self.assertIsNone(daemon.store.pending_owner_reply())
            daemon.store.close()

    async def test_owner_can_enable_a_dynamic_tool_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=root / "momoi.sqlite3",
                    log_level="INFO",
                    workspace=root,
                )
            )
            class MCP:
                tool_specs = [
                    {
                        "name": "mcp__demo__read",
                        "description": "Read a demo value.",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    }
                ]

                @staticmethod
                def has_tool(name: str) -> bool:
                    return name == "mcp__demo__read"

                @staticmethod
                def capability(_name: str) -> str:
                    return "read"

                @staticmethod
                async def call(_name: str, _arguments: object) -> dict[str, object]:
                    return {"ok": True, "value": "dynamic tool works"}

            daemon.mcp = MCP()  # type: ignore[assignment]
            daemon.tool_surface.mcp = daemon.mcp
            daemon.tool_executor.mcp = daemon.mcp
            event = IncomingMessage("owner-enable", "owner-enable", "读文件", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    names = [str(tool["name"]) for tool in tools]
                    if provider_self.calls == 1:
                        self.assertIn("read_file", names)
                        self.assertNotIn("mcp__demo__read", names)
                        call = ToolCall(
                            "enable-demo",
                            "tool_enable",
                            {"groups": ["demo"]},
                        )
                    elif provider_self.calls == 2:
                        self.assertIn("mcp__demo__read", names)
                        call = ToolCall(
                            "read-demo",
                            "mcp__demo__read",
                            {"say_to_owner": "我看看这个值"},
                        )
                    else:
                        self.assertIn("dynamic tool works", json.dumps(messages))
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
                            },
                        )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            reply = await daemon._run_tool_loop(
                daemon._system(),
                [{"role": "user", "content": "读文件"}],
                daemon.tool_surface.owner_specs("napcat"),
                [event],
                TurnDraft(),
                execution=TurnExecutionSpec("owner"),
                source_event_id=event.event_id,
                turn_id=turn_id,
                delivery_channel=daemon.channel,
            )
            self.assertIsInstance(reply, AgentReply)
            self.assertEqual(provider.calls, 3)
            daemon.store.close()

    async def test_owner_keeps_resident_internal_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("workspace group loaded")
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1",
                        "test",
                        "test",
                        100,
                        0,
                        1,
                        0,
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1",
                        "20000",
                        1,
                        60,
                        30,
                        30,
                        20,
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=root / "momoi.sqlite3",
                    log_level="INFO",
                    workspace=root,
                )
            )
            event = IncomingMessage(
                "owner-enable-workspace",
                "owner-enable-workspace",
                "读文件",
                1,
                1,
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    names = [str(tool["name"]) for tool in tools]
                    if provider_self.calls == 1:
                        self.assertIn("read_file", names)
                        self.assertIn("tool_enable", names)
                        call = ToolCall(
                            "read-note",
                            "read_file",
                            {"path": "note.txt"},
                        )
                    else:
                        self.assertIn(
                            "workspace group loaded",
                            json.dumps(messages, ensure_ascii=False),
                        )
                        call = ToolCall(
                            "finish-workspace",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
                            },
                        )
                    return ProviderResponse([], [call])

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            reply = await daemon._run_tool_loop(
                daemon._system(),
                [{"role": "user", "content": "读文件"}],
                daemon.tool_surface.owner_specs("napcat"),
                [event],
                TurnDraft(),
                execution=TurnExecutionSpec("owner"),
                source_event_id=event.event_id,
                turn_id=turn_id,
                delivery_channel=daemon.channel,
            )

            self.assertIsInstance(reply, AgentReply)
            self.assertEqual(provider.calls, 2)
            daemon.store.close()

    async def test_heartbeat_defers_while_owner_reply_is_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.store.commit_turn(
                [], "", AgentReply(["正常的 Owner 回复"]), turn_id="owner-turn"
            )
            self.assertTrue(daemon.store.claim_manual_heartbeat(now=1000))

            with patch.object(
                daemon, "_complete_heartbeat", new_callable=AsyncMock
            ) as complete:
                await daemon._complete_heartbeat_turn(asyncio.Event())

            complete.assert_not_awaited()
            self.assertIsNone(daemon.store.self_state()["heartbeat_claimed_at"])
            daemon.store.close()


    async def test_repeated_invalid_tools_force_a_visible_failure_response(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="DEBUG",
                )
            )

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    __: object,
                    tools: list[dict[str, object]],
                    **___: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls <= 3:
                        arguments = {
                            "title": "坏任务",
                            "success_criteria": "测试",
                            "next_action": "测试",
                            "next_review_at": "",
                        }
                        if self.calls == 1:
                            arguments["say_to_owner"] = "我先试着创建这个任务"
                        call = ToolCall(
                            f"bad-goal-{self.calls}",
                            "goal_create",
                            arguments,
                        )
                    elif self.calls == 4:
                        self.assert_terminal_tools(tools)
                        call = ToolCall(
                            "failed-message",
                            "send_bubbles",
                            {
                                "bubbles": ["创建任务失败：缺少有效的执行时间。"],
                            },
                        )
                    else:
                        self.assert_terminal_tools(tools)
                        call = ToolCall(
                            "failed-response",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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
                def assert_terminal_tools(tools: list[dict[str, object]]) -> None:
                    names = [tool["name"] for tool in tools]
                    if "send_bubbles" not in names or "end_turn" not in names:
                        raise AssertionError(tools)
                    if "goal_create" not in names:
                        raise AssertionError(tools)

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:bad-goal", "bad-goal", "创建任务", 1, 1)
            daemon.store.add_event(event)
            with self.assertLogs("momoi.runtime.turns", level="DEBUG") as logs:
                await daemon._complete_batch_turn(
                    [event], asyncio.Event(), daemon._turn_id(event.event_id)
                )
            self.assertEqual(provider.calls, 5)
            self.assertIn("缺少有效的执行时间", daemon.store.due_outbox()[0].text)
            self.assertTrue(
                any(
                    "Invalid isoformat string"
                    in str(
                        getattr(record, "momoi_fields", {}).get(
                            "result_message", ""
                        )
                    )
                    for record in logs.records
                )
            )
            tool_starts = [
                record
                for record in logs.records
                if getattr(record, "momoi_event", "") == "tool_start"
            ]
            tool_ends = [
                record
                for record in logs.records
                if getattr(record, "momoi_event", "") == "tool_end"
            ]
            self.assertTrue(tool_starts)
            self.assertTrue(tool_ends)
            self.assertTrue(
                all("arguments" in record.momoi_fields for record in tool_starts)
            )
            self.assertTrue(all("result" in record.momoi_fields for record in tool_ends))
            daemon.store.close()

    async def test_due_goal_stays_ahead_of_queued_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.autonomous.put_nowait(AutonomousJob.heartbeat())
            daemon.autonomous.put_nowait(AutonomousJob.goal("goal-1"))
            self.assertEqual(
                await daemon._next_work(), ("goal", AutonomousJob.goal("goal-1"))
            )
            self.assertEqual(
                await daemon._next_work(), ("goal", AutonomousJob.heartbeat())
            )
            daemon.store.close()

    async def test_goal_provider_failure_retries_same_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                    channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage("qq:1:retry", "retry", "稍后继续", 1, 1)
            daemon.store.add_event(event)
            draft = TurnDraft()
            goal = daemon.agenda_tools.execute(
                ToolCall(
                    "goal-retry",
                    "goal_create",
                    {
                        "title": "重试任务",
                        "success_criteria": "完成",
                        "next_action": "继续",
                        "next_review_at": (
                            datetime.now(ZoneInfo("UTC")) + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=event.event_id,
                allow_notify=False,
            )["goal"]
            daemon.store.commit_turn([event], event.text, AgentReply(["好"]), draft)
            await asyncio.sleep(0.03)
            claimed = daemon.store.claim_due_goal()
            original_review = float(claimed["next_review_at"])
            turn_id = daemon._turn_id("goal", goal["id"], original_review)

            async def fail(_: str, __: str) -> None:
                raise ProviderError("temporary")

            daemon._complete_goal = fail  # type: ignore[method-assign]
            before = time.time()
            await daemon._complete_goal_turn(goal["id"], asyncio.Event())
            deferred = daemon.store.goal(goal["id"])
            self.assertEqual(deferred["next_review_at"], original_review)
            self.assertGreaterEqual(float(deferred["retry_at"]), before + 299)
            self.assertEqual(deferred["failure_count"], 1)
            self.assertIsNone(deferred["review_claimed_at"])
            turn = daemon.store._db.execute(
                "SELECT state, failure_reason FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(
                (turn["state"], turn["failure_reason"]),
                ("running", "ProviderError"),
            )

            daemon.store._db.execute(
                "UPDATE goals SET retry_at=?, review_claimed_at=NULL WHERE id=?",
                (time.time() - 1, goal["id"]),
            )
            daemon.store._db.commit()
            retried = daemon.store.claim_due_goal()
            self.assertEqual(
                daemon._turn_id("goal", goal["id"], retried["next_review_at"]),
                turn_id,
            )
            daemon.store.release_goal_claim(goal["id"], defer_seconds=900)
            stopped = daemon.store.goal(goal["id"])
            self.assertIsNone(stopped["retry_at"])
            self.assertEqual(stopped["failure_count"], 0)
            self.assertGreater(float(stopped["next_review_at"]), time.time() + 899)
            daemon.store.close()

    async def test_heartbeat_can_stay_silent_or_queue_a_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="test",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                timezone="Asia/Shanghai",
                notifications=NotificationConfig(
                    cooldown_seconds=0,
                    pending_owner_delay_seconds=0,
                ),
                heartbeat=HeartbeatConfig(
                    enabled=True,
                    initial_delay_seconds=60,
                    min_interval_seconds=60,
                    max_interval_seconds=600,
                ),
                heartbeat_prompt="偶尔看看最近有什么有趣的新游戏。",
            )
            daemon = MomoiDaemon(config)
            self.assertTrue((Path(directory) / "artifacts").is_dir())
            self.assertTrue((Path(directory) / "tool-results").is_dir())
            daemon.store.create_episode(
                "最近的聊天话题",
                topics=["聊天"],
                entities=["Owner"],
                open_loops=["等待反馈"],
            )
            now = time.time()
            with daemon.store._db:
                daemon.store._db.execute(
                    """INSERT INTO turns
                       (id, kind, source_ids_json, state, started_at, updated_at)
                       VALUES ('recent-goal-turn', 'autonomous', '[]',
                               'completed', ?, ?)""",
                    (now, now),
                )
                daemon.store._db.execute(
                    """INSERT INTO messages
                       (turn_id, role, content, created_at,
                        source_event_ids_json, delivery_state)
                       VALUES ('recent-goal-turn', 'assistant',
                               '天气 Goal 已触发并成功送达', ?, '[]', 'delivered')""",
                    (now,),
                )

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _: object,
                    __: object,
                    tools: list[dict[str, object]],
                    **___: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    names = {str(tool["name"]) for tool in tools}
                    if self.calls == 1:
                        system_request = json.dumps(_, ensure_ascii=False)
                        if "偶尔看看最近有什么有趣的新游戏。" in system_request:
                            raise AssertionError(_)
                        request = json.dumps(__, ensure_ascii=False)
                        if (
                            "<workflow_contract>" not in request
                            or "偶尔看看最近有什么有趣的新游戏。" not in request
                            or "<autonomous_heartbeat>" not in request
                            or "<runtime_state>" not in request
                            or "<recent_topic_reference>" not in request
                            or "<recent_heartbeat_activities>" not in request
                            or "<recent_turn_base>" in request
                            or "<recent_turn_append>" in request
                            or "最近的聊天话题" not in request
                            or "天气 Goal 已触发并成功送达" not in request
                            or "<pending_owner_reply>" in request
                            or "reply_wait" in system_request
                        ):
                            raise AssertionError(__)
                        expected = {"heartbeat_begin"}
                        if names != expected:
                            raise AssertionError(names)
                        call = ToolCall(
                            "heartbeat-begin-one",
                            "heartbeat_begin",
                            {
                                "activity": "整理小游戏关卡灵感",
                                "mode": "work",
                                "recall_mode": "search",
                                "recall_queries": [
                                    {
                                        "semantic": "近期小游戏关卡灵感与未完成创作",
                                        "keywords": ["小游戏", "关卡"],
                                    }
                                ],
                                "tool_groups": [],
                                "strategy": [
                                    "读取一条玩法资讯",
                                    "有可复用灵感就记录，否则安静结束",
                                ],
                            },
                        )
                    elif self.calls == 2:
                        call = ToolCall(
                            "heartbeat-news",
                            "curl",
                            {"url": "https://news.example/today"},
                        )
                    elif self.calls == 3:
                        call = ToolCall(
                            "heartbeat-first-finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "heartbeat": {
                                    "activity": "整理小游戏关卡灵感",
                                    "result": "读完一条游戏新闻并记下玩法联想",
                                    "next_check_minutes": 2,
                                    "reason": "完成本次灵感整理",
                                },
                            },
                        )
                    elif self.calls == 4:
                        call = ToolCall(
                            "heartbeat-begin-two",
                            "heartbeat_begin",
                            {
                                "activity": "把关卡灵感安排成后续草案",
                                "mode": "work",
                                "recall_mode": "skip",
                                "recall_queries": [],
                                "tool_groups": [],
                                "strategy": [
                                    "创建后续 Goal",
                                    "把新点子分享给老师后结束",
                                ],
                            },
                        )
                    elif self.calls == 5:
                        call = ToolCall(
                            "heartbeat-goal",
                            "goal_create",
                            {
                                "title": "继续整理关卡点子",
                                "success_criteria": "写下一份可玩的关卡草案",
                                "next_action": "把玩法联想整理成关卡结构",
                                "next_review_at": (
                                    datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)
                                ).isoformat(),
                            },
                        )
                    elif self.calls == 6:
                        call = ToolCall(
                            "heartbeat-live",
                            "send_bubbles",
                            {"bubbles": ["刚想到一个关卡点子！"]},
                        )
                    else:
                        call = ToolCall(
                            f"heartbeat-{self.calls}",
                            "end_turn",
                            {
                                "reply_wait": {
                                    "wait": True,
                                    "delay_minutes": 4,
                                    "expected_information": "主人对关卡点子的回应",
                                    "reason": "想听老师对新关卡点子的看法",
                                },
                                "mood": {"decision": "unchanged"},
                                "heartbeat": {
                                    "activity": "整理小游戏关卡灵感",
                                    "result": "已建立自己的关卡草案任务继续整理",
                                    "next_check_minutes": 2,
                                    "reason": "有具体的新点子才分享",
                                },
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

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]

            async def read_news(_: ToolCall) -> dict[str, object]:
                return {"ok": True, "status": 200, "body": "新玩法公开"}

            daemon.builtin_tools.execute = read_news  # type: ignore[method-assign]
            daemon.store._db.execute(
                "UPDATE self_state SET next_heartbeat_at=? WHERE id=1",
                (time.time() - 1,),
            )
            daemon.store._db.commit()
            self.assertIsNotNone(
                daemon.store.claim_due_heartbeat(config.heartbeat, config.notifications)
            )
            await daemon._complete_heartbeat_turn(asyncio.Event())
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT COUNT(*) FROM notifications"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                daemon.store.self_state()["activity"], "整理小游戏关卡灵感"
            )
            self.assertEqual(
                daemon.store.self_state()["activity_result"],
                "读完一条游戏新闻并记下玩法联想",
            )

            daemon.store._db.execute(
                "UPDATE self_state SET next_heartbeat_at=? WHERE id=1",
                (time.time() - 1,),
            )
            daemon.store._db.commit()
            self.assertIsNotNone(
                daemon.store.claim_due_heartbeat(config.heartbeat, config.notifications)
            )
            await daemon._complete_heartbeat_turn(asyncio.Event())
            self.assertEqual(daemon.store.due_outbox()[0].text, "刚想到一个关卡点子！")
            goal = daemon.store.list_goals()[0]
            self.assertEqual(goal["authority"], "agent")
            self.assertEqual(goal["title"], "继续整理关卡点子")
            self.assertEqual(provider.calls, 7)
            daemon.store.close()



    async def test_owner_turn_stops_cleanly_at_configured_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                turn_max_total_tokens=1,
            )
            daemon = MomoiDaemon(config)

            class Provider:
                calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    raise AssertionError("provider must not be called beyond budget")

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:budget", "budget", "继续一个很长的任务", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)
            self.assertEqual(provider.calls, 0)
            self.assertIn(
                "per-turn processing limit", daemon.store.due_outbox()[0].text
            )
            turn = daemon.store._db.execute(
                "SELECT state, llm_calls FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual((turn["state"], turn["llm_calls"]), ("completed", 0))
            daemon.store.close()

    async def test_plain_text_with_end_turn_is_retried_through_send_bubbles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    request_text = json.dumps(messages, ensure_ascii=False)
                    if self.calls == 1:
                        self_outer.assertIn(
                            OWNER_BUBBLE_REQUEST_REMINDER, request_text
                        )
                        call = ToolCall(
                            "bad-end_turn",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                            },
                        )
                        return ProviderResponse(
                            [
                                {"type": "text", "text": "午饭要好好吃呀"},
                                {
                                    "type": "tool_use",
                                    "id": call.id,
                                    "name": call.name,
                                    "input": call.arguments,
                                },
                            ],
                            [call],
                        )
                    names = [tool["name"] for tool in tools]
                    if (
                        "send_bubbles" not in names
                        or "end_turn" not in names
                        or "memory_search" not in names
                    ):
                        raise AssertionError(tools)
                    if self.calls == 2:
                        correction = request_text
                        if (
                            "without end_turn" not in correction
                            or "alone on the next step" not in correction
                        ):
                            raise AssertionError(correction)
                        self_outer.assertIn(
                            OWNER_BUBBLE_REQUEST_REMINDER, correction
                        )
                        call = ToolCall(
                            "send",
                            "send_bubbles",
                            {"bubbles": ["午饭要好好吃呀"]},
                        )
                    else:
                        self_outer.assertNotIn(
                            OWNER_BUBBLE_REQUEST_REMINDER, request_text
                        )
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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

            self_outer = self
            provider = Provider()
            event = IncomingMessage(
                "owner-lunch", "owner-lunch", "等午饭吧", 1, 1
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            canonical_messages = [{"role": "user", "content": event.text}]
            reply = await daemon._run_tool_loop(
                daemon._system(),
                canonical_messages,
                daemon.tool_surface.owner_specs(),
                [event],
                TurnDraft(),
                execution=TurnExecutionSpec("owner"),
                source_event_id=event.event_id,
                turn_id=turn_id,
                delivery_channel=daemon.channel,
            )

            self.assertEqual(provider.calls, 3)
            self.assertNotIn(
                OWNER_BUBBLE_REQUEST_REMINDER,
                json.dumps(canonical_messages, ensure_ascii=False),
            )
            self.assertEqual(reply.messages, [])
            self.assertEqual(
                daemon.store._db.execute(
                    """SELECT text FROM turn_progress
                       WHERE turn_id=? ORDER BY created_at""",
                    (turn_id,),
                ).fetchone()["text"],
                "午饭要好好吃呀",
            )
            daemon.store.close()

    async def test_plain_text_before_recall_is_corrected_to_native_recall(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class Provider:
                calls = 0

                async def complete(
                    provider_self,
                    _system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.calls += 1
                    latest = json.dumps(messages[-1], ensure_ascii=False)
                    if provider_self.calls == 1:
                        return ProviderResponse(
                            [{"type": "text", "text": "[tool_call] recall(...)"}],
                            [],
                        )
                    if provider_self.calls == 2:
                        self.assertIn("Call recall first and alone", latest)
                        self.assertIn("native tool call", latest)
                        self.assertNotIn(OWNER_BUBBLE_REQUEST_REMINDER, latest)
                        return recall_response()
                    if provider_self.calls == 3:
                        self.assertIn(OWNER_BUBBLE_REQUEST_REMINDER, latest)
                        call = ToolCall(
                            "send-after-recall",
                            "send_bubbles",
                            {"bubbles": ["我在呢"]},
                        )
                    else:
                        self.assertNotIn(OWNER_BUBBLE_REQUEST_REMINDER, latest)
                        call = ToolCall(
                            "finish-after-recall",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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

            event = IncomingMessage("owner-sigh", "owner-sigh", "唉", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            canonical_messages = [{"role": "user", "content": event.text}]

            reply = await daemon._run_tool_loop(
                daemon._system(),
                canonical_messages,
                daemon.tool_surface.owner_specs(),
                [event],
                TurnDraft(),
                execution=TurnExecutionSpec("owner"),
                source_event_id=event.event_id,
                turn_id=turn_id,
                delivery_channel=daemon.channel,
            )

            self.assertEqual(provider.calls, 4)
            self.assertIsInstance(reply, AgentReply)
            self.assertNotIn(
                OWNER_BUBBLE_REQUEST_REMINDER,
                json.dumps(canonical_messages, ensure_ascii=False),
            )
            daemon.store.close()

    async def test_plain_text_with_invalid_mood_is_retried_through_send_bubbles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1", "test", "test", 100, 0, 1, 0
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )

            class Provider:
                def __init__(self) -> None:
                    self.calls = 0
                    self.corrections: list[str] = []

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        call = ToolCall(
                            "bad-end_turn",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": "unchanged",
                            },
                        )
                        return ProviderResponse(
                            [
                                {
                                    "type": "text",
                                    "text": "哼，游戏宅也会做功课的好吗",
                                },
                                {
                                    "type": "tool_use",
                                    "id": call.id,
                                    "name": call.name,
                                    "input": call.arguments,
                                },
                            ],
                            [call],
                        )
                    last = messages[-1]
                    content = last.get("content")
                    if isinstance(content, list):
                        self.corrections.extend(
                            str(block.get("text") or "")
                            for block in content
                            if isinstance(block, dict)
                        )
                    names = [tool["name"] for tool in tools]
                    if (
                        "send_bubbles" not in names
                        or "end_turn" not in names
                        or "memory_search" not in names
                    ):
                        raise AssertionError(tools)
                    if self.calls == 2:
                        call = ToolCall(
                            "send",
                            "send_bubbles",
                            {"bubbles": ["哼，游戏宅也会做功课的好吗"]},
                        )
                    else:
                        call = ToolCall(
                            "finish",
                            "end_turn",
                            {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
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

            provider = Provider()
            event = IncomingMessage(
                "owner-homework", "owner-homework", "不是天天打游戏么", 1, 1
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            reply = await daemon._run_tool_loop(
                daemon._system(),
                [{"role": "user", "content": event.text}],
                daemon.tool_surface.owner_specs(),
                [event],
                TurnDraft(),
                execution=TurnExecutionSpec("owner"),
                source_event_id=event.event_id,
                turn_id=turn_id,
                delivery_channel=daemon.channel,
            )

            self.assertEqual(provider.calls, 3)
            self.assertTrue(
                any(
                    "Call send_bubbles" in text
                    and "without end_turn" in text
                    and "alone on the next step" in text
                    for text in provider.corrections
                )
            )
            self.assertEqual(reply.messages, [])
            self.assertEqual(
                daemon.store._db.execute(
                    """SELECT text FROM turn_progress
                       WHERE turn_id=? ORDER BY created_at""",
                    (turn_id,),
                ).fetchone()["text"],
                "哼，游戏宅也会做功课的好吗",
            )
            daemon.store.close()

    async def test_optional_tool_announce_preserves_history_and_limits_parallel_batch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    llm=LLMConfig(
                        "http://127.0.0.1",
                        "test",
                        "test",
                        100,
                        0,
                        1,
                        0,
                        "openai",
                    ),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            event = IncomingMessage(
                "owner-announce", "owner-announce", "帮我继续查", 1, 1
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            execute = AsyncMock(
                side_effect=[
                    {"ok": True, "value": "first-result"},
                    {"ok": True, "value": "second-result"},
                    {"ok": True, "value": "third-result"},
                ]
            )
            daemon.builtin_tools.execute = execute  # type: ignore[method-assign]

            class Provider:
                calls = 0

                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        curl = next(tool for tool in tools if tool["name"] == "curl")
                        schema = curl["input_schema"]
                        if "say_to_owner" not in schema["properties"]:
                            raise AssertionError(schema)
                        if "say_to_owner" in schema.get("required", []):
                            raise AssertionError(schema)
                        calls = [
                            ToolCall(
                                "curl-missing",
                                "curl",
                                {"url": "https://missing.example"},
                            )
                        ]
                    elif self.calls == 2:
                        history = json.dumps(messages, ensure_ascii=False)
                        if "owner_work_acknowledgement_required" not in history:
                            raise AssertionError(messages)
                        calls = [
                            ToolCall(
                                "curl-one",
                                "curl",
                                {
                                    "url": "https://one.example",
                                    "say_to_owner": "先说这一句",
                                },
                            ),
                            ToolCall(
                                "curl-two",
                                "curl",
                                {
                                    "url": "https://two.example",
                                    "say_to_owner": "同批这句不应投递",
                                },
                            ),
                        ]
                    elif self.calls == 3:
                        history = json.dumps(messages, ensure_ascii=False)
                        if (
                            "先说这一句" not in history
                            or "同批这句不应投递" in history
                            or "first-result" not in history
                            or "second-result" not in history
                        ):
                            raise AssertionError(messages)
                        calls = [
                            ToolCall(
                                "curl-three",
                                "curl",
                                {"url": "https://three.example"},
                            )
                        ]
                    elif self.calls == 4:
                        calls = [
                            ToolCall(
                                "send",
                                "send_bubbles",
                                {"bubbles": ["查完了"]},
                            )
                        ]
                    else:
                        calls = [
                            ToolCall(
                                "finish",
                                "end_turn",
                                {
                                    "reply_wait": {"wait": False},
                                    "mood": {"decision": "unchanged"},
                                    "activity": {"decision": "unchanged"},
                                },
                            )
                        ]
                    return ProviderResponse(
                        [
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                            for call in calls
                        ],
                        calls,
                    )

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            reply = await daemon._run_tool_loop(
                daemon._system(),
                [{"role": "user", "content": event.text}],
                daemon.tool_surface.owner_specs(),
                [event],
                TurnDraft(),
                execution=TurnExecutionSpec("owner"),
                source_event_id=event.event_id,
                turn_id=turn_id,
                delivery_channel=daemon.channel,
            )

            self.assertEqual(provider.calls, 5)
            self.assertEqual(reply.messages, [])
            self.assertEqual(execute.await_count, 3)
            for await_call in execute.await_args_list:
                self.assertNotIn("say_to_owner", await_call.args[0].arguments)
            progress = daemon.store._db.execute(
                """SELECT tool_call_id, text FROM turn_progress
                   WHERE turn_id=? AND tool_call_id LIKE 'curl-%'
                   ORDER BY created_at""",
                (turn_id,),
            ).fetchall()
            self.assertEqual(
                [(row["tool_call_id"], row["text"]) for row in progress],
                [("curl-one", "先说这一句")],
            )
            journal = [
                json.loads(row["payload_json"])
                for row in daemon.store._db.execute(
                    """SELECT payload_json FROM turn_journal
                       WHERE turn_id=? ORDER BY sequence""",
                    (turn_id,),
                ).fetchall()
            ]
            self.assertEqual(
                [item["name"] for item in journal],
                ["curl", "curl", "curl", "curl", "curl", "curl"],
            )
            self.assertNotIn("say_to_owner", json.dumps(journal, ensure_ascii=False))
            self.assertIn("first-result", json.dumps(journal, ensure_ascii=False))
            daemon.store.close()

    async def test_owner_turn_does_not_retry_after_provider_exhausts_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)

            class Provider:
                calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    raise ProviderError("model engine error")

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            event = IncomingMessage("qq:provider-error", "provider-error", "测试", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)

            await asyncio.wait_for(
                daemon._complete_batch_turn([event], asyncio.Event(), turn_id),
                timeout=1,
            )

            self.assertEqual(provider.calls, 1)
            failure = daemon.store.due_outbox()[0].text
            self.assertIn("model service failed", failure)
            self.assertIn("Reason: model engine error", failure)
            self.assertNotIn("without repeated retries", failure)
            turn = daemon.store._db.execute(
                "SELECT state, failure_reason FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(
                (turn["state"], turn["failure_reason"]), ("completed", "ProviderError")
            )
            daemon.store.close()

    async def test_fatal_error_after_external_effect_requires_reconciliation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            event = IncomingMessage(
                "qq:fatal-after-tool", "fatal-after-tool", "测试", 1, 1
            )
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)

            async def fail_after_tool(_: object, current_turn_id: str) -> None:
                daemon.store.begin_tool_call(
                    current_turn_id, "call-1", "write_file", {}, "write"
                )
                raise RuntimeError("boom")

            daemon._complete_batch = fail_after_tool  # type: ignore[method-assign]
            await daemon._complete_batch_turn([event], asyncio.Event(), turn_id)

            self.assertIn(f"/resolve {turn_id[:12]}", daemon.store.due_outbox()[0].text)
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT status FROM reconciliations WHERE turn_id=?", (turn_id,)
                ).fetchone()["status"],
                "open",
            )
            daemon.store.close()

    async def test_scheduler_queues_persisted_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
                notifications=NotificationConfig(
                    cooldown_seconds=0,
                    pending_owner_delay_seconds=0,
                ),
            )
            daemon = MomoiDaemon(config)
            daemon.store.commit_autonomous_turn(
                "goal",
                TurnDraft(
                    notification_messages=["后台检查完成"],
                    notification_key="check.result",
                    notification_reason="test",
                ),
                turn_id="notification-turn",
            )
            stop = asyncio.Event()
            worker = asyncio.create_task(daemon._scheduler_worker(stop))
            for _ in range(100):
                if daemon.store.due_outbox():
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(daemon.store.due_outbox()[0].text, "后台检查完成")
            self.assertEqual(daemon.store.due_outbox()[0].channel, "napcat")
            stop.set()
            daemon.agenda_changed.set()
            await worker
            daemon.store.close()

    async def test_stop_command_cancels_active_turn_and_is_queued_for_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=12,
                transcript_turns_max=12,
                episode_raw_tail_turns=6,
                memory_results=6,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            daemon.store.queue_progress(
                "old-napcat-turn", "old-napcat-call", ["仍在排队"], "napcat"
            )
            daemon.store.queue_progress(
                "old-weixin-turn", "old-weixin-call", ["微信仍在排队"], "weixin"
            )
            daemon._active_turn = asyncio.create_task(asyncio.sleep(3600))
            command = IncomingMessage("qq:1:stop", "stop", "/stop", 1, 1)
            await daemon._receive(command)
            with self.assertRaises(asyncio.CancelledError):
                await daemon._active_turn
            self.assertEqual((await daemon.incoming.get()).text, "/stop")
            self.assertEqual(daemon.store.pending_events()[0].text, "/stop")
            states = {
                row["target_channel"]: row["state"]
                for row in daemon.store._db.execute(
                    "SELECT target_channel, state FROM outbox ORDER BY id"
                ).fetchall()
            }
            self.assertEqual(states["napcat"], "superseded")
            self.assertEqual(states["weixin"], "pending")
            daemon.store.close()

    async def test_stop_cancels_autonomous_turn_and_defers_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)
            source = IncomingMessage("qq:1:goal-stop", "goal-stop", "继续任务", 1, 1)
            daemon.store.add_event(source)
            draft = TurnDraft()
            created = daemon.agenda_tools.execute(
                ToolCall(
                    "goal-stop-create",
                    "goal_create",
                    {
                        "title": "长任务",
                        "success_criteria": "完成",
                        "next_action": "继续执行",
                        "next_review_at": (
                            datetime.now(ZoneInfo("UTC")) + timedelta(milliseconds=20)
                        ).isoformat(),
                    },
                ),
                draft,
                authority="owner",
                source_event_id=source.event_id,
                allow_notify=False,
            )
            goal_id = created["goal"]["id"]
            daemon.store.commit_turn(
                [source], source.text, AgentReply(["接下了"]), draft
            )
            initial = daemon.store.due_outbox()[0]
            daemon.store.mark_sent(initial.id)
            time.sleep(0.03)
            daemon.store.claim_due_goal()

            started = asyncio.Event()

            class Provider:
                def __init__(self) -> None:
                    self.calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        started.set()
                        await asyncio.Future()
                    if self.calls == 2:
                        call = ToolCall(
                            "stop-message",
                            "send_bubbles",
                            {"bubbles": ["已经停下来了"]},
                        )
                    else:
                        call = ToolCall(
                            "stop-response",
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

            provider = Provider()
            daemon.provider = with_owner_recall(provider)  # type: ignore[assignment]
            daemon.autonomous.put_nowait(AutonomousJob.goal(goal_id))
            worker = asyncio.create_task(daemon._agent_worker(asyncio.Event()))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                await daemon._receive(
                    IncomingMessage("qq:1:stop-goal", "stop-goal", "/stop", 2, 2)
                )
                for _ in range(100):
                    if daemon.store.due_outbox():
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(daemon.store.due_outbox()[0].text, "已经停下来了")
                goal = daemon.store.goal(goal_id)
                self.assertGreater(float(goal["next_review_at"]), time.time() + 800)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                daemon.store.close()

    async def test_stop_during_external_tool_leaves_ambiguous_audit_and_cancels_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(
                llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
                channel=NapCatConfig("ws://127.0.0.1", "20000", 0.01, 60, 30, 30, 20),
                system_prompt="You are Momoi.",
                transcript_turns_min=4,
                transcript_turns_max=4,
                episode_raw_tail_turns=2,
                memory_results=2,
                database=Path(directory) / "momoi.sqlite3",
                log_level="INFO",
            )
            daemon = MomoiDaemon(config)

            async def blocked_write(_: ToolCall) -> dict[str, object]:
                await asyncio.sleep(30)
                return {"ok": True}

            daemon.builtin_tools.execute = blocked_write  # type: ignore[method-assign]

            class Provider:
                def __init__(self) -> None:
                    self.calls = 0

                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    self.calls += 1
                    if self.calls == 1:
                        call = ToolCall(
                            "blocked-write",
                            "write_file",
                            {"path": "/tmp/momoi-stop-test", "content": "test"},
                        )
                    elif self.calls == 2:
                        call = ToolCall(
                            "stop-message",
                            "send_bubbles",
                            {
                                "bubbles": ["已经终止当前任务"],
                            },
                        )
                    else:
                        call = ToolCall(
                            "stop-after-tool",
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

            daemon.provider = with_owner_recall(Provider())  # type: ignore[assignment]
            stop = asyncio.Event()
            worker = asyncio.create_task(daemon._agent_worker(stop))
            original = IncomingMessage("qq:1:tool-stop", "tool-stop", "等一会儿", 1, 1)
            try:
                await daemon._receive(original)
                for _ in range(100):
                    audit = daemon.store._db.execute(
                        "SELECT state FROM tool_audit WHERE tool_call_id='blocked-write'"
                    ).fetchone()
                    if audit is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(audit["state"], "dispatching")
                await daemon._receive(
                    IncomingMessage("qq:1:stop-tool", "stop-tool", "/stop", 2, 2)
                )
                for _ in range(100):
                    row = daemon.store._db.execute(
                        "SELECT state FROM turns WHERE id=?",
                        (daemon._turn_id(original.event_id),),
                    ).fetchone()
                    if row is not None and row["state"] == "cancelled":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(row["state"], "cancelled")
                self.assertEqual(
                    daemon.store._db.execute(
                        "SELECT state FROM tool_audit WHERE tool_call_id='blocked-write'"
                    ).fetchone()["state"],
                    "dispatching",
                )
                self.assertEqual(
                    daemon.store._db.execute(
                        "SELECT status FROM reconciliations WHERE turn_id=?",
                        (daemon._turn_id(original.event_id),),
                    ).fetchone()["status"],
                    "open",
                )
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                daemon.store.close()

    async def test_message_reaches_llm_and_reply_reaches_napcat(self) -> None:
        stop = asyncio.Event()
        sent: list[str] = []
        llm_requests: list[dict[str, object]] = []

        async def llm(request: web.Request) -> web.Response:
            payload = await request.json()
            llm_requests.append(payload)
            main_call = len(llm_requests)
            if main_call == 1:
                recalled = recall_response()
                return web.json_response(
                    {"stop_reason": "tool_use", "content": recalled.content}
                )
            if main_call == 2:
                return web.json_response(
                    {
                        "stop_reason": "tool_use",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "progress-1",
                                "name": "send_bubbles",
                                "input": {
                                    "bubbles": ["我先处理一下"],
                                },
                            }
                        ],
                    }
                )
            if main_call <= 5:
                return web.json_response(
                    {
                        "stop_reason": "tool_use",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"search-{main_call}",
                                "name": "memory_search",
                                "input": {"query": "问候"},
                            }
                        ],
                    }
                )
            if main_call == 6:
                return web.json_response(
                    {"content": [{"type": "text", "text": "这段 raw text 不应发送"}]}
                )
            if main_call == 7:
                return web.json_response(
                    {
                        "stop_reason": "tool_use",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "send-final",
                                "name": "send_bubbles",
                                "input": {"bubbles": ["测试回复一", "测试回复二"]},
                            }
                        ],
                    }
                )
            return web.json_response(
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "end_turn-1",
                            "name": "end_turn",
                            "input": {
                                "reply_wait": {"wait": False},
                                "mood": {"decision": "unchanged"},
                                "activity": {"decision": "unchanged"},
                            },
                        }
                    ],
                }
            )

        async def napcat(request: web.Request) -> web.WebSocketResponse:
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 10000,
                    "user_id": 20000,
                    "message_id": 1,
                    "time": 1,
                    "message": [{"type": "text", "data": {"text": "你好"}}],
                }
            )
            async for message in socket:
                payload = json.loads(message.data)
                sent.append(payload["params"]["message"][0]["data"]["text"])
                await socket.send_json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": {"message_id": 2},
                        "echo": payload["echo"],
                    }
                )
                if len(sent) == 3:
                    stop.set()
            return socket

        llm_server = TestServer(web.Application())
        llm_server.app.router.add_post("/v1/messages", llm)
        napcat_server = TestServer(web.Application())
        napcat_server.app.router.add_get("/", napcat)
        await llm_server.start_server()
        await napcat_server.start_server()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = AppConfig(
                    llm=LLMConfig(
                        base_url=str(llm_server.make_url("/")).rstrip("/"),
                        api_key="test",
                        model="test",
                        max_tokens=100,
                        temperature=0,
                        timeout_seconds=1,
                        max_retries=0,
                    ),
                    channel=NapCatConfig(
                        url=str(napcat_server.make_url("/")).replace(
                            "http://", "ws://"
                        ),
                        owner_qq="20000",
                        quiet_seconds=0.01,
                        max_batch_seconds=1,
                        heartbeat_seconds=1,
                        reconnect_max_seconds=1,
                        send_timeout_seconds=1,
                    ),
                    system_prompt="You are Momoi.",
                    transcript_turns_min=12,
                    transcript_turns_max=12,
                    episode_raw_tail_turns=6,
                    memory_results=6,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
                with patch(
                    "momoi.runtime.dispatch.delivery.random.uniform",
                    return_value=0,
                ):
                    await asyncio.wait_for(MomoiDaemon(config).run(stop), timeout=2)
        finally:
            await napcat_server.close()
            await llm_server.close()

        self.assertEqual(sent, ["我先处理一下", "测试回复一", "测试回复二"])
        self.assertEqual(len(llm_requests), 8)
        initial_tools = [tool["name"] for tool in llm_requests[0]["tools"]]
        self.assertEqual(initial_tools, ["recall"])
        second_tools = [tool["name"] for tool in llm_requests[1]["tools"]]
        self.assertIn("send_bubbles", second_tools)
        self.assertIn("end_turn", second_tools)
        self.assertNotIn("tool_choice", llm_requests[0])
        self.assertNotIn("Context planning protocol", str(llm_requests[0]["system"]))
        self.assertIn("send_bubbles", second_tools)
        final_tools = [tool["name"] for tool in llm_requests[7]["tools"]]
        self.assertIn("send_bubbles", final_tools)
        self.assertIn("end_turn", final_tools)
        self.assertIn("memory_search", final_tools)
        self.assertNotIn("tool_choice", llm_requests[7])
        self.assertEqual(
            llm_requests[0]["system"][0]["cache_control"], {"type": "ephemeral"}
        )
        self.assertIn("You are Momoi.", llm_requests[0]["system"][0]["text"])
        self.assertTrue(
            llm_requests[0]["system"][0]["text"].rstrip().endswith("You are Momoi.")
        )
        self.assertEqual(len(llm_requests[0]["system"]), 1)
        self.assertEqual(len(llm_requests[7]["system"]), 2)
        self.assertIn("Memory tools", llm_requests[7]["system"][1]["text"])
        self.assertEqual(
            llm_requests[0]["system"][0]["text"],
            llm_requests[7]["system"][0]["text"],
        )
        self.assertEqual(
            llm_requests[1]["messages"][-1]["content"][0]["type"], "tool_result"
        )
        self.assertEqual(llm_requests[0]["messages"][-1]["role"], "user")
        current_content = llm_requests[0]["messages"][-1]["content"]
        # Owner text and its attachments are now separate blocks, so the cache
        # breakpoint sits at the end of the whole tail rather than on a single
        # combined block.
        self.assertEqual(current_content[-1]["cache_control"], {"type": "ephemeral"})
        current_text = "".join(
            str(block.get("text") or "")
            for block in current_content
            if isinstance(block, dict)
        )
        self.assertIn("你好", current_text)
        self.assertNotIn("Trusted runtime context", current_text)
        # The tail carries what moves with the Turn, ending in owner speech.
        # Slow-changing memory sits ahead of the transcript instead.
        self.assertIn("<current_owner_bubbles>", current_text)
        self.assertIn("</current_owner_bubbles>", current_text)
        self.assertIn("<workflow_contract>", current_text)
        self.assertIn("# Owner Turn contract", current_text)
        self.assertNotIn("Owner Turn: recall first", current_text)
        self.assertTrue(current_text.endswith("</current_owner_bubbles>"))
        self.assertNotIn("Every response in this Turn", current_text)
        self.assertIn("<runtime_state>", current_text)
        self.assertLess(
            current_text.index("<runtime_state>"),
            current_text.index("<current_owner_bubbles>"),
        )
        self.assertNotIn("<long_term_memories>", current_text)
        self.assertNotIn("<context_resolution>", current_text)
        self.assertNotIn(
            "Consecutive messages from the authenticated user",
            current_text,
        )
        self.assertNotIn("主人", current_text)
