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
        return result

    def parse(self, stage, arguments, *, visible=True):
        execution = TurnExecutionSpec(
            stage, goal_id="existing-goal" if stage == "goal" else None
        )
        return parse_end_turn(
            arguments,
            execution=execution,
            visible_since_owner_update=visible,
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

    def test_business_fields_are_rejected_by_every_chat_stage(self):
        for stage in self.STAGES:
            for field in ("heartbeat", "goal", "unknown"):
                with self.subTest(stage=stage, field=field):
                    reply, error = self.parse(stage, {**self.arguments(stage), field: {}})
                    self.assertIsNone(reply)
                    self.assertEqual(error, "unexpected_end_turn_fields")

    def test_end_turn_keeps_owner_rule_that_bubbles_use_send_bubbles(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                arguments = {**self.arguments(stage), "bubbles": ["一起玩吧"]}
                reply, error = self.parse(stage, arguments)
                self.assertIsNone(reply)
                self.assertEqual(error, "bubbles_not_allowed_in_end_turn")

    def test_private_maintenance_keeps_its_own_terminal_tools(self):
        for stage in (
            "reflection", "memory_maintenance",
            "episode_consolidate", "episode_anneal",
        ):
            with self.subTest(stage=stage):
                reply, error = self.parse(stage, self.arguments("webhook"))
                self.assertIsNone(reply)
                self.assertEqual(error, "end_turn_not_allowed")


if __name__ == "__main__":
    unittest.main()


class EndTurnSchemaTest(unittest.TestCase):
    def test_decision_schema_matches_runtime_validation(self):
        import json
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC

        schema = END_TURN_TOOL_SPEC['input_schema']
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        updated = EndTurnTest().arguments('heartbeat')['mood']
        waiting = EndTurnTest().arguments('heartbeat', wait=True)['reply_wait']
        moods = [
            ({'decision': 'unchanged'}, True), (updated, True),
            (json.dumps({'decision': 'unchanged'}), False),
            (json.dumps(updated), False), ('calm', False), ({}, False),
            ({'decision': 'unchanged', 'state': 'calm'}, False),
            ({**updated, 'decision': 'unknown'}, False),
            ({k: v for k, v in updated.items() if k != 'cause'}, False),
            ({**updated, 'intensity': 2}, False),
            ({**updated, 'intensity': '0.3'}, False),
            ({**updated, 'unexpected': 1}, False),
        ]
        waits = [
            ({'wait': False}, True), (waiting, True),
            (json.dumps({'wait': False}), False), (False, False), ({}, False),
            ({'wait': True}, False), ({'wait': 'false'}, False),
            ({'wait': False, 'delay_minutes': 3}, False),
            ({**waiting, 'delay_minutes': 0}, False),
            ({**waiting, 'unexpected': 1}, False),
        ]
        for mood, valid_mood in moods:
            for wait, valid_wait in waits:
                with self.subTest(mood=mood, wait=wait):
                    args = {'mood': mood, 'reply_wait': wait}
                    expected = valid_mood and valid_wait
                    self.assertEqual(validator.is_valid(args), expected)
                    reply, error = EndTurnTest().parse('heartbeat', args)
                    self.assertEqual(reply is not None and error is None, expected)
        self.assertTrue(validator.is_valid({}))
        self.assertFalse(validator.is_valid('{}'))

    def test_each_stage_schema_requires_exact_private_state(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.conversation import end_turn_tool_spec

        arguments = EndTurnTest()
        expected = {
            'owner': {'reply_wait', 'mood'},
            'heartbeat': {'reply_wait', 'mood'},
            'webhook': {'reply_wait', 'mood'},
            'reply_followup': {'reply_wait', 'mood'},
            'goal': set(),
        }
        for stage, required in expected.items():
            with self.subTest(stage=stage):
                spec = end_turn_tool_spec(stage)
                schema = spec['input_schema']
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                args = {} if stage == 'goal' else arguments.arguments(stage)
                self.assertEqual(set(schema['required']), required)
                self.assertTrue(validator.is_valid(args))
                for field in required:
                    self.assertFalse(validator.is_valid({k: v for k, v in args.items() if k != field}))
                for field in {'activity', 'heartbeat', 'goal'} - required:
                    self.assertFalse(validator.is_valid({**args, field: {}}))
                if stage != 'goal':
                    self.assertFalse(validator.is_valid({**args, 'goal': None}))
                if stage == 'reply_followup':
                    self.assertFalse(validator.is_valid(arguments.arguments(stage, wait=True)))
                self.assertEqual(spec, end_turn_tool_spec(stage))

    def test_terminal_text_and_delivery_boundary(self):
        from momoi.models import ToolCall
        from momoi.runtime.agent.harness import TurnHarness

        send = ToolCall('send', 'send_bubbles', {'bubbles': ['消息']})
        end = ToolCall('end', 'end_turn', {})
        work = ToolCall('work', 'read_file', {'path': 'test'})
        for stage in ('owner', 'heartbeat', 'webhook', 'reply_followup'):
            harness = TurnHarness.for_stage(stage)
            harness.started = True
            if stage == 'heartbeat':
                harness.accept('heartbeat_activity')
            # Reply-followup must use its opening send before it is marked started.
            if stage == 'reply_followup':
                harness.started = False
                calls = [send, end]
            else:
                calls = [end]
            self.assertIsNone(harness.validate(calls, has_assistant_text=False))
            self.assertEqual(
                harness.validate(calls, has_assistant_text=True),
                None if stage == 'reply_followup' else 'send_bubbles_required_before_end_turn',
            )
            self.assertIsNone(harness.validate([send, end], has_assistant_text=True))
            self.assertIsNone(harness.validate([send, end]))
        harness = TurnHarness.for_stage('owner')
        harness.accept('recall')
        self.assertIsNotNone(harness.validate([end, send]))
        self.assertIsNotNone(harness.validate([work, end]))
        self.assertIsNotNone(harness.validate([send, end, end]))
        self.assertIsNone(harness.validate([send, end], has_assistant_text=True))
