import json
import time
from types import SimpleNamespace

import pytest

from momoi.storage import Store
from momoi.runtime.agent.context_window import ContextWindow
from momoi.runtime.turn_support import context_data_message


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "memory.sqlite3")
    yield value
    value.close()


def add(store, text, activation="always", key="test", scope=""):
    with store._db:
        return store._db.execute(
            """INSERT INTO memories(kind,key,content,activation,authority,
               source_event_id,evidence_quote,created_at,updated_at,scope_key)
               VALUES('preference',?,?,?,'owner','test','test',?,?,?)""",
            (key, text, activation, time.time(), time.time(), scope),
        ).lastrowid


def test_scoped_memory_only_appears_in_matching_workflow(store):
    goal_id = "a" * 32
    add(store, "只在喝水任务显示", activation="scoped", key="water", scope=f"goal:{goal_id}")
    add(store, "只在心跳显示", activation="scoped", key="contact", scope="heartbeat")
    add(store, "只在 webhook 显示", activation="scoped", key="arrival", scope="webhook")
    assert "喝水" in store.scoped_memory_context(f"goal:{goal_id}")
    assert "心跳" not in store.scoped_memory_context(f"goal:{goal_id}")
    assert "心跳" in store.scoped_memory_context("heartbeat")
    assert "webhook" in store.scoped_memory_context("webhook")
    assert not store.always_memory_context()
    assert not store.memories.search_literal("喝水|心跳|webhook", 10)
    assert store.memories.search_literal("喝水", 10, include_scoped=True)
    state = store.transcript_memory_context(["turn"])
    assert not state["observed"]


def test_dashboard_replace_delete_and_restart_preserve_snapshot(store):
    identifier = add(store, "旧偏好")
    first = store.transcript_memory_context(["a"])
    updated_id = store.update_memory_content(identifier, "新偏好")["id"]
    changed = store.transcript_memory_context(["a", "b"])
    assert changed["snapshot"] == first["snapshot"]
    assert '<delete' in changed["events"][0]["content"]
    assert '<add' in changed["events"][0]["content"]
    assert "新偏好" in changed["events"][0]["content"]
    assert store.memories.repository.active("preference", "test")["content"] == "新偏好"
    store.forget_memory_by_id(updated_id, "撤销")
    deleted = store.transcript_memory_context(["a", "b"])
    assert deleted["snapshot"] == first["snapshot"]
    assert '<delete' in deleted["events"][1]["content"]
    assert store.memories.repository.active("preference", "test") is None
    # Read durable state through another connection, as after a restart.
    other = Store(store._db.execute("PRAGMA database_list").fetchone()[2])
    assert other.transcript_memory_context(["a", "b"]) == deleted
    folded = other.transcript_memory_context(["b"], compact=True)
    assert not folded["snapshot"]
    assert not folded["events"]
    assert not folded["snapshot_overrides"]
    assert not folded["overrides"]
    assert '<delete' in folded["folded_overrides"][str(identifier)]
    other.close()


def test_only_always_memory_changes_enter_transcript(store):
    first = store.transcript_memory_context(["a"])
    identifier = add(store, "仅检索记忆", activation="recall")
    added = store.transcript_memory_context(["a"])
    assert added["snapshot"] == first["snapshot"]
    assert not added["events"]
    assert not added["observed"]
    with store._db:
        store._db.execute("UPDATE memories SET activation='always' WHERE id=?", (identifier,))
    promoted = store.transcript_memory_context(["a"])
    assert '<add' in promoted["events"][-1]["content"]
    with store._db:
        store._db.execute("UPDATE memories SET expires_at=1 WHERE id=?", (identifier,))
    expired = store.transcript_memory_context(["a"])
    assert '<delete' in expired["events"][-1]["content"]


def test_old_recall_deltas_are_removed_from_replay_and_overrides(store):
    identifier = add(store, "旧召回", activation="recall", key="old.recall")
    stale = store.transcript_memory_context(["a"])
    stale["snapshot"][str(identifier)] = dict(store.memories.repository.inventory()[0])
    stale["observed"][str(identifier)] = stale["snapshot"][str(identifier)]
    stale["snapshot_overrides"][str(identifier)] = f'<delete id="{identifier}">旧召回</delete>'
    stale["overrides"][str(identifier)] = stale["snapshot_overrides"][str(identifier)]
    stale["events"] = [{"anchor": "a", "revision": 1,
                        "content": f'<memory_changes><delete id="{identifier}">旧召回</delete></memory_changes>'}]
    stale["revision"] = 1
    with store._db:
        store._db.execute("UPDATE transcript_memory_state SET data_json=? WHERE id=1",
                          (json.dumps(stale, ensure_ascii=False),))
    clean = store.transcript_memory_context(["a"])
    assert not clean["snapshot"] and not clean["observed"]
    assert not clean["events"] and not clean["overrides"]
    assert not clean["snapshot_overrides"]


