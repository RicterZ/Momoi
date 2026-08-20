import tempfile
import unittest
from pathlib import Path

from momoi.memory_tools import MemoryTools
from momoi.models import IncomingMessage, ToolCall, TurnDraft
from momoi.policies import (
    ContextPolicy,
    DaemonPolicy,
    MemoryPolicy,
    RuntimePolicies,
)
from momoi.runtime.daemon import _message_gap_bounds
from momoi.runtime.turn_support import MAX_CONSECUTIVE_TOOL_FAILURES
from momoi.storage import Store
from momoi.storage.memory import memory_expires_at


class RuntimePolicyDefaultsTests(unittest.TestCase):
    def test_defaults_match_existing_runtime_behavior(self):
        policies = RuntimePolicies()
        self.assertEqual(policies.daemon, DaemonPolicy())
        self.assertEqual(policies.context, ContextPolicy())
        self.assertEqual(policies.memory, MemoryPolicy())
        self.assertEqual(MAX_CONSECUTIVE_TOOL_FAILURES, 3)
        self.assertEqual(policies.context.max_visible_goals, 8)
        self.assertEqual(policies.context.max_visible_reminders, 8)
        self.assertEqual(policies.memory.lexical_overlap_floor, 0.1)
        self.assertEqual(_message_gap_bounds("短句"), (4.0, 5.0))
        self.assertEqual(_message_gap_bounds("中等长度" * 8), (5.0, 6.0))
        self.assertEqual(_message_gap_bounds("长消息" * 30), (6.0, 7.0))

    def test_default_memory_ttl_policy_is_applied(self):
        now = 100.0
        self.assertEqual(memory_expires_at("recent", 0, now), now + 3600)
        self.assertEqual(memory_expires_at("recent", 999, now), now + 168 * 3600)

    def test_injected_memory_policy_is_used_end_to_end(self):
        policy = MemoryPolicy(2, 12, 0.5)
        now = 100.0
        self.assertEqual(
            memory_expires_at("recent", 1, now, policy), now + 2 * 3600
        )
        self.assertEqual(
            memory_expires_at("recent", 99, now, policy), now + 12 * 3600
        )
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3", memory_policy=policy)
            tools = MemoryTools(store, policy)
            result = tools.execute(
                ToolCall(
                    "call-1",
                    "memory_remember",
                    {
                        "kind": "episodic",
                        "key": "policy.test",
                        "content": "这件事",
                        "evidence": "记住这件事",
                        "activation": "recent",
                        "ttl_hours": 1,
                    },
                ),
                [IncomingMessage("event-1", "owner", "记住这件事", 1, now)],
                TurnDraft(),
            )
            self.assertEqual(result["error"], "invalid_ttl")
            store.close()

    def test_injected_overlap_floor_controls_reflection_recall(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(
                Path(directory) / "momoi.sqlite3",
                memory_policy=MemoryPolicy(lexical_overlap_floor=0.6),
            )
            with store._db:
                store._db.execute(
                    """INSERT INTO reflections
                       (id, local_date, state, scheduled_at, created_at, completed_at)
                       VALUES ('reflection:policy', '2030-01-01', 'completed', 1, 1, 1)"""
                )
                store._db.execute(
                    """INSERT INTO reflection_memories
                       (kind, key, content, evidence, confidence,
                        source_reflection_id, created_at, updated_at)
                       VALUES ('practice', 'policy.overlap', 'alpha beta',
                               'alpha beta', 0.8, 'reflection:policy', 1, 1)"""
                )
            self.assertEqual(store.search_reflection_memories("alpha gamma", 3), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
