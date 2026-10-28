import tempfile
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from momoi.config.models import AppConfig
from momoi.runtime.context.service import ContextService
from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
from momoi.runtime.context.selection import RecallSelection
from momoi.storage import Store
from momoi.models import ToolCall
from momoi.runtime.workflows.episode.relations import EpisodeRelationWorkflow


def _summarize(store, identifier, title, summary, created_at):
    store.create_episode(title, episode_id=identifier)
    with store._db:
        store._db.execute(
            """UPDATE conversation_episodes SET narrative_summary=?,
               summarized_through_ordinal=1, created_at=? WHERE id=?""",
            (summary, created_at, identifier),
        )


def _append_dialogue(store, episode_id, ordinal, content):
    turn_id = f"{episode_id}-turn-{ordinal}"
    with store._db:
        store._db.execute(
            """INSERT INTO turns(id,kind,workflow_kind,source_ids_json,state,started_at,updated_at)
               VALUES (?,'owner','owner','[]','completed',1,1)""",
            (turn_id,),
        )
        store._db.execute(
            """INSERT INTO episode_turns(episode_id,turn_id,ordinal,relation)
               VALUES (?,?,?,'primary')""",
            (episode_id, turn_id, ordinal),
        )
        store._db.execute(
            """INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json)
               VALUES (?,'user',?,1,'[]')""",
            (turn_id, content),
        )


def test_new_episode_only_and_empty_review_is_durable():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            enabled_at = store._db.execute(
                "SELECT enabled_at FROM episode_relation_settings WHERE id=1"
            ).fetchone()[0]
            _summarize(store, "old", "项目启动", "团队确认项目启动", enabled_at - 10)
            _summarize(store, "new", "项目进展", "团队完成首个阶段", enabled_at + 10)
            candidate = store.claim_episode_relation_candidate()
            assert candidate["id"] == "new"
            store.finish_episode_relations("new", 1, [], {"old"})
            assert store.claim_episode_relation_candidate() is None
            assert store._db.execute("SELECT count(*) FROM episode_relations").fetchone()[0] == 0
            review = store._db.execute("SELECT * FROM episode_relation_reviews").fetchone()
            assert review["target_episode_id"] == "old"
            assert review["decision"] == "unrelated"
            assert review["source_summary_ordinal"] == 1
        finally:
            store.close()


def test_relation_recall_shape_includes_both_directions():
    from momoi.runtime.context import rendering

    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            _summarize(store, "old", "项目启动", "团队确认项目启动", 1)
            _summarize(store, "new", "项目进展", "团队完成首个阶段", 2)
            store._db.execute("UPDATE conversation_episodes SET summarized_through_ordinal=1 WHERE id='new'")
            with store._db:
                store._db.execute(
                    """INSERT INTO episode_relations
                       (source_episode_id,target_episode_id,relation,explanation,
                        evidence_json,source_summary_ordinal,created_at,updated_at)
                       VALUES ('new','old','follows_up','同一项目继续推进','{}',1,1,1)"""
                )
            selected = [{"episode_id": "new", "matched_keywords": []}]
            records = rendering.episode_recall_records(store, selected, 6000)
            assert records[0]["follows_up"] == [{"id": "old", "title": "项目启动"}]
            assert store.episode_relation_neighbors(["new"])["new"] == [{
                "direction": "outgoing", "type": "follows_up", "episode_id": "old",
                "title": "项目启动", "summary": "团队确认项目启动", "explanation": "同一项目继续推进",
            }]
            assert store.episode_relation_neighbors(["old"])["old"][0]["direction"] == "incoming"
            incoming = rendering.episode_recall_records(
                store, [{"episode_id": "old", "matched_keywords": []}], 6000
            )
            assert incoming[0]["followed_by"] == [{"id": "new", "title": "项目进展"}]
            assert "turns" not in records[0]
            assert records[0]["details_omitted"] is True
        finally:
            store.close()


def _relate(store, source, target, relation, explanation="关联", updated=1):
    with store._db:
        store._db.execute(
            """INSERT INTO episode_relations
               (source_episode_id,target_episode_id,relation,explanation,
                evidence_json,source_summary_ordinal,created_at,updated_at)
               VALUES (?,?,?,?,'{}',1,1,?)""",
            (source, target, relation, explanation, updated),
        )


def _recall(store, *ids, budget=6000):
    from momoi.runtime.context import rendering

    return rendering.episode_recall_records(
        store, [{"episode_id": value, "matched_keywords": []} for value in ids], budget
    )


