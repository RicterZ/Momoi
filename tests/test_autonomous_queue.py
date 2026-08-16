import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.runtime.daemon import (
    HEARTBEAT_QUEUE_ITEM,
    REFLECTION_QUEUE_PREFIX,
    MomoiDaemon,
)
from momoi.runtime.jobs import AutonomousJob


class AutonomousQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_reflection_goal_heartbeat_rotation_is_stable(self):
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
                    recent_raw_tokens=1000,
                    recent_turns=2,
                    memory_results=2,
                    memory_tokens=1000,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.autonomous.put_nowait(HEARTBEAT_QUEUE_ITEM)
            daemon.autonomous.put_nowait(REFLECTION_QUEUE_PREFIX + "2030-01-01")
            daemon.autonomous.put_nowait("goal-1")
            self.assertEqual(
                await daemon._next_work(),
                ("goal", AutonomousJob.reflection("2030-01-01")),
            )
            self.assertEqual(
                await daemon._next_work(), ("goal", AutonomousJob.goal("goal-1"))
            )
            self.assertEqual(
                await daemon._next_work(), ("goal", AutonomousJob.heartbeat())
            )
            daemon.store.close()

    def test_legacy_strings_and_reserved_goal_ids_are_unambiguous(self):
        self.assertEqual(
            AutonomousJob.from_legacy(HEARTBEAT_QUEUE_ITEM),
            AutonomousJob.heartbeat(),
        )
        self.assertEqual(
            AutonomousJob.from_legacy(REFLECTION_QUEUE_PREFIX + "2030-01-01"),
            AutonomousJob.reflection("2030-01-01"),
        )
        self.assertEqual(
            AutonomousJob.goal(HEARTBEAT_QUEUE_ITEM).kind,
            "goal",
        )


if __name__ == "__main__":
    unittest.main()
