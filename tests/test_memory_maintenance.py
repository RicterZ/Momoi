from tests.support import provider_catalog
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.observability.context import current_log_context
from momoi.models import IncomingMessage, ProviderResponse, ToolCall
from momoi.runtime import MomoiDaemon
from momoi.runtime.workflows.memory_maintenance import (
    MEMORY_MAINTENANCE_FINISH_SPEC,
    build_atomic_memory_groups,
    filter_owner_evidence_for_memories,
    memory_snapshot_fingerprint,
    pack_memory_groups,
    parse_memory_maintenance_result,
    render_memory_maintenance_request,
)
from momoi.storage import Store


def memory(
    memory_id: int,
    key: str,
    content: str,
    *,
    activation: str = "recall",
) -> dict[str, object]:
    return {
        "id": memory_id,
        "kind": "preference",
        "key": key,
        "content": content,
        "activation": activation,
        "expires_at": None,
        "source_event_id": f"event-{memory_id}",
        "evidence_quote": content,
        "updated_at": float(memory_id),
        "superseded_by": None,
    }


class MemoryMaintenanceProtocolTest(unittest.TestCase):
    def test_finish_tool_schema_has_expected_change_variants(self) -> None:
        schema = MEMORY_MAINTENANCE_FINISH_SPEC["input_schema"]
        assert isinstance(schema, dict)
        properties = schema["properties"]
        assert isinstance(properties, dict)
        variants = properties["changes"]["items"]["oneOf"]
        self.assertEqual(len(variants), 3)
        self.assertEqual(
            [variant["properties"]["action"]["enum"][0] for variant in variants],
            ["replace", "merge", "retire"],
        )
        merge = variants[1]["properties"]
        self.assertEqual(
            merge["snapshot_fingerprints"]["additionalProperties"]["pattern"],
            "^sha256:[0-9a-f]{64}$",
        )

    def test_parser_rejects_text_wrapped_results(self) -> None:
        result, error = parse_memory_maintenance_result(
            '{"version":1,"reviewed_ids":[],"changes":[],"regroup_requests":[],"summary":""}',
            mutable_memories={},
            context_ids=set(),
            directory_ids=set(),
            owner_evidence={},
        )
        self.assertIsNone(result)
        self.assertIn("result: expected object", error)

    def test_bootstrap_groups_cover_eighty_memories_without_splitting_duplicates(
        self,
    ) -> None:
        rows = [
            memory(index, f"topic.item_{index}", f"记忆 {index}")
            for index in range(1, 81)
        ]
        rows[40]["content"] = rows[0]["content"]
        groups = build_atomic_memory_groups(rows, range(1, 81))
        duplicate_group = next(group for group in groups if 1 in group)
        self.assertEqual(duplicate_group, [1, 41])

        by_id = {int(row["id"]): row for row in rows}
        batches = pack_memory_groups(groups, by_id, 30)
        self.assertEqual(
            {memory_id for batch in batches for memory_id in batch},
            set(range(1, 81)),
        )
        self.assertTrue(
            any(1 in batch and 41 in batch for batch in batches)
        )

    def test_renderer_keeps_directory_read_only_and_includes_fingerprints(
        self,
    ) -> None:
        mutable = memory(1, "home.light", "卧室灯使用暖光。")
        context = memory(2, "home.light.exception", "阅读时使用冷光。")
        rendered = render_memory_maintenance_request(
            mutable_memories=[mutable],
            context_memories=[context],
            memory_directory=[mutable, context],
            owner_evidence=[
                {
                    "event_id": "owner-1",
                    "occurred_at": "2030-01-01T12:00:00+08:00",
                    "content": "卧室改成冷光",
                }
            ],
            topic_context="卧室照明",
        )
        self.assertIn("<mutable_memories>", rendered)
        self.assertIn(memory_snapshot_fingerprint(mutable), rendered)
        self.assertIn("<memory_directory>", rendered)
        self.assertIn("event_id=owner-1", rendered)
        directory = rendered.split("<memory_directory>", 1)[1].split(
            "</memory_directory>", 1
        )[0]
        self.assertNotIn("snapshot_fingerprint", directory)

    def test_related_recent_facets_form_one_atomic_group(self) -> None:
        rows = [
            {
                **memory(
                    194,
                    "owner.life.changshou_lake_trip_plan",
                    "计划骑摩托去长寿湖，住一晚。",
                    activation="recent",
                ),
                "kind": "episodic",
            },
            {
                **memory(
                    195,
                    "owner.life.changshou_lake_ride_estimate",
                    "单程一百多公里，需要三四个小时。",
                    activation="recent",
                ),
                "kind": "episodic",
            },
            {
                **memory(
                    196,
                    "owner.life.changshou_lake_safety",
                    "正在确认大车和雷雨风险。",
                    activation="recent",
                ),
                "kind": "episodic",
            },
            {
                **memory(
                    197,
                    "owner.life.unrelated",
                    "另一个短期安排。",
                    activation="recent",
                ),
                "kind": "episodic",
            },
        ]
        groups = build_atomic_memory_groups(rows, {194, 195, 196, 197})
        self.assertIn([194, 195, 196], groups)
        self.assertIn([197], groups)

    def test_owner_evidence_is_filtered_to_current_group(self) -> None:
        rows = [
            memory(
                1,
                "owner.life.changshou_lake_trip_plan",
                "计划骑摩托去长寿湖。",
                activation="recent",
            )
        ]
        evidence = [
            {"event_id": "trip", "content": "长寿湖单程一百多公里"},
            {"event_id": "food", "content": "今晚想吃火锅"},
        ]
        selected = filter_owner_evidence_for_memories(evidence, rows)
        self.assertEqual(
            [item["event_id"] for item in selected],
            ["trip"],
        )

    def test_parser_requires_every_mutable_id_to_be_reviewed_or_regrouped(
        self,
    ) -> None:
        mutable = {
            1: memory(1, "home.light", "卧室灯使用暖光。"),
            2: memory(2, "food.spicy", "主人不吃辣。", activation="always"),
        }
        text = {
            "version": 1,
            "reviewed_ids": [1],
            "changes": [],
            "regroup_requests": [
                {
                    "anchor_ids": [2],
                    "include_ids": [3],
                    "reason": "可能与目录中的新偏好冲突。",
                }
            ],
            "summary": "一条保留，一条重新分组。",
        }
        result, error = parse_memory_maintenance_result(
            text,
            mutable_memories=mutable,
            context_ids=set(),
            directory_ids={1, 2, 3},
            owner_evidence={},
        )
        self.assertIsNone(error)
        self.assertEqual(result["reviewed_ids"], [1])
        self.assertEqual(result["regroup_requests"][0]["anchor_ids"], [2])

        incomplete = {
            "version": 1,
            "reviewed_ids": [1],
            "changes": [],
            "regroup_requests": [],
            "summary": "",
        }
        result, error = parse_memory_maintenance_result(
            incomplete,
            mutable_memories=mutable,
            context_ids=set(),
            directory_ids={1, 2, 3},
            owner_evidence={},
        )
        self.assertIsNone(result)
        self.assertIn("mutable ids have no decision", error)

    def test_parser_validates_replace_fingerprint_and_exact_owner_quote(
        self,
    ) -> None:
        row = memory(1, "home.light", "卧室灯使用暖光。")
        payload = {
            "version": 1,
            "reviewed_ids": [],
            "changes": [
                {
                    "action": "replace",
                    "memory_id": 1,
                    "snapshot_fingerprint": memory_snapshot_fingerprint(row),
                    "content": "卧室灯使用冷光。",
                    "activation": "recall",
                    "expires_at": None,
                    "evidence": {
                        "event_id": "owner-1",
                        "quote": "卧室灯改成冷光",
                    },
                    "reason": "主人更新了灯光偏好。",
                }
            ],
            "regroup_requests": [],
            "summary": "更新卧室灯。",
        }
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories={1: row},
            context_ids=set(),
            directory_ids={1},
            owner_evidence={"owner-1": "以后卧室灯改成冷光"},
        )
        self.assertIsNone(error)
        self.assertEqual(result["changes"][0]["content"], "卧室灯使用冷光。")

        payload["changes"][0]["evidence"]["event_id"] = "qq:owner-1"
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories={1: row},
            context_ids=set(),
            directory_ids={1},
            owner_evidence={"owner-1": "以后卧室灯改成冷光"},
        )
        self.assertIsNone(result)
        self.assertIn("unknown 'qq:owner-1'", error)

        payload["changes"][0]["evidence"]["event_id"] = "owner-1"
        payload["changes"][0]["evidence"]["quote"] = "不存在的原话"
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories={1: row},
            context_ids=set(),
            directory_ids={1},
            owner_evidence={"owner-1": "以后卧室灯改成冷光"},
        )
        self.assertIsNone(result)
        self.assertIn("not an exact contiguous substring", error)

        payload["changes"][0]["evidence"]["quote"] = "卧室灯改成冷光"
        payload["changes"][0]["snapshot_fingerprint"] = "sha256:stale"
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories={1: row},
            context_ids=set(),
            directory_ids={1},
            owner_evidence={"owner-1": "以后卧室灯改成冷光"},
        )
        self.assertIsNone(result)
        self.assertIn("snapshot_fingerprint: expected", error)

    def test_parser_requires_owner_evidence_ids_for_merge(self) -> None:
        rows = {
            1: memory(1, "trip.plan", "计划去长寿湖。", activation="recent"),
            2: memory(2, "trip.time", "单程三四小时。", activation="recent"),
        }
        change = {
            "action": "merge",
            "survivor_id": 1,
            "source_ids": [2],
            "snapshot_fingerprints": {
                str(memory_id): memory_snapshot_fingerprint(row)
                for memory_id, row in rows.items()
            },
            "content": "计划去长寿湖，单程三四小时。",
            "activation": "recent",
            "expires_at": 2000000000,
            "evidence_event_ids": ["owner-1", "owner-2"],
            "reason": "同一次短期行程。",
        }
        payload = {
            "version": 1,
            "reviewed_ids": [],
            "changes": [change],
            "regroup_requests": [],
            "summary": "合并行程。",
        }
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories=rows,
            context_ids=set(),
            directory_ids=set(rows),
            owner_evidence={
                "owner-1": "计划去长寿湖",
                "owner-2": "单程三四小时",
            },
        )
        self.assertIsNone(error)
        self.assertEqual(
            result["changes"][0]["evidence_event_ids"],
            ["owner-1", "owner-2"],
        )

        change["snapshot_fingerprints"]["1"] = change["snapshot_fingerprints"][
            "1"
        ].removeprefix("sha256:")
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories=rows,
            context_ids=set(),
            directory_ids=set(rows),
            owner_evidence={
                "owner-1": "计划去长寿湖",
                "owner-2": "单程三四小时",
            },
        )
        self.assertIsNone(result)
        self.assertIn("snapshot_fingerprints.1: expected", error)
        change["snapshot_fingerprints"] = {
            str(memory_id): memory_snapshot_fingerprint(row)
            for memory_id, row in rows.items()
        }

        rows[1]["activation"] = "always"
        change["activation"] = "always"
        change["snapshot_fingerprints"] = {
            str(memory_id): memory_snapshot_fingerprint(row)
            for memory_id, row in rows.items()
        }
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories=rows,
            context_ids=set(),
            directory_ids=set(rows),
            owner_evidence={
                "owner-1": "计划去长寿湖",
                "owner-2": "单程三四小时",
            },
        )
        self.assertIsNone(result)
        self.assertIn("cannot merge memory 2", error)

        rows[1]["activation"] = "recent"
        change["activation"] = "recent"
        change["snapshot_fingerprints"] = {
            str(memory_id): memory_snapshot_fingerprint(row)
            for memory_id, row in rows.items()
        }
        change["evidence_event_ids"] = ["qq:owner-1"]
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories=rows,
            context_ids=set(),
            directory_ids=set(rows),
            owner_evidence={
                "owner-1": "计划去长寿湖",
                "owner-2": "单程三四小时",
            },
        )
        self.assertIsNone(result)
        self.assertIn("unknown 'qq:owner-1'", error)

        del change["evidence_event_ids"]
        result, error = parse_memory_maintenance_result(
            payload,
            mutable_memories=rows,
            context_ids=set(),
            directory_ids=set(rows),
            owner_evidence={
                "owner-1": "计划去长寿湖",
                "owner-2": "单程三四小时",
            },
        )
        self.assertIsNone(result)
        self.assertIn("missing keys ['evidence_event_ids']", error)


