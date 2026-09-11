import unittest

from momoi.policies import (
    MemoryPolicy,
)


class RuntimePolicyDefaultsTests(unittest.TestCase):
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