def test_revised_topic_injects_latest_reviser_after_it():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            for index, episode_id in enumerate(("a", "b", "c", "other"), 1):
                _summarize(store, episode_id, f"{episode_id} 标题", "出行安排" * 200, index)
            _relate(store, "b", "a", "revises", "改为周六出发")
            _relate(store, "c", "b", "revises", "再改为周日出发")
            records = _recall(store, "a", "other")
            assert [record["id"] for record in records] == ["a", "c", "other"]
            assert records[0]["revised_by"] == [
                {"id": "b", "title": "b 标题", "explanation": "改为周六出发"}
            ]
            assert records[1]["injected"] == "revises"
            assert records[1]["revises"] == [{"id": "b", "title": "b 标题"}]
            assert len(records[0]["summary"]) < len(records[2]["summary"])
        finally:
            store.close()


def test_recalled_reviser_is_not_duplicated():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            _summarize(store, "a", "旧", "旧决定", 1)
            _summarize(store, "b", "新", "新决定", 2)
            _relate(store, "b", "a", "revises", "改主意")
            records = _recall(store, "b", "a")
            assert [record["id"] for record in records] == ["a", "b"]
            assert "injected" not in records[1]
            assert records[1]["revises"] == [{"id": "a"}]
        finally:
            store.close()


def test_follow_up_within_results_is_written_once_and_ordered():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            _summarize(store, "a", "起因", "开始", 1)
            _summarize(store, "b", "后续", "进展", 2)
            _relate(store, "b", "a", "follows_up")
            records = _recall(store, "b", "a")
            assert [record["id"] for record in records] == ["a", "b"]
            assert "followed_by" not in records[0]
            assert records[1]["follows_up"] == [{"id": "a"}]
        finally:
            store.close()


def test_unrecalled_follow_ups_are_capped_pointers():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            _summarize(store, "a", "起因", "开始", 1)
            for index in range(3):
                _summarize(store, f"f{index}", f"后续{index}", "进展", index + 2)
                _relate(store, f"f{index}", "a", "follows_up", updated=index)
            records = _recall(store, "a")
            assert [record["id"] for record in records] == ["a"]
            assert [item["id"] for item in records[0]["followed_by"]] == ["f2", "f1"]
        finally:
            store.close()


def test_tight_budget_drops_pointers_before_revisions():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            _summarize(store, "a", "起因", "开始" * 50, 1)
            _summarize(store, "b", "修订", "新决定", 2)
            _summarize(store, "f", "后续", "进展", 3)
            _summarize(store, "k", "背景", "背景", 4)
            _relate(store, "b", "a", "revises", "改主意")
            _relate(store, "f", "a", "follows_up")
            _relate(store, "k", "a", "context")
            records = _recall(store, "a", budget=60)
            assert records[0]["revised_by"][0]["id"] == "b"
            assert "context" not in records[0]
            assert "followed_by" not in records[0]
        finally:
            store.close()


def test_summaries_shrink_before_pointers_are_dropped():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            ids = [f"e{index}" for index in range(6)]
            for index, episode_id in enumerate(ids):
                _summarize(store, episode_id, f"话题{index}", "很长的经过" * 300, index + 1)
            for source, target in zip(ids[1:], ids):
                _relate(store, source, target, "follows_up")
            budget = 300
            records = _recall(store, *ids, budget=budget)
            assert all(record.get("follows_up") for record in records[1:])
            assert sum(rendering_tokens(record) for record in records) <= budget
        finally:
            store.close()


def rendering_tokens(record):
    from momoi.runtime.context import rendering

    return rendering._record_tokens(record)


def test_stale_revision_is_ignored():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            _summarize(store, "a", "旧", "旧决定", 1)
            _summarize(store, "b", "新", "新决定", 2)
            _relate(store, "b", "a", "revises")
            store._db.execute(
                "UPDATE conversation_episodes SET summarized_through_ordinal=2 WHERE id='b'"
            )
            records = _recall(store, "a")
            assert [record["id"] for record in records] == ["a"]
            assert "revised_by" not in records[0]
        finally:
            store.close()


