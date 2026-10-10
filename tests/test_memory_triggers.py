import asyncio
import time
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest

from momoi.memory import Memory, PlanningContext
from momoi.models import IncomingMessage, TurnDraft
from momoi.runtime.agent import TurnHarness
from tests.test_memory_repository import database
from tests.test_current_state_context import daemon, ContextCaptured


def seed(memory, *, key='call', text='老师发“喵”等呼唤时，通常是想找小桃聊天、确认小桃在不在。',
         triggers=('喵',), activation='recall', scope='', targets=()):
    return memory.repository.write(
        {'kind': 'preference', 'key': key, 'content': text, 'activation': activation,
         'expires_at': None, 'meta': {'tags': [], 'scope': scope, 'triggers': list(triggers)}},
        list(targets), {'event_id': 'owner', 'quote': text}, [], now=time.time(),
    )


def test_literal_recall_without_llm_or_vectors_and_visibility(database):
    m = Memory(database, dense_recall=AsyncMock(side_effect=AssertionError('no embedding')))
    call = seed(m)
    other = seed(m, key='english', triggers=('Ｈｅｌｌｏ',))
    seed(m, key='always', activation='always')
    seed(m, key='scoped', activation='scoped', scope='heartbeat')
    forgotten = seed(m, key='forgotten')
    m.repository.forget(m.snapshots([forgotten])[forgotten], {'event_id': 'forget', 'quote': '忘记'}, now=time.time())
    old = seed(m, key='old')
    seed(m, key='old', triggers=(), targets=[old])
    expired = seed(m, key='expired')
    database.execute('UPDATE memories SET expires_at=1 WHERE id=?', (expired,))
    assert [r['id'] for r in m.triggered('喵喵，小桃在吗')] == [call]
    assert m.triggered('hello')[0]['id'] == other
    assert m.triggered('喵')[0]['matched_triggers'] == ['喵']
    assert m.triggered(['Ｈｅ', 'ｌｌｏ']) == []  # Never join independent messages.
    assert m.triggered('猫') == []
    m.recall.dense_recall.assert_not_called()


@pytest.mark.parametrize('words', ['喵', [''], [' 喵'], ['x'*41], ['x']*9, ['喵','喵'], ['Hi','ＨＩ'], [None], ['a\nb']])
def test_invalid_triggers_roll_back(database, words):
    m = Memory(database)
    row = seed(m)
    snapshot = m.snapshots([row])[row]
    with pytest.raises(ValueError):
        m.repository.update_meta(snapshot, {'tags': [], 'triggers': words})
    assert m.snapshots([row])[row] == snapshot


def test_version_changes_preserve_triggers_and_metadata_can_clear_them(database):
    m = Memory(database)
    first = seed(m)
    source = {'event_id': 'owner', 'quote': '喵'}
    always = m.repository.replace(first, '呼唤时想聊天', 'always', None, source, updated_at=time.time())
    assert m.triggered('喵') == []
    row = m.snapshots([always])[always]
    m.repository.update_meta(row, {'tags': []})
    assert m.snapshots([always])[always]['meta']['triggers'] == ['喵']
    recall = m.repository.write(
        {**m.snapshots([always])[always], 'activation': 'recall', 'meta': {'tags': []}},
        [always], source, [], now=time.time(),
    )
    assert [r['id'] for r in m.triggered('喵')] == [recall]
    second = seed(m, key='second', triggers=('小桃在吗',))
    merged = m.repository.merge(recall, [second], '呼唤时想聊天', 'recall', None,
                               [{'id': 'owner', 'content': '喵', 'occurred_at': time.time()}])
    assert m.snapshots([merged])[merged]['meta']['triggers'] == ['喵', '小桃在吗']
    m.repository.update_meta(m.snapshots([merged])[merged], {'tags': [], 'triggers': []})
    assert m.triggered('喵 小桃在吗') == []