def test_running_turn_appends_changes_then_compaction_folds_them(store):
    identifier = add(store, "旧偏好")
    state = store.transcript_memory_context(["a"])
    prefix = context_data_message(("long_term_memories", "旧偏好"), required=True)
    prefix["_memory_snapshot"] = {
        "revision": state["revision"], "turn_ids": ["a"],
        "current": "旧偏好", "goals": "", "overrides": "",
    }
    messages = [prefix, {"role": "user", "content": "历史" * 1000, "_history_turn_ids": ["a"]},
                {"role": "user", "content": "当前"}]
    original = json.dumps(prefix["content"])
    window = ContextWindow(SimpleNamespace(max_input_tokens=100000, context_compaction_ratio=1), store, None)
    updated_id = store.update_memory_content(identifier, "新偏好")["id"]
    count = window.fit([], messages, [], 2)
    assert count == 2
    assert json.dumps(prefix["content"]) == original
    assert messages[-1]["_memory_change"]
    assert "新偏好" in messages[-1]["content"]
    length = len(messages)
    window.fit([], messages, [], 2)
    assert len(messages) == length
    window.config.max_input_tokens = 800
    count = window.fit([], messages, [], 2)
    assert count == 1
    assert "新偏好" in json.dumps(prefix["content"], ensure_ascii=False)
    assert not any(m.get("_memory_change") for m in messages)
    persisted = store.transcript_memory_context(["a"])
    assert not persisted["events"]
    assert persisted["snapshot"][str(updated_id)]["content"] == "新偏好"


def test_fold_does_not_consume_newer_revision(store):
    identifier = add(store, "一")
    state = store.transcript_memory_context(["a"])
    store.update_memory_content(identifier, "二")
    updated = store.transcript_memory_context(["a"])
    store.fold_transcript_memory(state["revision"])
    assert store.transcript_memory_context(["a"]) == updated


def test_shared_workflows_replay_identical_prefix_and_changes(tmp_path):
    from momoi.models import IncomingMessage, AgentReply
    from momoi.config.models import AppConfig
    from momoi.integrations.models import LLMConfig
    from momoi.channel.napcat import NapCatConfig
    from momoi.runtime import MomoiDaemon
    from tests.support import provider_catalog

    daemon = MomoiDaemon(AppConfig(
        providers=provider_catalog(LLMConfig("http://localhost", "test", "model", 100, 0, 1, 0)),
        channel=NapCatConfig("ws://localhost", "123", 1, 60, 30, 30, 20),
        system_prompt="test", transcript_turns_min=8, transcript_turns_max=16,
        episode_unsummarized_tail_turns=2, memory_results=2,
        database=tmp_path / "shared.sqlite3", log_level="INFO",
    ))
    store = daemon.store
    identifier = add(store, "旧记忆")
    event = IncomingMessage("past", "1", "过去", 1, 1)
    store.add_event(event)
    store.commit_turn([event], event.text, AgentReply(["回答"]), turn_id="past")
    store.append_turn_journal("past", "assistant_exchange", {
        "content": "内部判断", "results": [],
    }, trust="runtime")
    stages = ("owner", "heartbeat", "goal", "webhook",
              "plan_step", "current_state_maintenance")
    for stage in stages:
        store.begin_turn(stage, stage, [stage])
    with store._db:
        store._db.execute("UPDATE turns SET started_at=? WHERE id != 'past'", (time.time(),))
    baseline = daemon.shared_turn_context("owner")["messages"]
    store.update_memory_content(identifier, "新记忆")
    contexts = [daemon.shared_turn_context(stage)["messages"] for stage in stages]
    assert len({json.dumps(c, sort_keys=True) for c in contexts}) == 1
    assert contexts[0][0]["content"] == baseline[0]["content"]
    assert contexts[0][:-1][1:] == baseline[1:]
    assert contexts[0][-1]["_memory_change"]
    assert "新记忆" in contexts[0][-1]["content"]
    store.transcript_window_turn_limit(8, 16, force_compact=True)
    compacted = daemon.shared_turn_context("owner")["messages"]
    assert "新记忆" in json.dumps(compacted[0]["content"], ensure_ascii=False)
    assert not any(m.get("_memory_change") for m in compacted)
    store.close()


