import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.runtime.daemon import MomoiDaemon
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
            daemon.autonomous.put_nowait(AutonomousJob.heartbeat())
            daemon.autonomous.put_nowait(AutonomousJob.reflection("2030-01-01"))
            daemon.autonomous.put_nowait(AutonomousJob.goal("goal-1"))
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

if __name__ == "__main__":
    unittest.main()