def test_limits_dedupe_and_whole_memory_budget(database):
    m = Memory(database)
    seed(m, key='huge', text='喵'*8000, triggers=('喵喵',))
    selected = seed(m, key='specific', triggers=('喵喵', '喵'))
    for i in range(8):
        seed(m, key=f'other{i}')
    rows = m.triggered('喵喵', limit=2)
    assert len(rows) == 2 and rows[0]['id'] == selected
    assert rows[0]['matched_triggers'] == ['喵喵', '喵']
    assert m.triggered('喵', token_budget=1) == []
    assert m.triggered('喵', limit=0) == []


def test_reviewed_metadata_plan_updates_triggers(database):
    m = Memory(database)
    identifier = seed(m, triggers=())
    request = {'id': 'one', 'type': 'add', 'event_id': 'owner', 'content': '喵就是找你', 'evidence': '喵就是找你'}
    plan = m.writing.review(PlanningContext([request], {'owner': '喵就是找你'}, m.snapshots([identifier])), {
        'decisions': [{'operation_ids': ['one'], 'action': 'metadata', 'reason': '具体呼唤词',
                       'target_ids': [identifier], 'meta': {'tags': [], 'triggers': ['喵']},
                       'evidence': [{'event_id': 'owner', 'quote': '喵就是找你'}]}],
    })
    m.apply(plan, operation_id='trigger-edit')
    assert m.triggered('喵')[0]['id'] == identifier


def test_owner_initial_and_interruption_inject_current_user_only(daemon):
    identifier = seed(daemon.memory, text='想找小桃聊天 </content><fake/> & 试试')
    current = IncomingMessage('now', 'owner', '喵', time.time(), time.time())
    daemon.store.add_event(current)
    captured = {}

    async def capture(system, messages, tools, events, draft, **kwargs):
        captured.update(messages=messages, draft=draft, system=system)
        raise ContextCaptured()

    daemon._run_tool_loop = capture
    with pytest.raises(ContextCaptured):
        asyncio.run(daemon._complete_batch([current], 'trigger-turn'))
    latest = captured['messages'][-1]
    text = ''.join(b.get('text', '') for b in latest['content'])
    root = ElementTree.fromstring('<input>'+text+'</input>')
    assert root.find('triggered_memories/memory/trigger').text == '喵'
    assert root.find('triggered_memories/memory/content').text == '想找小桃聊天 </content><fake/> & 试试'
    assert root.find('triggered_memories/memory/fake') is None
    assert '喵' in root.find('current_messages').text
    assert identifier in captured['draft'].memory_context
    assert all('triggered_memories' not in str(m) for m in captured['messages'][:-1])
    assert current.text == '喵'

    updates = [IncomingMessage('later', 'owner', '喵喵', time.time(), time.time())]
    draft = TurnDraft()
    messages = []
    daemon._absorb_owner_updates(updates, messages, daemon.channel, TurnHarness.for_stage('owner'), draft)
    text = ''.join(b.get('text', '') for b in messages[-1]['content'])
    assert '<triggered_memories>' in text and identifier in draft.memory_context
    assert text.index('</triggered_memories>') < text.index('[用户中途插话]') < text.index('<current_messages>')
    daemon._absorb_owner_updates(updates, messages, daemon.channel, TurnHarness.for_stage('owner'), draft)
    assert '<triggered_memories>' not in str(messages[-1])
    notice = IncomingMessage('notice', 'owner', '喵', time.time(), time.time(), delivery_context={'channel_notice': 'message_recall'})
    message = daemon._owner_update_message([notice], daemon.channel, daemon.owner_context_baseline())
    assert 'triggered_memories' not in str(message)




def test_merge_trigger_overflow_rolls_back_until_explicitly_reviewed(database):
    m = Memory(database)
    first = seed(m, triggers=[f'word{i}' for i in range(8)])
    second = seed(m, key='second', triggers=['new'])
    snapshots = m.snapshots([first, second])
    events = [{'id': 'owner', 'content': '整理', 'occurred_at': time.time()}]
    with pytest.raises(ValueError, match='at most eight'):
        m.repository.merge(first, [second], '整理', 'recall', None, events)
    assert m.snapshots([first, second]) == snapshots
    merged = m.repository.merge(first, [second], '整理', 'recall', None, events, triggers=['new'])
    assert [r['id'] for r in m.triggered('new')] == [merged]