def test_relation_priority_applies_before_neighbor_limit():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            for index, episode_id in enumerate(("root", "background", "follow", "revision", "revision_new"), 1):
                _summarize(store, episode_id, episode_id, "项目进展", index)
            with store._db:
                for target, relation, updated in (
                    ("background", "context", 100),
                    ("follow", "follows_up", 90),
                    ("revision", "revises", 1),
                    ("revision_new", "revises", 2),
                ):
                    store._db.execute(
                        """INSERT INTO episode_relations
                           (source_episode_id,target_episode_id,relation,explanation,
                            evidence_json,source_summary_ordinal,created_at,updated_at)
                           VALUES (?, 'root', ?, '项目关联', '{}', 1, 1, ?)""",
                        (target, relation, updated),
                    )
            neighbors = store.episode_relation_neighbors(["root"], per_episode=3)["root"]
            assert [item["episode_id"] for item in neighbors] == ["revision_new", "revision", "follow"]
            assert all(item["direction"] == "incoming" for item in neighbors)
        finally:
            store.close()


def test_relation_graph_follows_incoming_and_outgoing_links_to_depth_two():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            for index, episode_id in enumerate(("old", "middle", "new"), 1):
                _summarize(store, episode_id, episode_id, f"{episode_id} summary", index)
            with store._db:
                for source, target in (("middle", "old"), ("new", "middle")):
                    store._db.execute(
                        """INSERT INTO episode_relations
                           (source_episode_id,target_episode_id,relation,explanation,
                            evidence_json,source_summary_ordinal,created_at,updated_at)
                           VALUES (?,?,'follows_up','承接','{}',1,1,1)""",
                        (source, target),
                    )
            one = store.episode_relation_graph("old")
            assert [(node["id"], node["depth"]) for node in one["nodes"]] == [
                ("old", 0), ("middle", 1),
            ]
            assert one["edges"][0]["source_episode_id"] == "middle"
            two = store.episode_relation_graph("old", 2)
            assert {(node["id"], node["depth"]) for node in two["nodes"]} == {
                ("old", 0), ("middle", 1), ("new", 2),
            }
            assert len(two["edges"]) == 2
            assert store.episode_relation_graph("middle", 1)["depth"] == 1
            with pytest.raises(ValueError, match="depth"):
                store.episode_relation_graph("old", 3)
        finally:
            store.close()


def test_relation_validation_and_updated_summary_rechecks():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            enabled_at = store._db.execute(
                "SELECT enabled_at FROM episode_relation_settings WHERE id=1"
            ).fetchone()[0]
            _summarize(store, "old", "项目启动", "团队确认项目启动", enabled_at - 10)
            _summarize(store, "new", "项目进展", "团队完成首个阶段", enabled_at + 10)
            candidate = store.claim_episode_relation_candidate()
            decision = {
                "target_episode_id": "old", "relation": "follows_up",
                "explanation": "项目推进到首个阶段", "source_evidence": "完成首个阶段",
                "target_evidence": "确认项目启动",
            }
            with pytest.raises(ValueError):
                store.finish_episode_relations("new", 1, [decision], set())
            with pytest.raises(ValueError, match=r"relations\[0\]\.target_evidence.*old.*确认项目启动"):
                store.finish_episode_relations(
                    "new", 1, [decision], {"old"}, evidence_records={
                        "new": {"summary": "团队完成首个阶段"},
                        "old": {"summary": "团队[…truncated…]"},
                    },
                )
            store.finish_episode_relations("new", 1, [decision], {"old"})
            assert store._db.execute("SELECT relation FROM episode_relations").fetchone()[0] == "follows_up"
            assert store._db.execute("SELECT count(*) FROM episode_relation_reviews").fetchone()[0] == 0
            with store._db:
                store._db.execute(
                    "UPDATE conversation_episodes SET summarized_through_ordinal=2 WHERE id='new'"
                )
            candidate = store.claim_episode_relation_candidate()
            assert candidate["id"] == "new"
            store.finish_episode_relations("new", 2, [], {"old"})
            assert store._db.execute("SELECT count(*) FROM episode_relations").fetchone()[0] == 0
            assert store._db.execute("SELECT source_summary_ordinal FROM episode_relation_reviews").fetchone()[0] == 2
        finally:
            store.close()


