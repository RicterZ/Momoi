import unittest

from momoi.policies import (
    ContextPolicy,
    DaemonPolicy,
    MemoryPolicy,
    RuntimePolicies,
)
from momoi.runtime.dispatch.delivery import message_gap_bounds
from momoi.runtime.turn_support import MAX_CONSECUTIVE_TOOL_FAILURES


class RuntimePolicyDefaultsTests(unittest.TestCase):
    def test_defaults_match_existing_runtime_behavior(self):
        policies = RuntimePolicies()
        self.assertEqual(policies.daemon, DaemonPolicy())
        self.assertEqual(policies.context, ContextPolicy())
        self.assertEqual(policies.memory, MemoryPolicy())
        self.assertEqual(MAX_CONSECUTIVE_TOOL_FAILURES, 3)
        self.assertEqual(policies.context.max_visible_goals, 8)
        self.assertEqual(message_gap_bounds("短句"), (4.0, 5.0))
        self.assertEqual(message_gap_bounds("中等长度" * 8), (5.0, 6.0))
        self.assertEqual(message_gap_bounds("长消息" * 30), (6.0, 7.0))

    def test_injected_memory_policy_is_used_end_to_end(self):
        policy = MemoryPolicy(recent_max_ttl_hours=12)
        from momoi.runtime.workflows.memory_operation.parsing import parse_decisions
        import time

        operation = {"id": "op", "type": "add", "event_id": "event"}
        decision = {
            "operation_ids": ["op"],
            "action": "write",
            "reason": "temporary",
            "target_ids": [],
            "evidence": [{"event_id": "event", "quote": "临时"}],
            "memory": {
                "kind": "episodic",
                "key": "temporary",
                "content": "临时",
                "activation": "recent",
                "expires_at": time.time() + 24 * 3600,
            },
        }
        with self.assertRaisesRegex(ValueError, "configured lifetime"):
            parse_decisions(
                {"decisions": [decision]},
                [operation],
                {},
                {"event": "临时"},
                policy.recent_max_ttl_hours,
            )


if __name__ == "__main__":
    unittest.main()