def test_running_request_sees_changes_folded_by_another_executor(store):
    identifier = add(store, "旧偏好")
    state = store.transcript_memory_context(["a"])
    prefix = context_data_message(("long_term_memories", "旧偏好"), required=True)
    prefix["_memory_snapshot"] = {
        "revision": state["revision"], "turn_ids": ["a"],
        "current": "旧偏好", "goals": "", "overrides": "",
    }
    store.forget_memory_by_id(identifier, "撤销")
    folded = store.transcript_memory_context(["b"], compact=True)
    assert not folded["events"]
    messages = [prefix, {"role": "user", "content": "当前"}]
    window = ContextWindow(SimpleNamespace(max_input_tokens=100000, context_compaction_ratio=1), store, None)
    window.fit([], messages, [], 1)
    assert '<delete' in messages[-1]["content"]
    assert messages[-1]["_memory_change"] == folded["revision"]
    assert store.transcript_memory_context(["b"])["boundary"] == "b"


def test_existing_window_adopts_current_history_format_immediately(tmp_path):
    import json
    from momoi.storage import Store
    store = Store(tmp_path / 'format.db')
    state = store.transcript_memory_context(['old'])
    del state['history_format']
    store._db.execute('UPDATE transcript_memory_state SET data_json=? WHERE id=1', (json.dumps(state),))
    store._db.commit()
    assert store.transcript_memory_context(['old'])['history_format'] == 4
    assert store.transcript_memory_context(['old'], compact=True)['history_format'] == 4
    assert store.transcript_memory_context(['old'])['history_format'] == 4
    store.close()


def test_episode_prefix_stays_frozen_until_compaction_and_survives_reopen(store, tmp_path):
    store.transcript_memory_context(["a", "b"])
    assert store.transcript_episode_snapshot("first summary") == "first summary"
    assert store.transcript_episode_snapshot("updated summary") == "first summary"
    reopened = Store(tmp_path / "memory.sqlite3")
    try:
        assert reopened.transcript_episode_snapshot("updated summary") == "first summary"
    finally:
        reopened.close()
    store.transcript_memory_context(["b"], compact=True)
    assert store.transcript_episode_snapshot("compacted summary") == "compacted summary"


def test_request_compaction_does_not_restore_dropped_turns_in_next_context(store):
    store.transcript_memory_context(["a", "b"])
    state = store.transcript_memory_context(["a", "b"])
    prefix = context_data_message(("long_term_memories", ""), required=True)
    prefix["_memory_snapshot"] = {
        "revision": state["revision"], "turn_ids": ["a", "b"],
        "current": "", "overrides": "", "goals": "",
    }
    messages = [prefix,
                {"role": "user", "content": "old " * 1000, "_history_turn_ids": ["a"]},
                {"role": "user", "content": "recent", "_history_turn_ids": ["b"]},
                {"role": "user", "content": "current"}]
    window = ContextWindow(SimpleNamespace(max_input_tokens=800, context_compaction_ratio=1), store, None)
    window.fit([], messages, [], 3)
    rows = [{"turn_id": "a"}, {"turn_id": "b"}, {"turn_id": "c"}]
    assert store.retained_transcript_rows(rows) == rows[1:]
    # A new executor observes the reduced boundary without resetting the snapshot.
    store.transcript_episode_snapshot("frozen")
    store.transcript_memory_context(["b", "c"])
    assert store.transcript_episode_snapshot("new summaries") == "frozen"


def test_folded_deletes_do_not_accumulate_in_prefix_and_clean_recall(store):
    from momoi.runtime.transcript.recall import remove_folded_memory_evidence
    import json
    identifier = add(store, 'old fact')
    state = store.transcript_memory_context(['a'])
    store.forget_memory_by_id(identifier, 'delete')
    changed = store.transcript_memory_context(['a'])
    assert changed['snapshot'] == state['snapshot']
    assert '<delete' in changed['events'][0]['content']
    store.fold_transcript_memory(changed['revision'])
    folded = store.transcript_memory_context(['a'])
    assert not folded['events'] and not folded['snapshot_overrides'] and not folded['overrides']
    for memory in ([{'id': identifier, 'content': 'old fact'}, {'id': 999, 'content': 'valid'}],
                   f'<memory id="{identifier}">old fact</memory><memory id="999">valid</memory>'):
        messages = [{'content': [{'type': 'tool_use', 'id': 'r', 'name': 'recall'}]},
                    {'content': [{'type': 'tool_result', 'tool_use_id': 'r',
                                  'content': json.dumps({'ok': True, 'memory': memory, 'episodes': ['keep']})}]}]
        remove_folded_memory_evidence(messages, folded['folded_overrides'])
        assert 'old fact' not in messages[-1]['content'][0]['content']
        assert 'valid' in messages[-1]['content'][0]['content']
        assert 'keep' in messages[-1]['content'][0]['content']
