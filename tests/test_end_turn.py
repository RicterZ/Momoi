import unittest

from momoi.runtime.agent.protocol import parse_end_turn
from momoi.runtime.agent.workflow import TurnExecutionSpec


class EndTurnTest(unittest.TestCase):
    STAGES = ("owner", "heartbeat", "webhook", "reply_followup")

    def arguments(self, stage, *, wait=False):
        result = {
            "mood": {
                "decision": "updated",
                "state": "frustrated",
                "intensity": 0.6,
                "cause": "The shared game ended badly",
            },
            "reply_wait": {"wait": wait},
        }
        if wait:
            result["reply_wait"].update(
                delay_minutes=3,
                expected_information="Which game to play next",
                reason="Suggest another game if the owner is still undecided",
            )
        if stage == "owner":
            result["activity"] = {"decision": "unchanged"}
        elif stage == "heartbeat":
            result["heartbeat"] = {
                "activity": "thinking about games",
                "result": "",
                "reason": "Taking a break",
                "next_check_minutes": 30,
            }
        return result

    def parse(self, stage, arguments, *, visible=True):
        execution = TurnExecutionSpec(
            stage, goal_id="existing-goal" if stage == "goal" else None
        )
        return parse_end_turn(
            arguments,
            execution=execution,
            visible_since_owner_update=visible,
            heartbeat_min_interval_seconds=300,
            heartbeat_max_interval_seconds=3600,
        )

    def test_all_chat_stages_return_private_state_without_messages(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                reply, error = self.parse(stage, self.arguments(stage))
                self.assertIsNone(error)
                self.assertEqual(reply.messages, [])
                self.assertEqual(reply.mood_update["state"], "frustrated")
                self.assertFalse(reply.should_schedule_reply_wait)

    def test_wait_requires_visible_bubbles_for_each_initiating_stage(self):
        for stage in ("owner", "heartbeat", "webhook"):
            with self.subTest(stage=stage):
                arguments = self.arguments(stage, wait=True)
                reply, error = self.parse(stage, arguments, visible=False)
                self.assertIsNone(reply)
                self.assertEqual(error, "reply_expectation_without_visible_bubble")
                reply, error = self.parse(stage, arguments)
                self.assertIsNone(error)
                self.assertTrue(reply.should_schedule_reply_wait)
                self.assertEqual(reply.reply_wait_delay_minutes, 3)

    def test_silent_close_is_allowed_except_for_required_followup(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                reply, error = self.parse(
                    stage, self.arguments(stage), visible=False
                )
                if stage == "reply_followup":
                    self.assertIsNone(reply)
                    self.assertEqual(error, "reply_followup_bubble_required")
                else:
                    self.assertIsNone(error)
                    self.assertIsNotNone(reply)

    def test_followup_cannot_start_another_wait(self):
        reply, error = self.parse(
            "reply_followup", self.arguments("reply_followup", wait=True)
        )
        self.assertIsNone(reply)
        self.assertEqual(error, "reply_followup_cannot_schedule_another_wait")

    def test_workflow_specific_state_cannot_cross_stages(self):
        for stage in self.STAGES:
            for field in ("activity", "heartbeat"):
                with self.subTest(stage=stage, field=field):
                    arguments = self.arguments(stage)
                    if field in arguments:
                        del arguments[field]
                        expected = (
                            "invalid_activity_decision"
                            if field == "activity" else "invalid_heartbeat_state"
                        )
                    else:
                        arguments[field] = (
                            {"decision": "unchanged"}
                            if field == "activity"
                            else self.arguments("heartbeat")["heartbeat"]
                        )
                        expected = (
                            "activity_update_not_allowed"
                            if field == "activity" else "heartbeat_state_not_allowed"
                        )
                    reply, error = self.parse(stage, arguments)
                    self.assertIsNone(reply)
                    self.assertEqual(error, expected)

    def test_end_turn_keeps_owner_rule_that_bubbles_use_send_bubbles(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                arguments = {**self.arguments(stage), "bubbles": ["一起玩吧"]}
                reply, error = self.parse(stage, arguments)
                self.assertIsNone(reply)
                self.assertEqual(error, "bubbles_not_allowed_in_end_turn")

    def test_heartbeat_review_time_respects_runtime_limits(self):
        for minutes in (4, 61):
            with self.subTest(minutes=minutes):
                arguments = self.arguments("heartbeat")
                arguments["heartbeat"]["next_check_minutes"] = minutes
                reply, error = self.parse("heartbeat", arguments)
                self.assertIsNone(reply)
                self.assertEqual(error, "heartbeat_interval_out_of_range")

    def test_goal_and_maintenance_keep_their_own_terminal_tools(self):
        for stage in (
            "goal", "reflection", "memory_maintenance",
            "episode_consolidate", "episode_anneal",
        ):
            with self.subTest(stage=stage):
                reply, error = self.parse(stage, self.arguments("webhook"))
                self.assertIsNone(reply)
                self.assertEqual(error, "end_turn_not_allowed")


if __name__ == "__main__":
    unittest.main()
