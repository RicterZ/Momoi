import unittest

from momoi.runtime.agent.protocol import parse_end_turn
from momoi.runtime.agent.workflow import TurnExecutionSpec


class EndTurnTest(unittest.TestCase):
    STAGES = ("owner", "heartbeat", "webhook")

    def arguments(self, stage):
        result = {
            "mood": {
                "decision": "updated",
                "state": "frustrated",
                "intensity": 0.6,
                "cause": "The shared game ended badly",
            },
        }
        return result

    def parse(self, stage, arguments):
        execution = TurnExecutionSpec(
            stage, goal_id="existing-goal" if stage == "goal" else None
        )
        return parse_end_turn(
            arguments,
            execution=execution,
        )

    def test_all_chat_stages_return_private_state_without_messages(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                reply, error = self.parse(stage, self.arguments(stage))
                self.assertIsNone(error)
                self.assertEqual(reply.messages, [])
                self.assertEqual(reply.mood_update["state"], "frustrated")


    def test_silent_close_is_allowed_for_every_chat_stage(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                reply, error = self.parse(
                    stage, self.arguments(stage)
                )
                self.assertIsNone(error)
                self.assertIsNotNone(reply)


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
            "reflection",
            "episode_consolidate", "episode_anneal",
        ):
            with self.subTest(stage=stage):
                reply, error = self.parse(stage, self.arguments("webhook"))
                self.assertIsNone(reply)
                self.assertEqual(error, "end_turn_not_allowed")


if __name__ == "__main__":
    unittest.main()


class EndTurnSchemaTest(unittest.TestCase):
    def test_each_stage_schema_requires_exact_private_state(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.conversation import end_turn_tool_spec

        arguments = EndTurnTest()
        expected = {
            'owner': {'mood'},
            'heartbeat': {'mood'},
            'webhook': {'mood'},
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
                self.assertEqual(spec, end_turn_tool_spec(stage))