def test_heartbeat_and_webhook_archives_are_not_summarized_or_linked():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            enabled_at = store._db.execute(
                "SELECT enabled_at FROM episode_relation_settings WHERE id=1"
            ).fetchone()[0]
            for kind in ("heartbeat", "webhook"):
                _summarize(store, kind, f"{kind}归档", "归档摘要", enabled_at + 1)
                with store._db:
                    store._db.execute(
                        "UPDATE conversation_episodes SET archive_kind=? WHERE id=?",
                        (kind, kind),
                    )
            _summarize(store, "owner", "用户话题", "用户话题摘要", enabled_at + 2)
            assert store.claim_episode_relation_candidate()["id"] == "owner"
            for kind in ("heartbeat", "webhook"):
                with pytest.raises(ValueError, match="older episode"):
                    store.finish_episode_relations("owner", 1, [{
                        "target_episode_id": kind, "relation": "context",
                        "explanation": "归档背景", "source_evidence": "用户话题摘要",
                        "target_evidence": "归档摘要",
                    }], {kind})
            assert store.claim_episode_annealing_candidate(1, 1000) is None
        finally:
            store.close()


def test_workflow_model_chooses_query_then_finishes():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "db")
        try:
            enabled_at = store._db.execute("SELECT enabled_at FROM episode_relation_settings").fetchone()[0]
            _summarize(store, "old", "项目启动", "团队确认项目启动", enabled_at - 10)
            _summarize(store, "new", "项目进展", "团队完成首个阶段", enabled_at + 10)
            _append_dialogue(store, "new", 1, "已归档对话")
            _append_dialogue(store, "new", 2, "尚未进入摘要的对话")

            class Runner(EpisodeRelationWorkflow, ContextService):
                async def _run_agent_workflow(self, system, messages, tools, turn_id, workflow):
                    assert "团队完成首个阶段" in messages[0]["content"]
                    assert "已归档对话" in messages[0]["content"]
                    assert "尚未进入摘要的对话" not in messages[0]["content"]
                    assert not semantic.prepare.called
                    assert tools[0] == RECALL_TOOL_SPEC
                    args = {"semantic": ["项目启动", "项目最初的决定"], "keyword": ["项目"]}
                    invalid = await workflow.execute_tool(ToolCall("bad", "recall", {"query": "项目启动"}))
                    assert not invalid["ok"]
                    from momoi.tools.validation import validate_tool_arguments
                    _, error = validate_tool_arguments("recall", args, tools[0]["input_schema"])
                    assert error is None
                    args["semantic"] = [""]
                    _, error = validate_tool_arguments("recall", args, tools[0]["input_schema"])
                    assert error is not None
                    args["semantic"] = ["项目启动", "项目最初的决定"]
                    result = await workflow.execute_tool(ToolCall("recall", "recall", args))
                    assert result["ok"], result
                    assert "memory" in result and "reflection" in result
                    assert result["episodes"][0]["id"] == "old"
                    from momoi.runtime.context.rendering import episode_recall_records
                    assert result["episodes"] == episode_recall_records(
                        store, [{"episode_id": "old"}], 6000,
                    )
                    result = await workflow.execute_tool(ToolCall("finish", "episode_relation_finish", {"relations": [{
                        "target_episode_id": "old", "relation": "follows_up",
                        "explanation": "项目推进到首个阶段", "source_evidence": "已归档对话",
                        "target_evidence": "确认项目启动",
                    }]}))
                    assert result["ok"] and workflow.is_complete()
                    return workflow.completion_result()

            semantic = SimpleNamespace(prepare=AsyncMock(return_value=None))
            runner = Runner()
            runner.config = AppConfig(
                providers=None, channel=None, system_prompt="", transcript_turns_min=1,
                transcript_turns_max=32, episode_unsummarized_tail_turns=2,
                memory_results=8, database=Path(directory) / "db", log_level="INFO",
            )
            runner._select_recall_topics = AsyncMock(return_value=RecallSelection(
                [dict(store.episode("old"), matched_queries=[{"unit_ids": ["u1"]}],
                      relevance_confidence=1.0)], [], [],
            ))
            store.begin_turn("test", "episode_relation", [])
            runner.store = store
            runner.semantic_recall = semantic
            asyncio.run(runner._build_episode_relations(store.episode("new"), 1, "test"))
            assert len(semantic.prepare.call_args.args[0]) == 2
            assert runner._select_recall_topics.called
            assert runner._select_recall_topics.call_args.kwargs["model_selection"] is False
            assert runner._select_recall_topics.call_args.kwargs["exclude_episode_ids"] == ("new",)
            assert "episode_actions" not in store.context_plan("test")["plan"]
            assert store.context_plan("test")["plan"]["intent_units"][0]["intent"] == "项目启动"
            assert store._db.execute("SELECT count(*) FROM episode_relations").fetchone()[0] == 1
        finally:
            store.close()
