"""Non-LLM events and unfinished episode summaries must not erase context."""
import json

from momoi.channel.napcat import NapCatConfig
from momoi.config.models import AppConfig
from momoi.integrations.models import LLMConfig
from momoi.models import AgentReply, IncomingMessage
from momoi.runtime import MomoiDaemon
from momoi.runtime.context.presentation import recent_episode_lines
from momoi.storage import Store
from tests.support import provider_catalog


def add_turn(store, turn_id, text, at, *, native=True, event=False):
    message = IncomingMessage(turn_id, '1', text, at, at)
    store.add_event(message)
    store.commit_turn([message], text, AgentReply([] if event else ['回答']), turn_id=turn_id)
    with store._db:
        store._db.execute('UPDATE turns SET updated_at=? WHERE id=?', (at, turn_id))
        if event:
            store._db.execute("UPDATE messages SET role='event' WHERE turn_id=?", (turn_id,))
    if native:
        store.append_turn_journal(turn_id, 'assistant_exchange', {
            'content': [{'type': 'tool_use', 'id': 'reply-' + turn_id,
                         'name': 'reply', 'input': {'intent': '回答'}}],
            'results': [{'type': 'tool_result', 'tool_use_id': 'reply-' + turn_id,
                         'content': '{"ok":true,"bubbles":["回答"]}'}],
        }, trust='runtime')


def test_poke_without_exchange_preserves_earlier_native_dialogue(tmp_path):
    daemon = MomoiDaemon(AppConfig(
        providers=provider_catalog(LLMConfig('http://localhost', 'test', 'model', 100, 0, 1, 0)),
        channel=NapCatConfig('ws://localhost', '123', 1, 60, 30, 30, 20),
        system_prompt='test', transcript_turns_min=8, transcript_turns_max=8,
        episode_unsummarized_tail_turns=2, memory_results=2,
        database=tmp_path / 'store.sqlite3', log_level='INFO',
    ))
    try:
        add_turn(daemon.store, 'earlier', '上午没有进食', 1)
        add_turn(daemon.store, 'poke', '【QQ 戳一戳】用户戳了机器人一下', 2, native=False, event=True)
        add_turn(daemon.store, 'later', '现在准备午餐', 3)
        daemon.store.begin_turn('active', 'owner', ['active'])
        shared = daemon.shared_turn_context('active')
        assert {r['turn_id'] for r in shared['rows']} == {'earlier', 'poke', 'later'}
        text = json.dumps(shared['history'], ensure_ascii=False)
        assert '上午没有进食' in text and 'QQ 戳一戳' in text and '现在准备午餐' in text
        assert text.index('上午没有进食') < text.index('QQ 戳一戳') < text.index('现在准备午餐')
    finally:
        daemon.store.close()


def test_episode_index_keeps_pending_and_split_topics_without_duplicate_summaries(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    try:
        for tid, at in [('old', 1), ('pending', 2), ('split-old', 3), ('split-new', 4), ('retained', 5), ('runtime', 0)]:
            add_turn(store, tid, '示例消息', at)
        for eid, tids in [('ready', ['old']), ('pending-topic', ['pending']),
                          ('split-topic', ['split-old', 'split-new']),
                          ('retained-topic', ['retained']), ('runtime-topic', ['runtime'])]:
            store.create_episode('示例话题 ' + eid, episode_id=eid)
            for tid in tids:
                store.link_turn_to_episode(eid, tid)
        with store._db:
            store._db.execute("UPDATE conversation_episodes SET narrative_summary='完整摘要' WHERE id IN ('ready','split-topic','retained-topic')")
            store._db.execute("UPDATE conversation_episodes SET archive_kind='webhook' WHERE id='runtime-topic'")
        entries = store.compacted_episode_directory(['split-new', 'retained'], 10, before_timestamp=10)
        by_id = {e['id']: e for e in entries}
        assert set(by_id) == {'ready', 'pending-topic', 'split-topic'}
        assert by_id['ready']['narrative_summary'] == '完整摘要'
        assert by_id['pending-topic']['summary_state'] == 'pending'
        assert by_id['split-topic']['summary_state'] == 'partial'
        assert by_id['split-topic']['narrative_summary'] == ''
        assert by_id['split-topic']['last_activity_timestamp'] == store.context_timestamp(3)
        rendered = recent_episode_lines(entries, {})
        assert 'summary_state="pending"' in rendered and 'summary_state="partial"' in rendered
        assert rendered.count('完整摘要') == 1 and 'episode_read' in rendered
        # An index frozen for the cache stays immutable as summaries complete.
        store.transcript_memory_context(['split-new', 'retained'])
        store.transcript_episode_snapshot(rendered)
        with store._db:
            store._db.execute("UPDATE conversation_episodes SET narrative_summary='新摘要' WHERE id='pending-topic'")
        assert store.transcript_episode_snapshot('updated') == rendered
    finally:
        store.close()
