import asyncio
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config import AppConfig, LLMConfig
from momoi.logging_context import current_log_context
from momoi.models import AgentReply, IncomingMessage, ProviderResponse
from momoi.runtime import MomoiDaemon
from momoi.runtime.context_assembler import assemble_main_context
from momoi.runtime.episode_prompt_renderer import (
    parse_episode_summary_result,
    render_episode_annealing_request,
    render_episode_consolidation_request,
)
from momoi.runtime.turn_support import (
    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
    EPISODE_SUMMARY_SYSTEM_PROMPT,
)
from momoi.storage import estimate_tokens


def prompt_section(prompt: str, name: str) -> str:
    match = re.search(rf"<{name}>\n(.*?)\n</{name}>", prompt, re.DOTALL)
    if match is None:
        raise AssertionError(f"missing prompt section: {name}")
    return match.group(1)


def annealing_items(prompt: str, section: str, label: str) -> list[dict[str, object]]:
    text = prompt_section(prompt, section)
    if text == "none":
        return []
    blocks = re.split(rf"\n\n(?={label} \d+ \[)", text)
    items: list[dict[str, object]] = []
    for block in blocks:
        item: dict[str, object] = {}
        for key in ("message_id", "turn_id", "ordinal"):
            match = re.search(rf"(?:\[| \| ){key}=([^|\]]+)", block)
            if match is None:
                raise AssertionError(f"missing {key} in {label}")
            item[key] = (
                int(match.group(1))
                if key in {"message_id", "ordinal"}
                else match.group(1).strip()
            )
        exact_label = "QUOTE" if label == "Claim" else "CONTENT"
        content = re.search(
            rf"<exact_{exact_label.lower()}>\n(.*?)\n"
            rf"</exact_{exact_label.lower()}>",
            block,
            re.DOTALL,
        )
        if content is None:
            raise AssertionError(f"missing exact content in {label}")
        item["quote" if label == "Claim" else "content"] = content.group(1)
        items.append(item)
    return items


