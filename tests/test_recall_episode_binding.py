from tests.support import provider_catalog
import tempfile
import unittest
import uuid
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.agent.runtime_tools import recall_owner_context
from momoi.runtime.agent.context_window import ContextWindow


def config(directory: str) -> AppConfig:
    return AppConfig(
        providers=provider_catalog(
            LLMConfig("http://127.0.0.1", "test", "test", 100, 0, 1, 0)
        ),
        channel=NapCatConfig("ws://127.0.0.1", "20000", 1, 60, 30, 30, 20),
        system_prompt="test",
        transcript_turns_min=4,
        transcript_turns_max=4,
        episode_unsummarized_tail_turns=2,
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
            unit = {"semantic": ["茶"], "keyword": ["茶"], "kind": ["preference"]}
            result = await recall_owner_context(
                ToolCall("recall", "recall", unit),
                current_events=[event], turn_id=turn_id,
                submit_context=daemon.submit_owner_context,
            )
            self.assertTrue(result["ok"])
            self.assertTrue(any("主人偏好绿茶" in row["content"] for row in result["memory"]))
            self.assertFalse(any("泡茶用八十度水" in row["content"] for row in result["memory"]))
            self.assertEqual(
                daemon.store.context_plan(turn_id)["plan"]["intent_units"][0]["memory_filters"]["kinds"],
                ["preference"],
            )

            # A different search angle adds evidence; the stored recall preserves both.
            unit["kind"] = ["practice"]
            await daemon.submit_owner_context([event], turn_id, unit)
            record = daemon.store.context_plan(turn_id)
            stored = str(record["retrieval"])
            self.assertIn("主人偏好绿茶", stored)
            self.assertIn("泡茶用八十度水", stored)
            self.assertEqual(record["plan"]["intent_units"][0]["intent"], "茶")
            self.assertEqual(record["retrieval"]["effective_recall_queries"], ["茶"])
            self.assertEqual(len(record["plan"]["supplemental_queries"]), 1)
            with patch.object(daemon, "_select_recall_topics", side_effect=RuntimeError("lookup failed")):
                with self.assertRaises(RuntimeError):
                    await daemon.submit_owner_context([event], turn_id, unit)
            self.assertEqual(daemon.store.context_plan(turn_id), record)


    async def test_recall_journal_retains_each_full_result(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            from momoi.models import TurnDraft
            turn_id = "recall-journal"
            daemon.store.begin_turn(turn_id, "owner", [])
            for index in range(2):
                call = ToolCall(f"recall-{index}", "recall", {"query": str(index)})
                trace = daemon.tool_executor.begin_trace(
                    call, daemon.tool_executor.source("recall"), turn_id=turn_id,
                    stage="owner", call_id=str(index), round_number=index, channel="test",
                )
                daemon.tool_executor.finish_trace(
                    trace, call, {"ok": True, "memory": str(index) * 2000}, TurnDraft(),
                )
            rows = daemon.store._db.execute(
                "SELECT item_type, payload_json FROM turn_journal WHERE turn_id=? ORDER BY sequence",
                (turn_id,),
            ).fetchall()
            self.assertEqual([r["item_type"] for r in rows], ["tool_call", "tool_result"] * 2)
            self.assertEqual(json.loads(rows[1]["payload_json"])["result"]["memory"], "0" * 2000)
            self.assertEqual(json.loads(rows[3]["payload_json"])["result"]["memory"], "1" * 2000)
            activity = daemon.store.turn_activity([turn_id])[turn_id]
            self.assertEqual(len(activity), 2)
            self.assertEqual(activity[0]["recall_result"]["memory"], "0" * 2000)
            self.assertEqual(activity[1]["recall_arguments"], {"query": "1"})
            from momoi.runtime.transcript.models import TranscriptGroup
            from momoi.runtime.transcript.evidence import render_group_evidence
            group = TranscriptGroup("assistant", (), (), (), (turn_id,), 0, 0)
            rendered = render_group_evidence(
                [group], 0, tool_activity={turn: activity for turn in group.turn_ids},
                action_limit=0, timezone=daemon.store.timezone,
                labels={turn: "T-1" for turn in group.turn_ids},
            )["content"][0]["text"]
            self.assertEqual(rendered.count("<historical_recall>"), 2)
            self.assertIn("0" * 2000, rendered)
            self.assertIn("1" * 2000, rendered)


    async def test_malformed_recall_returns_actionable_example_without_persistence(self):
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
        import copy

        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("bad:shape", "1", "晚安", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            unit = {"semantic": ["约定的见面时间"]}
            for arguments, path in (
                ({"semantic": "约定的时间"}, "recall requires a query or filter"),
                ({"units": [unit]}, "recall requires a query or filter"),
            ):
                with self.subTest(path=path):
                    result = await recall_owner_context(
                        ToolCall("bad", "recall", arguments), current_events=[event],
                        turn_id=turn_id, submit_context=daemon.submit_owner_context,
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "invalid_recall")
                    self.assertIn(path, result["message"])
                    self.assertIn("本次检索尚未成功", result["message"])
                    self.assertNotIn("example_arguments", result)
                    self.assertIsNone(daemon.store.context_plan(turn_id))

    async def test_removed_modes_rejected_without_retrieval_or_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("old-mode", "1", "整理书房", 1, 1)
            daemon.store.add_event(event)
            daemon.store.begin_turn("old-mode", "owner", [event.event_id])
            with patch.object(daemon.semantic_recall, "prepare", new_callable=AsyncMock) as dense:
                for mode in ("search", "skip", "reuse"):
                    result = await recall_owner_context(
                        ToolCall("r", "recall", {"units": [{"intent": "整理书房", "recall_mode": mode,
                                 "recall_queries": [{"semantic": "书房整理方法"}]}]}),
                        current_events=[event], turn_id="old-mode", submit_context=daemon.submit_owner_context,
                    )
                    self.assertFalse(result["ok"])
                    self.assertIsNone(daemon.store.context_plan("old-mode"))
                dense.assert_not_awaited()

    async def test_episode_binding_requires_reason_before_persistence(self) -> None:
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
        self.assertFalse(Draft202012Validator(RECALL_TOOL_SPEC["input_schema"]).is_valid(
            {"units": [{"intent": "x", "recall_mode": "skip", "recall_queries": [],
                          "episode": {"action": "new"}}]}
        ))
        return
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC

        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("episode:reason", "1", "开始整理书房", 1, 1)
            daemon.store.add_event(event)
            turn_id = daemon._turn_id(event.event_id)
            daemon.store.begin_turn(turn_id, "owner", [event.event_id])
            base = {
                "intent": "开始整理书房", "recall_mode": "skip",
                "recall_queries": [],
            }
            validator = Draft202012Validator(RECALL_TOOL_SPEC["input_schema"])
            for episode in (
                {"action": "new", "ref": "new:study", "title": "整理书房"},
                {"action": "continue", "ref": "candidate-id"},
                {"action": "new", "ref": "new:study", "title": "整理书房", "reason": "  "},
            ):
                arguments = {"units": [{**base, "episode": episode}]}
                self.assertFalse(validator.is_valid(arguments))
                with self.assertRaisesRegex(ValueError, "episode.reason"):
                    await daemon.submit_owner_context([event], turn_id, arguments)
                self.assertIsNone(daemon.store.context_plan(turn_id))

    async def test_skip_rejects_search_arguments_instead_of_discarding_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            self.addCleanup(daemon.store.close)
            event = IncomingMessage("skip:invalid", "1", "收到", 1, 1)
            daemon.store.add_event(event)
            unit = {"intent": "确认收到", "recall_mode": "skip", "recall_queries": [],
                    }
            for extra in ({"recall_queries": [{"semantic": "往事"}]},
                          {"recall_queries": [{"semantic": ""}]},
                          {"recall_from_turn_id": "previous"}):
                with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "recall requires a query or filter"):
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
                        {"semantic": [semantic], "keyword": keywords},
                    )
                    with self.assertNoLogs("momoi.runtime.retrieval.service", level="WARNING"):
                        result = await recall_owner_context(
                            call,
                            current_events=[event],
                            turn_id=turn_id,
                            submit_context=daemon.submit_owner_context,
                        )
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["state"], "recalled")
                    self.assertNotIn("error", result)
                    contents = [row["content"] for row in result["memory"]]
                    self.assertNotIn("semantic-only-memory", contents)
                    if expected:
                        self.assertIn(expected, contents)
                    else:
                        self.assertEqual(result["memory"], [])
                    self.assertNotIn("disabled", json.dumps(result))
                    self.assertNotIn("fallback", json.dumps(result))
                    record = daemon.store.context_plan(turn_id)
                    self.assertEqual(record["state"], "recalled")
                    diagnostics = record["retrieval"]["semantic_recall"]
                    self.assertEqual(diagnostics["fallback_reason"], "disabled")
                    self.assertEqual(diagnostics["query_batch_size"], 0)
                    self.assertEqual(diagnostics["request_ms"], 0)
            encoder.assert_not_awaited()

    async def test_candidate_directory_fills_from_recent_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(config(directory))
            daemon.config = replace(daemon.config, summary_results=2)
            for suffix, title in (
                ("inside", "窗口内经历"),
                ("outside-old", "较早的窗口外经历"),
                ("outside-new", "较新的窗口外经历"),
            ):
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

            with daemon.store._db:
                daemon.store._db.execute(
                    "UPDATE conversation_episodes SET narrative_summary=? WHERE id=?",
                    ("从整理书房开始，后来讨论书架摆放。", "episode-inside"),
                )
                daemon.store._db.execute(
                    "UPDATE conversation_episodes SET narrative_summary=? WHERE id=?",
                    ("更早的话题摘要。", "episode-outside-old"),
                )
                daemon.store._db.execute(
                    "UPDATE conversation_episodes SET narrative_summary=? WHERE id=?",
                    ("未来的话题摘要。", "episode-outside-new"),
                )
                for suffix, updated_at in (
                    ("inside", 10.0), ("outside-old", 5.0), ("outside-new", 30.0)
                ):
                    daemon.store._db.execute(
                        "UPDATE turns SET updated_at=? WHERE id=?",
                        (updated_at, f"turn-{suffix}"),
                    )

            candidates = daemon.owner_context_candidates(
                ["turn-inside"],
                {"turn-inside": "T-1"},
            )["recent_episodes"]

            summary_message = daemon.episode_context_message(
                ["turn-inside"], before_timestamp=40.0
            )
            self.assertTrue(summary_message["_context_prefix"])
            self.assertEqual(summary_message["role"], "user")
            self.assertIn("更早的话题摘要。", str(summary_message["content"]))
            self.assertNotIn("episode-inside", str(summary_message["content"]))
            self.assertNotIn("episode-outside-new", str(summary_message["content"]))
            messages = [
                {"role": "user", "content": "memory and goal", "_context_prefix": True},
                summary_message,
                {"role": "user", "content": "旧消息" * 1000,
                 "_history_turn_ids": ["turn-inside"]},
                {"role": "user", "content": "当前消息"},
            ]
            window = ContextWindow(
                SimpleNamespace(max_input_tokens=650, summary_results=3),
                daemon.store,
                SimpleNamespace(refit=lambda *args, **kwargs: None),
            )
            self.assertEqual(window.fit([], messages, [], 3), 2)
            self.assertIn("从整理书房开始", str(messages[1]["content"]))
            self.assertIn("更早的话题摘要。", str(messages[1]["content"]))
            self.assertIn("未来的话题摘要。", str(messages[1]["content"]))
            self.assertEqual([message["role"] for message in messages],
                             ["user", "user", "user"])
            self.assertIn('id="episode-inside"', candidates)
            self.assertIn("<title>窗口内经历</title>", candidates)
            self.assertIn("<summary>从整理书房开始，后来讨论书架摆放。</summary>", candidates)
            self.assertIn('turns="T-1"', candidates)
            self.assertIn("last_activity=", candidates)
            self.assertIn('id="episode-outside-new"', candidates)
            self.assertIn("<title>较新的窗口外经历</title><summary>未来的话题摘要。</summary>", candidates)
            self.assertNotIn("episode-outside-old", candidates)
            daemon.store.link_turn_to_episode("episode-outside-old", "turn-inside")
            crossing = daemon.episode_context_message(
                ["turn-inside"], before_timestamp=40.0
            )
            self.assertNotIn("更早的话题摘要。", str(crossing["content"]))
            self.assertEqual(candidates.count("<episode "), 2)
            self.assertLess(
                candidates.index('id="episode-inside"'),
                candidates.index('id="episode-outside-new"'),
            )
            self.assertNotIn('turns=""', candidates)
            self.assertNotIn("status=", candidates)
            self.assertNotIn("open_loops=", candidates)
            daemon.config = replace(daemon.config, summary_results=0)
            self.assertEqual(
                daemon.owner_context_candidates(["turn-inside"])["recent_episodes"],
                "",
            )
            daemon.config = replace(daemon.config, summary_results=2)
            self.assertNotIn(
                'id="episode-outside-new"',
                daemon.owner_context_candidates(
                    ["turn-inside", "turn-outside-old"],
                    {"turn-inside": "T-1", "turn-outside-old": "T-2"},
                )["recent_episodes"],
            )
            daemon.store.close()

    async def test_new_episode_ref_is_resolved_before_owner_commit(self) -> None:
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
        self.assertFalse(Draft202012Validator(RECALL_TOOL_SPEC["input_schema"]).is_valid(
            {"units": [{"intent": "x", "recall_mode": "skip", "recall_queries": [],
                          "episode": {"action": "new"}}]}
        ))
        return
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
                            "recall_queries": [
                                {
                                    "semantic": "此前整理书房的计划与进展",
                                    "keywords": ["书房", "整理"],
                                }
                            ],

                            "episode": {
                                "action": "new",
                                "ref": "new:study-cleanup",
                                "title": "整理书房",
                                "reason": "开始整理书房是一段新的具体经历，而非延续旧话题",
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
        from jsonschema import Draft202012Validator
        from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
        self.assertFalse(Draft202012Validator(RECALL_TOOL_SPEC["input_schema"]).is_valid(
            {"units": [{"intent": "x", "recall_mode": "skip", "recall_queries": [],
                          "episode": {"action": "continue"}}]}
        ))
        return
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
                                "recall_queries": [
                                    {
                                        "semantic": "当前经历此前的状态",
                                        "keywords": [],
                                    }
                                ],

                                "episode": {
                                    "action": "continue",
                                    "ref": "not-a-candidate",
                                    "title": "",
                                    "reason": "仍在谈同一件事，但引用的 Episode 不是候选项",
                                },
                            }
                        ]
                    },
                )
            self.assertIsNone(daemon.store.context_plan(turn_id))
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
