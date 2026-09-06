from tests.support import provider_catalog
import asyncio
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig, EpisodeAnnealingConfig
from momoi.integrations.models import LLMConfig
from momoi.observability.context import current_log_context
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.context.rendering import assemble_main_context
from momoi.runtime.workflows.episode import (
    render_episode_annealing_request,
    render_episode_consolidation_request,
)
from momoi.runtime.workflows.episode import (
    EPISODE_CLASSIFY_TURNS_SPEC,
    EPISODE_CONSOLIDATION_FINISH_SPEC,
    EPISODE_SUMMARY_FINISH_SPEC,
)
from momoi.runtime.turn_support import (
    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
    EPISODE_SUMMARY_SYSTEM_PROMPT,
)
from momoi.storage import (
    EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS,
    estimate_tokens,
)


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


def workflow_response(
    name: str,
    arguments: dict[str, object],
    *,
    call_id: str = "workflow-call",
    reasoning: str = "",
) -> ProviderResponse:
    call = ToolCall(call_id, name, arguments)
    return ProviderResponse(
        [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        ],
        [call],
        reasoning=reasoning,
    )


def workflow_calls_response(
    items: list[tuple[str, dict[str, object]]],
) -> ProviderResponse:
    calls = [
        ToolCall(f"workflow-call-{index}", name, arguments)
        for index, (name, arguments) in enumerate(items, 1)
    ]
    return ProviderResponse(
        [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
            for call in calls
        ],
        calls,
    )


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
    async def test_scheduler_ignores_deferrals_after_eight_hours_without_llm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.commit_turn(
                [], "expired", AgentReply([]), turn_id="deferred-expired"
            )
            daemon.store.commit_turn(
                [], "fresh", AgentReply([]), turn_id="deferred-fresh"
            )
            now = time.time()
            with daemon.store._db:
                daemon.store._db.executemany(
                    """INSERT INTO episode_consolidation_decisions
                       (turn_id, action, reason, processed_at)
                       VALUES (?, 'deferred', ?, ?)""",
                    [
                        (
                            "deferred-expired",
                            "waiting for context",
                            now - EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS,
                        ),
                        (
                            "deferred-fresh",
                            "still waiting for context",
                            now - EPISODE_CONSOLIDATION_DEFER_TIMEOUT_SECONDS + 60,
                        ),
                    ],
                )

            stop = asyncio.Event()
            worker = asyncio.create_task(daemon._scheduler_worker(stop))
            for _ in range(100):
                expired = daemon.store._db.execute(
                    """SELECT action FROM episode_consolidation_decisions
                       WHERE turn_id='deferred-expired'"""
                ).fetchone()
                if expired["action"] == "ignored":
                    break
                await asyncio.sleep(0.01)
            stop.set()
            daemon.agenda_changed.set()
            await worker

            decisions = {
                str(row["turn_id"]): (str(row["action"]), str(row["reason"]))
                for row in daemon.store._db.execute(
                    """SELECT turn_id, action, reason
                       FROM episode_consolidation_decisions"""
                ).fetchall()
            }
            self.assertEqual(
                decisions["deferred-expired"],
                ("ignored", "defer_timeout_8h"),
            )
            self.assertEqual(
                decisions["deferred-fresh"],
                ("deferred", "still waiting for context"),
            )
            self.assertTrue(daemon.episode_annealing_requested.is_set())
            self.assertEqual(
                daemon.store._db.execute(
                    """SELECT COUNT(*) FROM turns
                       WHERE workflow_kind='episode_consolidate'"""
                ).fetchone()[0],
                0,
            )
            daemon.store.close()

    async def test_restart_recovers_interrupted_episode_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = MomoiDaemon(config(directory))
            first.store.create_episode("长期项目", episode_id="episode-main")
            for ordinal in range(1, 6):
                add_turn(first, ordinal)
            candidate = first.store.claim_episode_annealing_candidate(2, 30)
            self.assertIsNotNone(candidate)
            anneal_turn_id = first._turn_id(
                "episode-anneal",
                "episode-main",
                candidate["through_ordinal"],
            )
            consolidate_turn_id = first._turn_id(
                "episode-consolidate", "pending-1", "through:"
            )
            first.store.begin_turn(
                anneal_turn_id,
                "episode_anneal",
                [
                    "episode-anneal:episode-main:"
                    f"{candidate['through_ordinal']}"
                ],
            )
            first.store.begin_turn(
                consolidate_turn_id,
                "episode_consolidate",
                ["episode-consolidate:pending-1"],
            )
            first.store.close()

            recovered = MomoiDaemon(config(directory))

            self.assertIsNone(
                recovered.store.episode("episode-main")["summary_claimed_at"]
            )
            rows = recovered.store._db.execute(
                """SELECT id, state, stage, failure_reason FROM turns
                   WHERE id IN (?, ?) ORDER BY id""",
                (anneal_turn_id, consolidate_turn_id),
            ).fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["state"], "cancelled")
                self.assertEqual(row["stage"], "cancelled")
                self.assertEqual(
                    row["failure_reason"],
                    "process_restart_interrupted_episode_maintenance",
                )

            self.assertEqual(
                recovered.store.begin_turn(
                    anneal_turn_id,
                    "episode_anneal",
                    [
                        "episode-anneal:episode-main:"
                        f"{candidate['through_ordinal']}"
                    ],
                ),
                "running",
            )
            revived = recovered.store._db.execute(
                "SELECT state, stage, failure_reason FROM turns WHERE id=?",
                (anneal_turn_id,),
            ).fetchone()
            self.assertEqual(revived["state"], "running")
            self.assertEqual(revived["stage"], "started")
            self.assertIsNone(revived["failure_reason"])
            recovered.store.close()

    async def test_episode_maintenance_uses_owner_idle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                replace(
                    config(directory),
                    episode_annealing=EpisodeAnnealingConfig(
                        idle_seconds=0.02,
                        max_seconds=1,
                    ),
                )
            )
            daemon.store.commit_turn(
                [], "pending", AgentReply([]), turn_id="pending-1"
            )
            loop = asyncio.get_running_loop()
            daemon._last_owner_activity_at = loop.time()

            started_at = loop.time()
            ready = await asyncio.wait_for(
                daemon._wait_for_episode_annealing_ready(asyncio.Event()),
                timeout=0.2,
            )

            self.assertTrue(ready)
            self.assertGreaterEqual(loop.time() - started_at, 0.015)
            daemon.store.close()

    async def test_full_consolidation_batch_uses_owner_idle_timeout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                replace(
                    config(directory),
                    episode_annealing=EpisodeAnnealingConfig(
                        idle_seconds=0.02,
                        max_seconds=1,
                    ),
                )
            )
            for ordinal in range(1, 7):
                daemon.store.commit_turn(
                    [],
                    f"pending-{ordinal}",
                    AgentReply([]),
                    turn_id=f"pending-{ordinal}",
                )
            daemon._last_owner_activity_at = asyncio.get_running_loop().time()

            started_at = asyncio.get_running_loop().time()
            ready = await asyncio.wait_for(
                daemon._wait_for_episode_annealing_ready(asyncio.Event()),
                timeout=0.2,
            )

            self.assertTrue(ready)
            self.assertGreaterEqual(
                asyncio.get_running_loop().time() - started_at,
                0.015,
            )
            daemon.store.close()

    async def test_partial_consolidation_is_never_claimed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.store.commit_turn(
                [], "pending", AgentReply([]), turn_id="pending-1"
            )
            candidates: list[dict[str, object]] = []

            async def consolidate(candidate: dict[str, object]) -> bool:
                candidates.append(candidate)
                return True

            daemon._consolidate_episode_turns = consolidate  # type: ignore[method-assign]

            self.assertFalse(await daemon._run_episode_annealing_once())
            self.assertEqual(candidates, [])
            daemon.store.close()

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
                    self.assertEqual(tools, [EPISODE_SUMMARY_FINISH_SPEC])
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
                    return workflow_response(
                        "episode_summary_finish",
                        {
                            "claims": claims,
                            "narrative_summary": "",
                            "emotional_context": {
                                "owner": "",
                                "momoi": "",
                                "tone": "",
                            },
                            "outcomes": [],
                        },
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
            with self.assertRaisesRegex(RuntimeError, "workflow protocol"):
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
                    return workflow_response(
                        "episode_summary_finish",
                        response,
                    )

            daemon.provider = Provider()  # type: ignore[assignment]
            with self.assertRaisesRegex(RuntimeError, "does not match raw history"):
                await daemon._anneal_episode_history("turn-5")
            episode = daemon.store.episode("episode-main")
            self.assertEqual(episode["working_summary"], "")
            self.assertEqual(episode["summarized_through_ordinal"], 0)
            self.assertEqual(episode["summary_failure_count"], 1)
            self.assertIsNone(episode["summary_abandoned_at"])
            daemon.store.close()

    async def test_summary_tool_stores_narrative_emotion_and_outcomes(self) -> None:
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
                    return workflow_response(
                        "episode_summary_finish",
                        {
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
                with self.assertRaisesRegex(RuntimeError, "workflow protocol"):
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
            for ordinal in range(1, 7):
                daemon.store.commit_turn(
                    [],
                    f"ping-{ordinal}",
                    AgentReply([]),
                    turn_id=f"pending-{ordinal}",
                )

            class Provider:
                systems: list[object] = []
                contexts: list[dict[str, object]] = []
                consolidation_round = 0

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
                        provider_self.consolidation_round += 1
                        self.assertEqual(
                            _tools,
                            [
                                EPISODE_CLASSIFY_TURNS_SPEC,
                                EPISODE_CONSOLIDATION_FINISH_SPEC,
                            ],
                        )
                        if provider_self.consolidation_round == 2:
                            assistant = messages[-2]["content"]
                            self.assertNotIn(
                                "reasoning",
                                [block.get("type") for block in assistant],
                            )
                            return workflow_response(
                                "episode_consolidation_finish", {}
                            )
                        ids = re.findall(
                            r"^  turn id: (.+)$",
                            prompt_section(
                                str(messages[0]["content"]), "pending_turns"
                            ),
                            re.MULTILINE,
                        )
                        self.assertEqual(len(ids), 6)
                        response = workflow_calls_response(
                            [
                                (
                                    "episode_classify_turns",
                                    {
                                        "decisions": [
                                            {
                                                "action": "ignore",
                                                "turn_ids": ids[:3],
                                                "reason": "low information",
                                            }
                                        ]
                                    },
                                ),
                                (
                                    "episode_classify_turns",
                                    {
                                        "decisions": [
                                            {
                                                "action": "ignore",
                                                "turn_ids": ids[3:-1],
                                                "reason": "low information",
                                            },
                                            {
                                                "action": "defer",
                                                "turn_ids": [ids[-1]],
                                                "reason": "needs later context",
                                            },
                                        ]
                                    },
                                ),
                            ]
                        )
                        return ProviderResponse(
                            response.content,
                            response.tool_calls,
                            reasoning="split the fixed batch safely",
                        )
                    message = annealing_items(
                        str(messages[0]["content"]), "new_messages", "Message"
                    )[0]
                    return workflow_response(
                        "episode_summary_finish",
                        {
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
                    )

            provider = Provider()
            daemon.provider = provider  # type: ignore[assignment]
            self.assertTrue(await daemon._run_episode_annealing_once())
            self.assertEqual(
                provider.systems,
                [
                    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
                    EPISODE_CONSOLIDATION_SYSTEM_PROMPT,
                    EPISODE_SUMMARY_SYSTEM_PROMPT,
                ],
            )
            self.assertEqual(
                [context["stage"] for context in provider.contexts],
                ["episode_consolidate", "episode_consolidate", "episode_anneal"],
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
                       WHERE turn_id='pending-6'"""
                ).fetchone()["action"],
                "deferred",
            )
            self.assertIsNone(daemon.store.claim_episode_consolidation_candidate())
            self.assertEqual(
                daemon.store.episode("episode-main")["narrative_summary"],
                "项目讨论仍在继续。",
            )
            daemon.store.close()

    async def test_consolidation_allows_any_number_of_valid_tool_rounds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            for ordinal in range(1, 7):
                daemon.store.commit_turn(
                    [],
                    f"pending-{ordinal}",
                    AgentReply([]),
                    turn_id=f"pending-{ordinal}",
                )
            candidate = daemon.store.claim_episode_consolidation_candidate()
            self.assertIsNotNone(candidate)
            turn_ids = [str(turn["turn_id"]) for turn in candidate["turns"]]
            calls = 0

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    tools: list[dict[str, object]],
                    **kwargs: object,
                ) -> ProviderResponse:
                    nonlocal calls
                    calls += 1
                    assert tools == [
                        EPISODE_CLASSIFY_TURNS_SPEC,
                        EPISODE_CONSOLIDATION_FINISH_SPEC,
                    ]
                    assert kwargs["require_tool"] is True
                    if calls == 7:
                        return workflow_response(
                            "episode_consolidation_finish", {}
                        )
                    action = "defer" if calls == 6 else "ignore"
                    return workflow_response(
                        "episode_classify_turns",
                        {
                            "decisions": [
                                {
                                    "action": action,
                                    "turn_ids": [turn_ids[calls - 1]],
                                    "reason": "bounded test decision",
                                }
                            ]
                        },
                        call_id=f"classify-{calls}",
                    )

            daemon.provider = Provider()  # type: ignore[assignment]
            self.assertTrue(await daemon._consolidate_episode_turns(candidate))
            self.assertEqual(calls, 7)
            self.assertEqual(
                daemon.store.episode_consolidation_remaining(turn_ids), []
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