def latest_pending_turn_id(prompt: str) -> str:
    ids = re.findall(
        r"^  turn id: (.+)$",
        prompt_section(prompt, "pending_turns"),
        re.MULTILINE,
    )
    if not ids:
        raise AssertionError("missing pending Turn")
    return ids[-1]


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
    def test_consolidation_prompt_is_human_readable_and_drops_storage_metadata(
        self,
    ) -> None:
        prompt = render_episode_consolidation_request(
            {
                "turns": [
                    {
                        "turn_id": "turn-1",
                        "timestamp": "updated-at-is-redundant",
                        "messages": [
                            {
                                "id": 91,
                                "turn_id": "turn-1",
                                "role": "user",
                                "content": "今天把项目做完了",
                                "created_at": 123.0,
                                "delivery_state": "received",
                                "timestamp": "2026-08-20T12:00:00+08:00",
                            }
                        ],
                    }
                ],
                "context_turns": [],
                "candidate_episodes": [
                    {
                        "id": "episode-project",
                        "title": "完成项目",
                        "status": "closing",
                        "narrative_summary": "老师完成了项目。",
                        "topics": ["项目"],
                        "entities": [],
                        "open_loops": [],
                    }
                ],
            }
        )

        self.assertTrue(prompt.startswith("<pending_turns>\nTurn 1"))
        self.assertIn("[OWNER timestamp=2026-08-20T12:00:00+08:00]", prompt)
        self.assertIn("今天把项目做完了", prompt)
        self.assertIn("<candidate_episodes>\nEpisode 1", prompt)
        self.assertNotIn("created_at", prompt)
        self.assertNotIn("updated-at-is-redundant", prompt)
        self.assertNotIn("message id:", prompt)
        self.assertFalse(prompt.startswith("{"))

    def test_annealing_prompt_preserves_exact_quoteable_text(self) -> None:
        raw = '第一行 <tag attr="x"> & \\n\n第二行：不要改空白'
        prompt = render_episode_annealing_request(
            {
                "id": "episode-1",
                "title": "一次讨论",
                "working_summary_claims": [
                    {
                        "message_id": 7,
                        "turn_id": "turn-old",
                        "ordinal": 1,
                        "role": "user",
                        "delivery_state": "received",
                        "quote": "原有结论 <保持原样>",
                    }
                ],
            },
            [
                {
                    "id": 8,
                    "turn_id": "turn-new",
                    "ordinal": 2,
                    "role": "assistant",
                    "delivery_state": "internal",
                    "timestamp": "2026-08-20T13:00:00+08:00",
                    "content": raw,
                }
            ],
        )

        self.assertIn("<previous_verified_claims>\nClaim 1", prompt)
        self.assertIn("source=MOMOI delivery=internal", prompt)
        self.assertIn(
            "<exact_content>\n"
            + raw
            + "\n</exact_content>",
            prompt,
        )
        self.assertNotIn("&lt;", prompt)
        self.assertNotIn("\\u", prompt)
        self.assertFalse(prompt.startswith("{"))

    async def test_maintenance_timeout_uses_episode_retry_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_config = config(directory)
            app_config = replace(
                app_config,
                episode_annealing=replace(
                    app_config.episode_annealing,
                    max_seconds=0.01,
                ),
            )
            daemon = MomoiDaemon(app_config)
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)

            class Provider:
                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")

            daemon.provider = Provider()  # type: ignore[assignment]
            with self.assertRaises(TimeoutError):
                await daemon._run_episode_annealing_once()

            episode = daemon.store.episode("episode-main")
            self.assertIsNone(episode["summary_claimed_at"])
            self.assertEqual(episode["summary_failure_count"], 1)
            self.assertIsNotNone(episode["summary_retry_at"])
            maintenance_turn = daemon.store._db.execute(
                """SELECT state, failure_reason FROM turns
                   WHERE source_ids_json LIKE '%episode-anneal:%'"""
            ).fetchone()
            self.assertEqual(maintenance_turn["state"], "running")
            self.assertEqual(maintenance_turn["failure_reason"], "TimeoutError")
            daemon.store.close()

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

    async def test_closed_empty_episode_is_eligible_for_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("已经结束的讨论", episode_id="closed-empty")
            event = IncomingMessage("event-1", "event-1", "讨论已经完成", 1, 1)
            daemon.store.add_event(event)
            daemon.store.commit_turn(
                [event],
                event.text,
                AgentReply(["好的"]),
                turn_id="turn-1",
            )
            outbox_id = daemon.store._db.execute(
                "SELECT id FROM outbox WHERE turn_id='turn-1'"
            ).fetchone()["id"]
            daemon.store.mark_sent(int(outbox_id))
            daemon.store.link_turn_to_episode("closed-empty", "turn-1")
            with daemon.store._db:
                daemon.store._db.execute(
                    """UPDATE conversation_episodes
                       SET status='closed', closed_at=updated_at
                       WHERE id='closed-empty'"""
                )

            candidate = daemon.store.claim_episode_annealing_candidate(2, 1000)

            self.assertEqual(candidate["episode"]["id"], "closed-empty")
            self.assertEqual(candidate["through_ordinal"], 1)
            self.assertTrue(candidate["messages"])
            daemon.store.release_episode_annealing("closed-empty", failed=False)
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
                prompts: list[str] = []

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    self.assertEqual(system, EPISODE_SUMMARY_SYSTEM_PROMPT)
                    self.assertEqual(tools, [])
                    prompt = str(messages[0]["content"])
                    provider_self.prompts.append(prompt)
                    claims = [
                        {
                            name: claim[name]
                            for name in ("message_id", "turn_id", "ordinal", "quote")
                        }
                        for claim in annealing_items(
                            prompt, "previous_verified_claims", "Claim"
                        )
                    ]
                    message = annealing_items(prompt, "new_messages", "Message")[0]
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
                                    {
                                        "version": 2,
                                        "claims": claims,
                                        "narrative_summary": "",
                                        "emotional_context": {},
                                        "outcomes": [],
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        [],
                    )

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            self.assertTrue(await daemon._run_episode_annealing_once())

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
            maintenance_turn = daemon.store._db.execute(
                """SELECT state, failure_reason FROM turns
                   WHERE source_ids_json LIKE '%episode-anneal:%'"""
            ).fetchone()
            self.assertEqual(
                (maintenance_turn["state"], maintenance_turn["failure_reason"]),
                ("completed", None),
            )
            self.assertEqual(
                {
                    item["ordinal"]
                    for item in annealing_items(
                        provider.prompts[0], "new_messages", "Message"
                    )
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
            context = assemble_main_context(daemon.store, retrieval, 10000)[
                "episodes"
            ]
            self.assertIn("source OWNER turn=turn-1 ordinal=1", context)
            self.assertNotIn("第4轮主人消息", context)
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
                annealing_items(
                    provider.prompts[1], "previous_verified_claims", "Claim"
                )[0]["quote"],
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
            self.assertEqual(
                daemon.store.next_episode_annealing_retry_at(),
                episode["summary_retry_at"],
            )
            self.assertEqual(episode["summarized_through_ordinal"], 0)
            daemon.store.close()

    async def test_closed_episode_failure_schedules_worker_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("已结束的项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)
            with daemon.store._db:
                daemon.store._db.execute(
                    """UPDATE conversation_episodes SET status='closed'
                       WHERE id='episode-main'"""
                )
            candidate = daemon.store.claim_episode_annealing_candidate(2, 10000)
            self.assertIsNotNone(candidate)

            daemon.store.release_episode_annealing("episode-main")

            episode = daemon.store.episode("episode-main")
            self.assertEqual(
                daemon.store.next_episode_annealing_retry_at(),
                episode["summary_retry_at"],
            )
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
                    message = annealing_items(
                        str(messages[0]["content"]), "new_messages", "Message"
                    )[0]
                    response = {
                        "version": 2,
                        "claims": [
                            {
                                "message_id": message["message_id"],
                                "turn_id": message["turn_id"],
                                "ordinal": message["ordinal"],
                                "quote": "主人答应了原文里不存在的事情",
                            }
                        ],
                        "narrative_summary": "",
                        "emotional_context": {},
                        "outcomes": [],
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
            self.assertIsNone(episode["summary_abandoned_at"])
            daemon.store.close()

    async def test_v2_summary_stores_narrative_emotion_and_outcomes(self) -> None:
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
                    message = annealing_items(
                        str(messages[0]["content"]), "new_messages", "Message"
                    )[0]
                    return ProviderResponse(
                        [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "version": 2,
                                        "claims": [
                                            {
                                                "message_id": message["message_id"],
                                                "turn_id": message["turn_id"],
                                                "ordinal": message["ordinal"],
                                                "quote": f"第{message['ordinal']}轮",
                                            }
                                        ],
                                        "narrative_summary": "主人和桃衣持续讨论长期项目。",
                                        "emotional_context": {
                                            "owner": "投入",
                                            "momoi": "配合",
                                            "tone": "合作",
                                        },
                                        "outcomes": ["完成一次阶段讨论"],
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        [],
                    )

            daemon.provider = Provider()  # type: ignore[assignment]
            self.assertTrue(await daemon._run_episode_annealing_once())
            episode = daemon.store.episode("episode-main")
            self.assertEqual(
                episode["narrative_summary"],
                "主人和桃衣持续讨论长期项目。",
            )
            self.assertEqual(episode["emotional_context"]["tone"], "合作")
            self.assertEqual(episode["outcomes"], ["完成一次阶段讨论"])
            daemon.store.close()

    def test_summary_parser_rejects_obsolete_or_malformed_shapes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid result"):
            parse_episode_summary_result('{"version":1,"claims":[]}')
        with self.assertRaisesRegex(RuntimeError, "invalid outcomes"):
            parse_episode_summary_result(
                json.dumps(
                    {
                        "version": 2,
                        "claims": [],
                        "narrative_summary": "",
                        "emotional_context": {},
                        "outcomes": [{"outcome": "老师已下班，当天工作结束"}],
                    },
                    ensure_ascii=False,
                )
            )

    async def test_third_failure_abandons_episode_and_other_lines_continue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("卡住的旧线", episode_id="episode-stuck")
            for ordinal in range(1, 6):
                add_named_turn(daemon, "episode-stuck", ordinal, "stuck")

            class Provider:
                async def complete(self, *_: object, **__: object) -> ProviderResponse:
                    return ProviderResponse([], [])

            daemon.provider = Provider()  # type: ignore[assignment]
            for attempt in range(3):
                daemon.store._db.execute(
                    "UPDATE conversation_episodes SET summary_retry_at=0 WHERE id=?",
                    ("episode-stuck",),
                )
                daemon.store._db.commit()
                claimed = daemon.store.claim_episode_annealing_candidate(2, 10000)
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed["episode"]["id"], "episode-stuck")
                with self.assertRaisesRegex(RuntimeError, "no text"):
                    await daemon._anneal_episode_history(
                        f"turn-stuck-{attempt}",
                        candidate=claimed,
                    )

            daemon.store.create_episode("还能退的线", episode_id="episode-ready")
            for ordinal in range(1, 6):
                add_named_turn(daemon, "episode-ready", ordinal, "ready")

            stuck = daemon.store.episode("episode-stuck")
            self.assertEqual(stuck["summary_failure_count"], 3)
            self.assertIsNotNone(stuck["summary_abandoned_at"])
            self.assertIsNone(stuck["summary_retry_at"])
            self.assertIsNone(
                daemon.store.next_episode_annealing_retry_at()
            )

            claimed = daemon.store.claim_episode_annealing_candidate(2, 10000)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["episode"]["id"], "episode-ready")
            daemon.store.release_episode_annealing("episode-ready", failed=False)

            add_named_turn(daemon, "episode-stuck", 6, "stuck")
            revived = daemon.store.episode("episode-stuck")
            self.assertIsNone(revived["summary_abandoned_at"])
            self.assertEqual(revived["summary_failure_count"], 0)
            daemon.store.close()

    async def test_deferred_consolidation_does_not_block_anneal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(daemon, ordinal)
            daemon.store.commit_turn(
                [], "ping", AgentReply([]), turn_id="lonely-ping"
            )

            class Provider:
                systems: list[object] = []
                contexts: list[dict[str, object]] = []

                async def complete(
                    provider_self,
                    system: object,
                    messages: list[dict[str, object]],
                    _tools: list[dict[str, object]],
                    **_: object,
                ) -> ProviderResponse:
                    provider_self.systems.append(system)
                    provider_self.contexts.append(current_log_context())
                    if system == EPISODE_CONSOLIDATION_SYSTEM_PROMPT:
                        latest = latest_pending_turn_id(str(messages[0]["content"]))
                        return ProviderResponse(
                            [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "version": 1,
                                            "decisions": [
                                                {
                                                    "action": "defer",
                                                    "turn_ids": [latest],
                                                    "reason": "needs later context",
                                                }
                                            ],
                                        },
                                        ensure_ascii=False,
                                    ),
                                }
                            ],
                            [],
                        )
                    message = annealing_items(
                        str(messages[0]["content"]), "new_messages", "Message"
                    )[0]
                    return ProviderResponse(
                        [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "version": 2,
                                        "claims": [
                                            {
                                                "message_id": message["message_id"],
                                                "turn_id": message["turn_id"],
                                                "ordinal": message["ordinal"],
                                                "quote": f"第{message['ordinal']}轮",
                                            }
                                        ],
                                        "narrative_summary": "项目讨论仍在继续。",
                                        "emotional_context": {
                                            "owner": "",
                                            "momoi": "",
                                            "tone": "",
                                        },
                                        "outcomes": [],
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        [],
                    )

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            self.assertTrue(await daemon._run_episode_annealing_once())
            self.assertEqual(
                provider.systems,
                [
                    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
                    EPISODE_SUMMARY_SYSTEM_PROMPT,
                ],
            )
            self.assertEqual(
                [context["stage"] for context in provider.contexts],
                ["episode_consolidate", "episode_anneal"],
            )
            self.assertTrue(
                all(context.get("turn_id") for context in provider.contexts)
            )
            self.assertTrue(
                all(context.get("call_id") for context in provider.contexts)
            )
            self.assertEqual(
                daemon.store._db.execute(
                    """SELECT action FROM episode_consolidation_decisions
                       WHERE turn_id='lonely-ping'"""
                ).fetchone()["action"],
                "deferred",
            )
            self.assertIsNone(daemon.store.claim_episode_consolidation_candidate())
            self.assertEqual(
                daemon.store.episode("episode-main")["narrative_summary"],
                "项目讨论仍在继续。",
            )
            daemon.store.close()


def add_named_turn(
    daemon: MomoiDaemon, episode_id: str, ordinal: int, prefix: str
) -> None:
    event_id = f"{prefix}-event-{ordinal}"
    turn_id = f"{prefix}-turn-{ordinal}"
    event = IncomingMessage(
        event_id, event_id, f"{prefix}第{ordinal}轮主人消息", ordinal, ordinal
    )
    daemon.store.add_event(event)
    daemon.store.begin_turn(turn_id, "owner", [event_id])
    daemon.store.commit_turn(
        [event],
        event.text,
        AgentReply([f"{prefix}第{ordinal}轮桃衣回复"]),
        turn_id=turn_id,
    )
    outbox_id = daemon.store._db.execute(
        "SELECT id FROM outbox WHERE turn_id=?", (turn_id,)
    ).fetchone()["id"]
    daemon.store.mark_sent(int(outbox_id))
    daemon.store.link_turn_to_episode(episode_id, turn_id)
