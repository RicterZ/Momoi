import tempfile
import unittest
from pathlib import Path

from momoi.tools.agenda import AgendaTools
from momoi.tools.memory import MemoryTools
from momoi.models import AgentReply, IncomingMessage, ToolCall, TurnDraft
from momoi.storage import Store


class P0GoldenTests(unittest.TestCase):
    def test_owner_turn_side_effect_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            event = IncomingMessage(
                "event-1", "message-1", "记住我喜欢乌龙茶，明天提醒我买茶", 1, 1
            )
            store.add_event(event)
            draft = TurnDraft()
            self.assertTrue(
                MemoryTools(store).execute(
                    ToolCall(
                        "memory-1",
                        "memory_operation",
                        {
                            "type": "add",
                            "content": "喜欢乌龙茶",
                            "evidence": "喜欢乌龙茶",
                        },
                    ),
                    [event],
                    draft,
                )["ok"]
            )
            self.assertTrue(
                AgendaTools(store).execute(
                    ToolCall(
                        "goal-1",
                        "goal_create",
                        {
                            "title": "提醒买乌龙茶",
                            "success_criteria": "已经提醒主人买乌龙茶",
                            "next_action": "通知主人买乌龙茶，然后完成 Goal",
                            "next_review_at": "2030-01-01T12:00:00+00:00",
                        },
                    ),
                    draft,
                    authority="owner",
                    source_event_id=event.event_id,
                )["ok"]
            )
            store.commit_turn(
                [event],
                event.text,
                AgentReply(["记住了，也安排好了。"]),
                draft,
                turn_id="turn-1",
                target_channel="test",
            )
            store.record_turn_usage("turn-1", 12, 3)
            outbox = store.due_outbox()[0]
            goal = store.list_goals()[0]
            self.assertEqual(store.list_memories(), [])
            operation = store._db.execute("SELECT state,operations_json FROM memory_operation_batches WHERE id='turn-1'").fetchone()
            import json
            memory = {"state": operation["state"], "content": json.loads(operation["operations_json"])[0]["content"]}
            snapshot = {
                "outbox": {
                    "turn_id": outbox.turn_id,
                    "text": outbox.text,
                    "state": outbox.state,
                    "channel": outbox.channel,
                },
                "memory": memory,
                "goal": {
                    key: goal[key]
                    for key in ("title", "status", "next_review_at", "schedule")
                },
                "usage": store.turn_usage("turn-1"),
                "turn": dict(
                    store._db.execute(
                        "SELECT state, stage, failure_reason FROM turns WHERE id='turn-1'"
                    ).fetchone()
                ),
            }
            snapshot["usage"].pop("started_at")
            self.assertEqual(
                snapshot,
                {
                    "outbox": {
                        "turn_id": "turn-1",
                        "text": "记住了，也安排好了。",
                        "state": "pending",
                        "channel": "test",
                    },
                    "memory": {
                        "state": "pending",
                        "content": "喜欢乌龙茶",
                    },
                    "goal": {
                        "title": "提醒买乌龙茶",
                        "status": "active",
                        "next_review_at": 1893499200.0,
                        "schedule": None,
                    },
                    "usage": {"llm_calls": 1, "input": 12, "output": 3},
                    "turn": {
                        "state": "completed",
                        "stage": "completed",
                        "failure_reason": None,
                    },
                },
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
