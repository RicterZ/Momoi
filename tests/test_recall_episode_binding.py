from tests.support import provider_catalog
import tempfile
import unittest
import uuid
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.runtime_tools import recall_owner_context


def config(directory: str) -> AppConfig:
    return AppConfig(
        providers=provider_catalog(
            LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)
        ),
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
    async def test_recall_kind_allowlist_limits_confirmed_and_reflection_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            now = 10.0
            with daemon.store._db:
                daemon.store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES (?, 'tea.preference', '主人偏好绿茶', 'recall', 'owner', 'seed', '绿茶', 0.5, ?, ?)""",
                    ("preference", now, now),
                )
                daemon.store._db.execute(
                    """INSERT INTO memories
                       (kind, key, content, activation, authority, source_event_id,
                        evidence_quote, importance, created_at, updated_at)
                       VALUES (?, 'tea.practice', '泡茶用八十度水', 'recall', 'owner', 'seed', '八十度', 0.5, ?, ?)""",
                    ("practice", now, now),
                )
            event = IncomingMessage("kind-filter", "owner", "茶", 20, 20)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])

            async def select_all(_system, _messages, tools, **_kwargs):
                memory_count = tools[0]["input_schema"]["properties"]["memory_indices"]["maxItems"]
                return ProviderResponse([], [ToolCall(
                    "selection", "select_topics",
                    {"indices": [], "memory_indices": list(range(memory_count)),
                     "reflection_indices": []},
                )])

            daemon.provider = SimpleNamespace(complete=select_all)
            unit = {
                "intent": "茶的信息",
                "kind": ["preference"],
                "recall_mode": "search",
                "recall_queries": [{"semantic": "茶", "keywords": ["茶"]}],
                "recall_from_turn_id": "",
                "episode": {"action": "none", "ref": "", "title": ""},
            }
            result = await recall_owner_context(
                ToolCall("recall", "recall", {"units": [unit]}),
                current_events=[event], turn_id=turn_id,
                submit_context=daemon.submit_owner_context,
            )
            self.assertTrue(result["ok"])
            self.assertIn("主人偏好绿茶", result["memory"])
            self.assertNotIn("泡茶用八十度水", result["memory"])
            self.assertEqual(
                daemon.store.context_plan(turn_id)["plan"]["intent_units"][0]["kind"],
                ["preference"],
            )

    async def test_malformed_recall_returns_actionable_example_without_persistence(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC, RECALL_SKIP_EXAMPLE
        import copy

        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("bad:shape", "1", "晚安", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            unit = copy.deepcopy(RECALL_SKIP_EXAMPLE["units"][0])
            for arguments, path in (
                (unit, "units:"),
                ({"units": [{**unit, "recall_queries": "[]"}]}, "units[0].recall_queries"),
                ({"units": [{**unit, "episode": '{"action":"none"}'}]}, "units[0].episode"),
            ):
                with self.subTest(path=path):
                    result = await recall_owner_context(
                        ToolCall("bad", "recall", arguments), current_events=[event],
                        turn_id=turn_id, submit_context=daemon.submit_owner_context,
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "invalid_recall")
                    self.assertIn(path, result["message"])
                    self.assertIn("has not succeeded yet", result["message"])
                    Draft202012Validator(RECALL_TOOL_SPEC["input_schema"]).validate(result["example_arguments"])
                    self.assertIsNone(daemon.store.context_plan(turn_id))

    async def test_skip_preserves_episode_routing_without_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("skip:new", "1", "开始整理书房", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            unit = {
                "intent": "主人开始整理书房",
                "recall_mode": "skip", "recall_queries": [], "recall_from_turn_id": "",
                "episode": {"action": "new", "ref": "new:study", "title": "整理书房"},
            }
            with patch.object(daemon.semantic_recall, "prepare", new_callable=AsyncMock) as dense:
                result = await recall_owner_context(
                    ToolCall("recall", "recall", {"units": [unit]}),
                    current_events=[event], turn_id=turn_id,
                    submit_context=daemon.submit_owner_context,
                )
            dense.assert_not_awaited()
            self.assertTrue(result["ok"])
            self.assertIn("no_retrieval_units=u1", result["status"])
            record = daemon.store.context_plan(turn_id)
            self.assertEqual(record["state"], "recalled")
            self.assertEqual(record["plan"]["intent_units"][0]["recall"]["mode"], "skip")
            for field in ("recall_memories", "reflection_memories", "episodes", "effective_recall_queries"):
                self.assertEqual(record["retrieval"][field], [])
            self.assertEqual(daemon.store.recall_reuse_candidates([turn_id]), [])
            daemon.store.commit_turn([event], event.text, AgentReply([]), turn_id=turn_id)
            linked = daemon.store._db.execute(
                "SELECT episode_id FROM episode_turns WHERE turn_id=?", (turn_id,),
            ).fetchone()
            self.assertEqual(daemon.store.episode(linked["episode_id"])["title"], "整理书房")

    async def test_skip_rejects_search_arguments_instead_of_discarding_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("skip:invalid", "1", "收到", 1, 1)
            daemon.store.add_event(event)
            unit = {"intent": "确认收到", "recall_mode": "skip", "recall_queries": [],
                    "recall_from_turn_id": "", "episode": {"action": "none"}}
            for extra in ({"recall_queries": [{"semantic": "往事"}]},
                          {"recall_queries": [{"semantic": ""}]},
                          {"recall_from_turn_id": "previous"}):
                with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "skip requires empty"):
                    daemon._plan_from_submission([event], {"units": [{**unit, **extra}]},
                                                 turn_id="invalid", revision=1)

    async def test_disabled_embedding_recall_uses_only_keywords_without_tool_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            self.assertFalse(daemon.config.providers.enabled("embedding"))
            self.assertIsNone(daemon.services.embedding)
            # Fail loudly if the disabled branch ever tries to invoke the encoder.
            encoder = AsyncMock(
                side_effect=AssertionError("disabled embedding was called")
            )
            daemon.semantic_recall.client = SimpleNamespace(encode=encoder)

            async def select_all(_system, _messages, tools, **_kwargs):
                memory_count = tools[0]["input_schema"]["properties"]["memory_indices"]["maxItems"]
                return ProviderResponse([], [ToolCall(
                    "selection", "select_topics",
                    {"indices": [], "memory_indices": list(range(memory_count)),
                     "reflection_indices": []},
                )])

            daemon.provider = SimpleNamespace(complete=select_all)
            semantic = "semantic-only-sentinel"
            with daemon.store._db:
                for key, content in (
                    ("keyword-anchor", "keyword-only-memory"),
                    (semantic, "semantic-only-memory"),
                ):
                    daemon.store._db.execute(
                        """INSERT INTO memories
                           (kind, key, content, activation, authority, source_event_id,
                            evidence_quote, importance, created_at, updated_at)
                           VALUES ('shared', ?, ?, 'recall', 'owner', 'seed', ?, 0.5, 1, 1)""",
                        (key, content, content),
                    )
            for index, (keywords, expected) in enumerate(
                [
                    (["keyword-anchor"], "keyword-only-memory"),
                    (["unmatched-anchor"], ""),
                    ([], ""),
                ]
            ):
                with self.subTest(keywords=keywords):
                    event = IncomingMessage(
                        f"disabled-recall-{index}",
                        "owner",
                        "test",
                        10 + index,
                        10 + index,
                    )
                    daemon.store.add_event(event)
                    turn_id = f"disabled-recall-turn-{index}"
                    daemon.store.begin_turn(turn_id, "owner", [event.event_id])
                    call = ToolCall(
                        f"recall-{index}",
                        "recall",
                        {
                            "units": [
                                {
                                    "intent": "test keyword-only recall",
                                    "recall_mode": "search",
                                    "recall_queries": [
                                        {"semantic": semantic, "keywords": keywords}
                                    ],
                                    "recall_from_turn_id": "",
                                    "episode": {
                                        "action": "none",
                                        "ref": "",
                                        "title": "",
                                    },
                                }
                            ]
                        },
                    )
                    with self.assertNoLogs("momoi.semantic.service", level="WARNING"):
                        result = await recall_owner_context(
                            call,
                            current_events=[event],
                            turn_id=turn_id,
                            submit_context=daemon.submit_owner_context,
                        )
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["state"], "recalled")
                    self.assertNotIn("error", result)
                    self.assertNotIn("semantic-only-memory", result["memory"])
                    if expected:
                        self.assertIn(expected, result["memory"])
                    else:
                        self.assertEqual(result["memory"], "")
                    self.assertNotIn("disabled", json.dumps(result))
                    self.assertNotIn("fallback", json.dumps(result))
                    record = daemon.store.context_plan(turn_id)
                    self.assertEqual(record["state"], "recalled")
                    diagnostics = record["retrieval"]["semantic_recall"]
                    self.assertEqual(diagnostics["fallback_reason"], "disabled")
                    self.assertEqual(diagnostics["query_batch_size"], 0)
                    self.assertEqual(diagnostics["request_ms"], 0)
            encoder.assert_not_awaited()

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
                {"turn-inside": "T-1"},
            )["recent_episodes"]

            self.assertIn('id="episode-inside"', candidates)
            self.assertIn("<title>窗口内经历</title>", candidates)
            self.assertIn('turns="T-1"', candidates)
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
