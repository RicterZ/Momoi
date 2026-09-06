import unittest

from momoi.models import ToolCall
from momoi.runtime.agent import TURN_HARNESS_SPECS, TurnHarness
from momoi.runtime.agent.protocol import (
    assistant_history_content,
    handle_no_tool_response,
    harness_correction,
)
from momoi.runtime.turn_support import PROMPT_ROOT
from momoi.runtime.turn_support import ExternalToolTurnError, MAX_CONSECUTIVE_TOOL_FAILURES
from momoi.runtime.agent.workflow import WorkflowProtocolError


class TurnHarnessTest(unittest.TestCase):
    def test_no_tool_failures_stop_at_the_shared_limit(self) -> None:
        for stage in TURN_HARNESS_SPECS:
            for started in (False, True):
                for external_effect in (False, True):
                    with self.subTest(stage=stage, started=started, external_effect=external_effect):
                        workflow = stage in {
                            "reflection", "memory_maintenance", "memory_operation",
                            "episode_consolidate", "episode_anneal",
                        }
                        messages = []
                        failed_rounds = 0
                        error_type = (
                            ExternalToolTurnError
                            if external_effect and not workflow
                            else WorkflowProtocolError
                        )
                        for attempt in range(1, MAX_CONSECUTIVE_TOOL_FAILURES + 1):
                            arguments = dict(
                                workflow_correction="Use native tools" if workflow else None,
                                heartbeat_turn=stage == "heartbeat",
                                harness_started=started,
                                goal_turn=stage == "goal",
                                require_response=stage in {
                                    "owner", "heartbeat", "webhook", "reply_followup",
                                },
                                owner_turn=stage == "owner",
                                failed_rounds=failed_rounds,
                                last_tool_error="",
                                external_effect=external_effect,
                            )
                            if attempt == MAX_CONSECUTIVE_TOOL_FAILURES:
                                with self.assertRaises(error_type):
                                    handle_no_tool_response(messages, "hello", **arguments)
                            else:
                                resolution = handle_no_tool_response(
                                    messages, "hello", **arguments,
                                )
                                failed_rounds = resolution.failed_rounds
                                self.assertEqual(failed_rounds, attempt)

    def test_owner_text_corrections_preserve_opening_and_delivery_rules(self) -> None:
        for started, expected in ((False, "recall first and alone"),
                                  (True, "Call send_bubbles")):
            with self.subTest(started=started):
                messages = []
                resolution = handle_no_tool_response(
                    messages, "hello", workflow_correction=None, heartbeat_turn=False,
                    harness_started=started, goal_turn=False, require_response=True,
                    owner_turn=True, failed_rounds=0, last_tool_error="",
                )
                self.assertEqual(resolution.action, "retry")
                self.assertIn(expected, messages[-1]["content"])
        correction = harness_correction(
            [ToolCall("end", "end_turn", {})], "assistant_text_forbidden",
            owner_turn=True,
        )
        self.assertIn("Call send_bubbles", correction[-1]["text"])

    WORKFLOW_PROMPTS = {
        "owner": "owner.md",
        "heartbeat": "heartbeat.md",
        "reply_followup": "reply_wait.md",
        "webhook": "webhook.md",
        "goal": "goal.md",
        "reflection": "reflection.md",
        "memory_maintenance": "memory_maintenance.md",
        "episode_consolidate": "episode_consolidation.md",
        "episode_anneal": "episode_summary.md",
    }

    def test_private_reasoning_is_not_replayed_between_rounds(self) -> None:
        content = [
            {"type": "reasoning", "text": "openai private thought"},
            {"type": "thinking", "thinking": "anthropic private thought"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "tool_use", "id": "1", "name": "curl", "input": {}},
        ]

        self.assertEqual(
            assistant_history_content(content),
            [{"type": "tool_use", "id": "1", "name": "curl", "input": {}}],
        )

    def test_every_model_turn_stage_has_an_explicit_harness(self) -> None:
        self.assertEqual(
            set(TURN_HARNESS_SPECS),
            {
                "owner",
                "heartbeat",
                "reply_followup",
                "webhook",
                "goal",
                "reflection",
                "memory_maintenance", "memory_operation",
                "episode_consolidate",
                "episode_anneal",
            },
        )

    def test_empty_first_states_are_declared_not_implicit(self) -> None:
        for stage in {
            "webhook",
            "goal",
            "reflection",
            "memory_maintenance", "memory_operation",
            "episode_consolidate",
            "episode_anneal",
        }:
            with self.subTest(stage=stage):
                harness = TurnHarness.for_stage(stage)
                self.assertIsNone(harness.spec.first_tool)
                self.assertTrue(harness.started)

    def test_workflow_contracts_name_their_harness_boundary_tools(self) -> None:
        for stage, filename in self.WORKFLOW_PROMPTS.items():
            with self.subTest(stage=stage):
                contract = PROMPT_ROOT.joinpath(filename).read_text(encoding="utf-8")
                spec = TURN_HARNESS_SPECS[stage]
                if spec.first_tool is not None:
                    self.assertIn(f"`{spec.first_tool}`", contract)
                self.assertIn(f"`{spec.terminal_tool}`", contract)

    def test_owner_recall_must_be_first_and_alone(self) -> None:
        harness = TurnHarness.for_stage("owner")
        recall = ToolCall("recall", "recall", {})
        send = ToolCall("send", "send_bubbles", {"bubbles": ["ok"]})

        self.assertEqual(
            harness.validate([recall, send]),
            "recall_must_be_first_and_alone",
        )
        self.assertIsNone(harness.validate([recall]))
        harness.accept("recall")
        self.assertEqual(harness.validate([recall]), "recall_already_completed")

    def test_owner_progress_tools_require_preceding_bubbles_once(self) -> None:
        harness = TurnHarness.for_stage(
            "owner",
            progress_tool_names=frozenset(
                {"curl", "goal_create", "mcp__demo__lookup"}
            ),
        )
        harness.accept("recall")
        bubbles = ToolCall("say", "send_bubbles", {"bubbles": ["我看看"]})
        curl = ToolCall("curl", "curl", {"url": "https://example.com"})

        self.assertEqual(
            harness.validate([curl]),
            "send_bubbles_required_before_progress_work",
        )
        self.assertEqual(
            harness.validate([curl, bubbles]),
            "send_bubbles_required_before_progress_work",
        )
        self.assertIsNone(harness.validate([bubbles, curl]))
        harness.observe_calls([bubbles, curl])
        self.assertIsNone(harness.validate([curl]))
        harness.reset()
        harness.accept("recall")
        self.assertEqual(
            harness.validate([curl]),
            "send_bubbles_required_before_progress_work",
        )

    def test_owner_progress_rule_uses_calls_not_delivery_results(self) -> None:
        harness = TurnHarness.for_stage(
            "owner", progress_tool_names=frozenset({"curl"})
        )
        harness.accept("recall")
        bubbles = ToolCall("say", "send_bubbles", {"bubbles": []})

        self.assertIsNone(harness.validate([bubbles]))
        harness.observe_calls([bubbles])
        self.assertTrue(harness.progress_bubbles_seen)
        self.assertIsNone(
            harness.validate(
                [ToolCall("curl", "curl", {"url": "https://example.com"})]
            )
        )

    def test_progress_rule_is_owner_harness_only(self) -> None:
        webhook = TurnHarness.for_stage(
            "webhook", progress_tool_names=frozenset({"curl"})
        )
        self.assertIsNone(
            webhook.validate(
                [ToolCall("curl", "curl", {"url": "https://example.com"})]
            )
        )

    def test_webhook_harness_rejects_tools_outside_its_contract(self) -> None:
        harness = TurnHarness.for_stage("webhook")

        self.assertEqual(
            harness.validate([ToolCall("memory", "memory_search", {"query": "x"})]),
            "tool_not_allowed",
        )
        self.assertIsNone(
            harness.validate([ToolCall("curl", "curl", {"url": "https://x"})])
        )

    def test_required_tool_is_enforced_without_surface_projection(self) -> None:
        harness = TurnHarness.for_stage("goal")

        self.assertEqual(
            harness.validate(
                [ToolCall("work", "goal_update", {})],
                required_tool="end_turn",
            ),
            "end_turn_required",
        )
        self.assertIsNone(
            harness.validate(
                [ToolCall("finish", "end_turn", {"goal": {"status": "done", "result": "done"}})],
                required_tool="end_turn",
            )
        )

    def test_assistant_text_invalidates_an_otherwise_valid_tool_call(self) -> None:
        harness = TurnHarness.for_stage("owner")
        recall = ToolCall("recall", "recall", {})

        self.assertEqual(
            harness.validate([recall], has_assistant_text=True),
            "assistant_text_forbidden",
        )

    def test_harness_requires_its_boundary_tools_on_the_surface(self) -> None:
        harness = TurnHarness.for_stage("owner")

        with self.assertRaisesRegex(ValueError, "end_turn, recall"):
            harness.validate_surface(set())
        harness.validate_surface({"recall", "send_bubbles", "end_turn"})

    def test_heartbeat_and_reply_wait_have_explicit_first_states(self) -> None:
        self.assertEqual(
            TurnHarness.for_stage("heartbeat").spec.first_tool,
            "heartbeat_begin",
        )
        self.assertEqual(
            TurnHarness.for_stage("reply_followup").spec.first_tool,
            "send_bubbles",
        )

    def test_terminal_tool_must_be_alone_for_every_stage(self) -> None:
        work = ToolCall("work", "work", {})
        for stage, spec in TURN_HARNESS_SPECS.items():
            with self.subTest(stage=stage):
                harness = TurnHarness.for_stage(stage)
                if spec.first_tool is not None:
                    harness.accept(spec.first_tool)
                terminal = ToolCall("terminal", spec.terminal_tool, {})
                self.assertEqual(
                    harness.validate([work, terminal]),
                    f"{spec.terminal_tool}_must_be_alone",
                )

    def test_unknown_stage_cannot_fall_back_to_an_empty_harness(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing Turn harness"):
            TurnHarness.for_stage("unknown")


if __name__ == "__main__":
    unittest.main()