class MemoryMaintenanceStorageTest(unittest.TestCase):
    def test_apply_merge_migrates_evidence_and_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            events = [
                IncomingMessage("owner-1", "1", "卧室灯用暖光", 10, 10),
                IncomingMessage("owner-2", "2", "卧室灯还是用暖光", 20, 20),
            ]
            for event in events:
                self.assertTrue(store.add_event(event))
            first = store._db.execute(
                """INSERT INTO memories
                   (kind,key,content,activation,authority,source_event_id,
                    evidence_quote,importance,created_at,updated_at)
                   VALUES ('preference','home.light','卧室灯使用暖光。','recall',
                           'owner','owner-1','卧室灯用暖光',0.5,10,10)"""
            )
            second = store._db.execute(
                """INSERT INTO memories
                   (kind,key,content,activation,authority,source_event_id,
                    evidence_quote,importance,created_at,updated_at)
                   VALUES ('preference','home.bedroom.light','卧室灯用暖光。','recall',
                           'owner','owner-1','卧室灯用暖光',0.5,11,11)"""
            )
            first_id = int(first.lastrowid)
            second_id = int(second.lastrowid)
            store._db.executemany(
                """INSERT INTO memory_evidence
                   (memory_id,source_event_id,quote,created_at)
                   VALUES (?,?,?,?)""",
                (
                    (first_id, "owner-1", "卧室灯用暖光", 10),
                    (second_id, "owner-2", "卧室灯还是用暖光", 20),
                ),
            )
            turn_id = "maintenance-storage"
            store.queue_memory_maintenance_turn(turn_id, "manual:test")
            store.claim_memory_maintenance_turn(turn_id)
            snapshots = {
                int(item["id"]): item
                for item in store.maintenance_memory_inventory()
            }
            decision = {
                "reviewed_ids": [],
                "completed_ids": [first_id, second_id],
                "changes": [
                    {
                        "action": "merge",
                        "survivor_id": first_id,
                        "source_ids": [second_id],
                        "content": "卧室灯使用暖光。",
                        "activation": "recall",
                        "expires_at": None,
                        "evidence_event_ids": ["owner-1", "owner-2"],
                        "reason": "重复记忆",
                    }
                ],
                "regroup_requests": [],
                "summary": "合并重复项。",
            }
            store.apply_memory_maintenance_batch(
                turn_id,
                decision,
                snapshots,
                owner_marker=store.latest_owner_event_marker(),
            )
            self.assertEqual(
                store._db.execute(
                    "SELECT superseded_by FROM memories WHERE id=?",
                    (second_id,),
                ).fetchone()["superseded_by"],
                first_id,
            )
            migrated = store._db.execute(
                """SELECT source_event_id FROM memory_evidence
                   WHERE memory_id=? ORDER BY source_event_id""",
                (first_id,),
            ).fetchall()
            self.assertEqual(
                [row["source_event_id"] for row in migrated],
                ["owner-1", "owner-2"],
            )
            checkpoint = store._db.execute(
                """SELECT payload_json FROM turn_journal
                   WHERE turn_id=? AND item_type='memory_maintenance_batch'""",
                (turn_id,),
            ).fetchone()
            self.assertEqual(
                json.loads(checkpoint["payload_json"])["completed_ids"],
                [first_id, second_id],
            )
            store.close()

    def test_snapshot_change_rejects_entire_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "momoi.sqlite3")
            cursor = store._db.execute(
                """INSERT INTO memories
                   (kind,key,content,activation,authority,source_event_id,
                    evidence_quote,importance,created_at,updated_at)
                   VALUES ('preference','food.spicy','主人不吃辣。','always',
                           'owner','owner-1','我不吃辣',0.5,10,10)"""
            )
            memory_id = int(cursor.lastrowid)
            store._db.commit()
            turn_id = "maintenance-stale"
            store.queue_memory_maintenance_turn(turn_id, "manual:test")
            store.claim_memory_maintenance_turn(turn_id)
            snapshot = store.maintenance_memory_inventory()[0]
            store._db.execute(
                "UPDATE memories SET content='主人偶尔吃辣。' WHERE id=?",
                (memory_id,),
            )
            store._db.commit()
            with self.assertRaisesRegex(ValueError, "memory_snapshot_changed"):
                store.apply_memory_maintenance_batch(
                    turn_id,
                    {
                        "reviewed_ids": [],
                        "changes": [],
                        "regroup_requests": [],
                        "summary": "",
                    },
                    {memory_id: snapshot},
                    owner_marker=store.latest_owner_event_marker(),
                )
            self.assertEqual(
                store._db.execute(
                    """SELECT COUNT(*) FROM turn_journal
                       WHERE turn_id=?
                         AND item_type='memory_maintenance_batch'""",
                    (turn_id,),
                ).fetchone()[0],
                0,
            )
            store.close()


class MemoryMaintenanceExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_invalid_calls_defer_without_turn_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0)),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.store.add_event(
                IncomingMessage("owner-1", "1", "以后我可以吃辣", 20, 20)
            )
            cursor = daemon.store._db.execute(
                """INSERT INTO memories
                   (kind,key,content,activation,authority,source_event_id,
                    evidence_quote,importance,created_at,updated_at)
                   VALUES ('preference','food.spicy','主人不吃辣。','always',
                           'owner','owner-0','我不吃辣',0.5,10,10)"""
            )
            memory_id = int(cursor.lastrowid)
            daemon.store._db.commit()
            turn_id = "maintenance-invalid-protocol"
            daemon.store.queue_memory_maintenance_turn(turn_id, "manual:test")
            calls = 0

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    _tools: object,
                    **_kwargs: object,
                ) -> ProviderResponse:
                    nonlocal calls
                    calls += 1
                    call = ToolCall(
                        f"finish-{calls}",
                        "memory_maintenance_finish",
                        {
                            "version": 1,
                            "reviewed_ids": [],
                            "changes": [
                                {
                                    "action": "replace",
                                    "memory_id": memory_id,
                                    "snapshot_fingerprint": "missing-prefix",
                                    "content": "主人可以吃辣。",
                                    "activation": "always",
                                    "expires_at": None,
                                    "evidence": {
                                        "event_id": "owner-1",
                                        "quote": "我可以吃辣",
                                    },
                                    "reason": "主人更新偏好。",
                                }
                            ],
                            "regroup_requests": [],
                            "summary": "更新偏好。",
                        },
                    )
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
                    )

            daemon.provider = Provider()
            with self.assertLogs("momoi.runtime.turns", level="WARNING") as logs:
                await daemon._complete_memory_maintenance_turn(
                    turn_id, asyncio.Event()
                )
            self.assertEqual(calls, 3)
            row = daemon.store._db.execute(
                "SELECT state,stage,failure_reason FROM turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            self.assertEqual(row["state"], "running")
            self.assertEqual(row["stage"], "memory_maintenance_queued")
            self.assertIn("snapshot_fingerprint: expected", row["failure_reason"])
            self.assertTrue(
                any(
                    getattr(record, "momoi_event", "")
                    == "memory_maintenance_protocol_deferred"
                    for record in logs.records
                )
            )
            daemon.store.close()

    async def test_bootstrap_correction_applies_and_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0)),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            cursor = daemon.store._db.execute(
                """INSERT INTO memories
                   (kind,key,content,activation,authority,source_event_id,
                    evidence_quote,importance,created_at,updated_at)
                   VALUES ('preference','food.spicy','主人不吃辣。','always',
                           'owner','owner-1','我不吃辣',0.5,10,10)"""
            )
            memory_id = int(cursor.lastrowid)
            daemon.store._db.commit()
            daemon.store.add_event(
                IncomingMessage(
                    "owner-2",
                    "2",
                    "以后我可以吃辣，之前那条改掉",
                    20,
                    20,
                )
            )
            snapshot = next(
                item
                for item in daemon.store.maintenance_memory_inventory()
                if item["id"] == memory_id
            )
            turn_id = "maintenance-bootstrap"
            daemon.store.queue_memory_maintenance_turn(turn_id, "manual:test")
            call_contexts: list[dict[str, object]] = []

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    _tools: object,
                    **_kwargs: object,
                ) -> ProviderResponse:
                    assert _tools == [MEMORY_MAINTENANCE_FINISH_SPEC]
                    assert _kwargs["require_tool"] is True
                    call_contexts.append(current_log_context())
                    payload = {
                        "version": 1,
                        "reviewed_ids": (
                            [memory_id] if len(call_contexts) == 1 else []
                        ),
                        "changes": [
                            {
                                "action": "replace",
                                "memory_id": memory_id,
                                "snapshot_fingerprint": (
                                    memory_snapshot_fingerprint(snapshot)
                                ),
                                "content": "主人可以吃辣。",
                                "activation": "always",
                                "expires_at": None,
                                "evidence": {
                                    "event_id": "owner-2",
                                    "quote": "我可以吃辣",
                                },
                                "reason": "主人更新了饮食偏好。",
                            }
                        ],
                        "regroup_requests": [],
                        "summary": "建议更新饮食偏好。",
                    }
                    call = ToolCall(
                        "finish-maintenance",
                        "memory_maintenance_finish",
                        payload,
                    )
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
                    )

            daemon.provider = Provider()
            with self.assertLogs("momoi.runtime.turns", level="INFO") as logs:
                should_requeue = await daemon._complete_memory_maintenance_turn(
                    turn_id, asyncio.Event()
                )
                self.assertTrue(should_requeue)
                yielded = daemon.store._db.execute(
                    "SELECT state,stage,failure_reason FROM turns WHERE id=?",
                    (turn_id,),
                ).fetchone()
                self.assertEqual(yielded["state"], "running")
                self.assertEqual(
                    yielded["stage"], "memory_maintenance_queued"
                )
                self.assertIsNone(yielded["failure_reason"])
                self.assertFalse(
                    daemon.store.memory_maintenance_bootstrap_complete()
                )
                should_requeue = await daemon._complete_memory_maintenance_turn(
                    turn_id, asyncio.Event()
                )
                self.assertFalse(should_requeue)
            state = daemon.store._db.execute(
                "SELECT state FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            self.assertEqual(state["state"], "completed")
            journal = daemon.store.memory_maintenance_journal(turn_id)
            batch = next(
                item
                for item in journal
                if item["item_type"] == "memory_maintenance_batch"
            )
            self.assertEqual(batch["change_count"], 1)
            self.assertTrue(
                any(
                    item["item_type"] == "memory_maintenance_complete"
                    for item in journal
                )
            )
            self.assertTrue(daemon.store.memory_maintenance_bootstrap_complete())
            self.assertEqual(call_contexts[0]["stage"], "memory_maintenance")
            self.assertEqual(call_contexts[0]["turn_id"], turn_id)
            self.assertEqual(call_contexts[0]["round"], 1)
            self.assertTrue(call_contexts[0]["call_id"])
            self.assertNotIn("preview", call_contexts[0])
            self.assertTrue(
                any("memory_maintenance_applied" in line for line in logs.output)
            )
            applied_log = next(
                record
                for record in logs.records
                if getattr(record, "momoi_event", "")
                == "memory_maintenance_applied"
            )
            self.assertIn(
                "主人可以吃辣",
                applied_log.momoi_fields["changes"],
            )
            self.assertEqual(
                daemon.store._db.execute(
                    "SELECT content FROM memories WHERE id=?", (memory_id,)
                ).fetchone()["content"],
                "主人可以吃辣。",
            )
            daemon.store.close()

    async def test_apply_mode_commits_validated_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig("http://127.0.0.1", "test", "test", 1000, 0, 1, 0)),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            daemon.store.add_event(
                IncomingMessage(
                    "owner-2",
                    "2",
                    "以后我可以吃辣，之前那条改掉",
                    20,
                    20,
                )
            )
            cursor = daemon.store._db.execute(
                """INSERT INTO memories
                   (kind,key,content,activation,authority,source_event_id,
                    evidence_quote,importance,created_at,updated_at)
                   VALUES ('preference','food.spicy','主人不吃辣。','always',
                           'owner','owner-1','我不吃辣',0.5,10,10)"""
            )
            memory_id = int(cursor.lastrowid)
            daemon.store._db.commit()
            snapshot = daemon.store.maintenance_memory_inventory()[0]
            turn_id = "maintenance-apply"
            daemon.store.queue_memory_maintenance_turn(turn_id, "manual:test")

            class Provider:
                async def complete(
                    self,
                    _system: object,
                    _messages: object,
                    _tools: object,
                    **_kwargs: object,
                ) -> ProviderResponse:
                    assert _tools == [MEMORY_MAINTENANCE_FINISH_SPEC]
                    assert _kwargs["require_tool"] is True
                    payload = {
                        "version": 1,
                        "reviewed_ids": [],
                        "changes": [
                            {
                                "action": "replace",
                                "memory_id": memory_id,
                                "snapshot_fingerprint": (
                                    memory_snapshot_fingerprint(snapshot)
                                ),
                                "content": "主人可以吃辣。",
                                "activation": "always",
                                "expires_at": None,
                                "evidence": {
                                    "event_id": "owner-2",
                                    "quote": "我可以吃辣",
                                },
                                "reason": "主人更新了饮食偏好。",
                            }
                        ],
                        "regroup_requests": [],
                        "summary": "更新饮食偏好。",
                    }
                    call = ToolCall(
                        "finish-maintenance",
                        "memory_maintenance_finish",
                        payload,
                    )
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
                    )

            daemon.provider = Provider()
            should_requeue = await daemon._complete_memory_maintenance_turn(
                turn_id, asyncio.Event()
            )
            self.assertTrue(should_requeue)
            should_requeue = await daemon._complete_memory_maintenance_turn(
                turn_id, asyncio.Event()
            )
            self.assertFalse(should_requeue)
            updated = daemon.store._db.execute(
                """SELECT content,source_event_id,evidence_quote,updated_at
                   FROM memories WHERE id=?""",
                (memory_id,),
            ).fetchone()
            self.assertEqual(updated["content"], "主人可以吃辣。")
            self.assertEqual(updated["source_event_id"], "owner-2")
            self.assertEqual(updated["evidence_quote"], "我可以吃辣")
            self.assertEqual(updated["updated_at"], 20)
            journal = daemon.store.memory_maintenance_journal(turn_id)
            self.assertTrue(
                any(
                    item["item_type"] == "memory_maintenance_batch"
                    for item in journal
                )
            )
            self.assertTrue(daemon.store.memory_maintenance_bootstrap_complete())
            daemon.store.close()

    async def test_stop_after_batch_keeps_turn_queued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig(
                        "http://127.0.0.1", "test", "test", 1000, 0, 1, 0
                    )),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            turn_id = "maintenance-stop-after-batch"
            daemon.store.queue_memory_maintenance_turn(turn_id, "manual:test")
            stop = asyncio.Event()

            async def run_batch(
                _turn_id: str,
            ) -> tuple[bool, int, str | None]:
                stop.set()
                return False, 0, None

            daemon._run_memory_maintenance_batch = run_batch  # type: ignore[method-assign]
            should_requeue = await daemon._complete_memory_maintenance_turn(
                turn_id, stop
            )
            self.assertFalse(should_requeue)
            row = daemon.store._db.execute(
                "SELECT state,stage,failure_reason FROM turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            self.assertEqual(row["state"], "running")
            self.assertEqual(row["stage"], "memory_maintenance_queued")
            self.assertIsNone(row["failure_reason"])
            daemon.store.close()

    async def test_owner_stop_keeps_cancelled_batch_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            daemon = MomoiDaemon(
                AppConfig(
                    providers=provider_catalog(LLMConfig(
                        "http://127.0.0.1", "test", "test", 1000, 0, 1, 0
                    )),
                    channel=NapCatConfig(
                        "ws://127.0.0.1", "20000", 1, 60, 30, 30, 20
                    ),
                    system_prompt="test",
                    transcript_turns_min=4,
                    transcript_turns_max=4,
                    episode_raw_tail_turns=2,
                    memory_results=2,
                    database=Path(directory) / "momoi.sqlite3",
                    log_level="INFO",
                )
            )
            turn_id = "maintenance-owner-stop"
            daemon.store.queue_memory_maintenance_turn(turn_id, "manual:test")
            started = asyncio.Event()

            async def run_batch(
                _turn_id: str,
            ) -> tuple[bool, int, str | None]:
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            daemon._run_memory_maintenance_batch = run_batch  # type: ignore[method-assign]
            task = asyncio.create_task(
                daemon._complete_memory_maintenance_turn(
                    turn_id, asyncio.Event()
                )
            )
            await started.wait()
            daemon._stop_requested = True
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            row = daemon.store._db.execute(
                "SELECT state,stage,failure_reason FROM turns WHERE id=?",
                (turn_id,),
            ).fetchone()
            self.assertEqual(row["state"], "running")
            self.assertEqual(row["stage"], "memory_maintenance_queued")
            self.assertEqual(row["failure_reason"], "owner_stop")
            daemon.store.close()


if __name__ == "__main__":
    unittest.main()
