from tests.support import provider_catalog
import tempfile
import unittest
import uuid
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime import MomoiDaemon


def config(directory: str) -> AppConfig:
    return AppConfig(
        providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        transcript_turns_min=4,
        transcript_turns_max=4,
        episode_raw_tail_turns=2,
        memory_results=2,
        database=Path(directory) / "momoi.sqlite3",
        log_level="INFO",
    )


class RecallEpisodeBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_directory_follows_transcript_episode_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            for suffix, title in (("inside", "窗口内经历"), ("outside", "窗口外经历")):
                event = IncomingMessage(suffix, suffix, title, 1, 1)
                daemon.store.add_event(event)
                turn_id = f"turn-{suffix}"
                daemon.store.begin_turn(turn_id, "owner", [event.event_id])
                daemon.store.commit_turn(
                    [event],
                    event.text,
                    AgentReply(["知道了"]),
                    turn_id=turn_id,
                )
                daemon.store.create_episode(title, episode_id=f"episode-{suffix}")
                daemon.store.link_turn_to_episode(f"episode-{suffix}", turn_id)

            candidates = daemon.owner_context_candidates(
                ["turn-inside"],
                {"turn-inside": "T1"},
            )["candidate_episodes"]

            self.assertIn("id=episode-inside", candidates)
            self.assertIn("title=窗口内经历", candidates)
            self.assertIn("turns=T1", candidates)
            self.assertIn("last_activity=", candidates)
            self.assertNotIn("episode-outside", candidates)
            self.assertNotIn("status=", candidates)
            self.assertNotIn("summary=", candidates)
            self.assertNotIn("open_loops=", candidates)
            daemon.store.close()

    async def test_new_episode_ref_is_resolved_before_owner_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            event = IncomingMessage("episode:new", "1", "开始整理书房", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])

            await daemon.submit_owner_context(
                [event],
                turn_id,
                {
                    "units": [
                        {
                            "intent": "整理书房",
                            "recall_mode": "search",
                            "recall_queries": [
                                {
                                    "semantic": "此前整理书房的计划与进展",
                                    "keywords": ["书房", "整理"],
                                }
                            ],
                            "recall_from_turn_id": "",
                            "episode": {
                                "action": "new",
                                "ref": "new:study-cleanup",
                                "title": "整理书房",
                            },
                        }
                    ]
                },
            )
            daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["开始吧"]),
                turn_id=turn_id,
            )

            expected = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"momoi:episode:{turn_id}:1:new:study-cleanup",
            ).hex
            linked = daemon.store._db.execute(
                "SELECT episode_id, unit_ids_json FROM episode_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            self.assertEqual(linked["episode_id"], expected)
            self.assertEqual(daemon.store.episode(expected)["title"], "整理书房")
            daemon.store.close()

    async def test_unknown_continue_target_is_rejected_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            event = IncomingMessage("episode:bad", "1", "继续", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])

            with self.assertRaisesRegex(ValueError, "episode reference"):
                await daemon.submit_owner_context(
                    [event],
                    turn_id,
                    {
                        "units": [
                            {
                                "intent": "继续当前经历",
                                "recall_mode": "search",
                                "recall_queries": [
                                    {
                                        "semantic": "当前经历此前的状态",
                                        "keywords": [],
                                    }
                                ],
                                "recall_from_turn_id": "",
                                "episode": {
                                    "action": "continue",
                                    "ref": "not-a-candidate",
                                    "title": "",
                                },
                            }
                        ]
                    },
                )
            self.assertIsNone(daemon.store.context_plan(turn_id))
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
