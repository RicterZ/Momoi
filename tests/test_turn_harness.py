import unittest

from momoi.models import ToolCall
from momoi.runtime.agent import TURN_HARNESS_SPECS, TurnHarness
from momoi.runtime.turn_support import PROMPT_ROOT


class TurnHarnessTest(unittest.TestCase):
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
                "memory_maintenance",
                "episode_consolidate",
                "episode_anneal",
            },
        )

    def test_empty_first_states_are_declared_not_implicit(self) -> None:
        for stage in {
            "webhook",
            "goal",
            "reflection",
            "memory_maintenance",
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
