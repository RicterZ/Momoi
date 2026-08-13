import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_assembler import assemble_main_context
from momoi.runtime.turns import EPISODE_SUMMARY_SYSTEM_PROMPT
from momoi.storage import estimate_tokens


def config(directory: str) -> AppConfig:
    return AppConfig(
        llm=LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        recent_raw_tokens=10000,
        recent_turns=2,
        memory_results=2,
        memory_tokens=1000,
        database=Path(directory) / "momoi.sqlite3",
        log_level="INFO",
    )


def add_turn(daemon: MomoiDaemon, ordinal: int) -> None:
    event_id = f"event-{ordinal}"
    turn_id = f"turn-{ordinal}"
    event = IncomingMessage(event_id, event_id, f"第{ordinal}轮主人消息", ordinal, ordinal)
    daemon.store.add_event(event)
    daemon.store.begin_turn(turn_id, "owner", [event_id])
    daemon.store.commit_turn(
        [event],
        event.text,
        AgentReply([f"第{ordinal}轮桃衣回复"]),
        turn_id=turn_id,
    )
    outbox_id = daemon.store._db.execute(
        "SELECT id FROM outbox WHERE turn_id=?", (turn_id,)
    ).fetchone()["id"]
    daemon.store.mark_sent(int(outbox_id))
    daemon.store.link_turn_to_episode("episode-main", turn_id)


class EpisodeAnnealingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_annealing_releases_claim_without_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)
            started = asyncio.Event()

            class Provider:
                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    started.set()
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")

            daemon.provider = Provider()  # type: ignore[assignment]
            task = asyncio.create_task(daemon._anneal_episode_history("turn-5"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            episode = daemon.store.episode("episode-main")
            self.assertIsNone(episode["summary_claimed_at"])
            self.assertEqual(episode["summary_failure_count"], 0)
            self.assertIsNone(episode["summary_retry_at"])
            daemon.store.close()

    async def test_annealing_claims_only_one_bounded_prefix_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 11):
                add_turn(daemon, ordinal)

            candidate = daemon.store.claim_episode_annealing_candidate(2, 30)

            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["through_ordinal"], 2)
            self.assertLessEqual(
                sum(
                    estimate_tokens(str(message["content"]))
                    for message in candidate["messages"]
                ),
                30,
            )
            daemon.store.release_episode_annealing("episode-main")
            daemon.store.close()

    async def test_old_episode_prefix_progressively_merges_into_working_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)

            class Provider:
                payloads: list[dict[str, object]] = []

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.assertEqual(system, EPISODE_SUMMARY_SYSTEM_PROMPT)
                    self.assertEqual(tools, [])
                    payload = json.loads(str(messages[0]["content"]))
                    provider_self.payloads.append(payload)
                    claims = [
                        {
                            name: claim[name]
                            for name in ("message_id", "turn_id", "ordinal", "quote")
                        }
                        for claim in payload["episode"]["previous_verified_claims"]
                    ]
                    message = payload["new_messages"][0]
                    claims.append(
                        {
                            "message_id": message["message_id"],
                            "turn_id": message["turn_id"],
                            "ordinal": message["ordinal"],
                            "quote": f"第{message['ordinal']}轮",
                        }
                    )
                    return ProviderResponse(
                        [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"version": 1, "claims": claims},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        [],
                    )

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            await daemon._anneal_episode_history("turn-5")

            episode = daemon.store.episode("episode-main")
            self.assertEqual(episode["summarized_through_ordinal"], 3)
            self.assertEqual(
                episode["working_summary"],
                '- [source OWNER turn=turn-1 ordinal=1] "第1轮"',
            )
            self.assertEqual(
                {
                    item["ordinal"]
                    for item in daemon.store.episode_messages(
                        "episode-main", 10000, after_ordinal=3
                    )
                },
                {4, 5},
            )
            self.assertEqual(
                {
                    item["ordinal"]
                    for item in provider.payloads[0]["new_messages"]
                },
                {1, 2, 3},
            )

            retrieval = {
                "episodes": [
                    {
                        "episode_id": "episode-main",
                        "relation": "primary",
                        "unit_ids": ["project"],
                        "is_new": False,
                    }
                ]
            }
            context = assemble_main_context(daemon.store, retrieval, 10000, 10000)[
                "episodes"
            ]
            self.assertIn("source OWNER turn=turn-1 ordinal=1", context)
            self.assertIn("第4轮主人消息", context)
            self.assertNotIn("第1轮主人消息", context)

            for ordinal in range(6, 10):
                add_turn(daemon, ordinal)
            await daemon._anneal_episode_history("turn-9")
            episode = daemon.store.episode("episode-main")
            self.assertEqual(episode["summarized_through_ordinal"], 7)
            self.assertEqual(
                episode["working_summary"],
                '- [source OWNER turn=turn-1 ordinal=1] "第1轮"\n'
                '- [source OWNER turn=turn-4 ordinal=4] "第4轮"',
            )
            self.assertEqual(
                provider.payloads[1]["episode"]["previous_verified_claims"][0][
                    "quote"
                ],
                "第1轮",
            )
            self.assertEqual(
                {
                    item["ordinal"]
                    for item in daemon.store.episode_messages(
                        "episode-main", 10000, after_ordinal=7
                    )
                },
                {8, 9},
            )
            self.assertEqual(
                daemon.store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                18,
            )
            daemon.store.close()

    async def test_failed_annealing_releases_claim_with_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)

            class Provider:
                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    return ProviderResponse([], [])

            daemon.provider = Provider()  # type: ignore[assignment]
            with self.assertRaisesRegex(RuntimeError, "no text"):
                await daemon._anneal_episode_history("turn-5")
            episode = daemon.store.episode("episode-main")
            self.assertIsNone(episode["summary_claimed_at"])
            self.assertEqual(episode["summary_failure_count"], 1)
            self.assertIsNotNone(episode["summary_retry_at"])
            self.assertEqual(episode["summarized_through_ordinal"], 0)
            daemon.store.close()

    async def test_hallucinated_summary_quote_cannot_replace_working_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    payload = json.loads(str(messages[0]["content"]))
                    message = payload["new_messages"][0]
                    response = {
                        "version": 1,
                        "claims": [
                            {
                                "message_id": message["message_id"],
                                "turn_id": message["turn_id"],
                                "ordinal": message["ordinal"],
                                "quote": "主人答应了原文里不存在的事情",
                            }
                        ],
                    }
                    return ProviderResponse(
                        [
                            {
                                "type": "text",
                                "text": json.dumps(response, ensure_ascii=False),
                            }
                        ],
                        [],
                    )

            daemon.provider = Provider()  # type: ignore[assignment]
            with self.assertRaisesRegex(ValueError, "does not match raw history"):
                await daemon._anneal_episode_history("turn-5")
            episode = daemon.store.episode("episode-main")
            self.assertEqual(episode["working_summary"], "")
            self.assertEqual(episode["summarized_through_ordinal"], 0)
            self.assertEqual(episode["summary_failure_count"], 1)
            daemon.store.close()
