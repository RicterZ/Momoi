import json

from momoi.storage import Store
from momoi.tools.memory import MemoryTools
from momoi.tools.episode_timeline import project_episode
from tests.test_builtin_presentations import normalized
from tests.test_episode_execution import setup


def test_execution_timeline_interleaves_speech_with_real_tool_times(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        setup(store)
        for timestamp, role, text in [(1, 'user', 'start'), (15, 'assistant', 'intermediate reply'),
                                       (30, 'assistant', 'done')]:
            with store._db:
                store._db.execute(
                    "INSERT INTO messages(turn_id,role,content,created_at,delivery_state,source_event_ids_json) "
                    "VALUES ('t',?,?,?,'delivered','[]')", (role, text, timestamp))
        for identifier, name, start, finish in [('a', 'read_file', 10, 12),
                                                ('b', 'write_file', 20, 22),
                                                ('c', 'end_turn', 24, 25)]:
            store.append_turn_journal('t', 'tool_call', {'tool_call_id': identifier, 'name': name,
                'arguments': {'path': '/example'}, 'arguments_complete': True}, created_at=start)
            result = {'ok': True, 'content': identifier, 'result_ref': 'tr_' + identifier * 32}
            store.append_turn_journal('t', 'tool_result', {'tool_call_id': identifier, 'name': name,
                'result': result}, created_at=finish)
            store.append_turn_journal('t', 'assistant_exchange', {
                'content': [{'type': 'text', 'text': 'internal note'},
                            {'type': 'tool_use', 'id': identifier, 'name': name,
                             'input': {'path': '/example'}}],
                'results': [{'type': 'tool_result', 'tool_use_id': identifier, 'content': json.dumps(result)}]},
                created_at=finish + .5)
        raw = MemoryTools(store)._episode_read({'episode_id': 'e', 'execution_cursor': 0})
        shown, snapshots = normalized(tmp_path, 'episode_read', raw)
        turn = shown['turns'][0]
        text = turn['timeline']
        assert text.index('USER: start') < text.index('read_file:')
        assert '"content":"a"' in text, text
        assert text.index('"content":"a"') < text.index('ASSISTANT: intermediate reply')
        assert text.index('ASSISTANT: intermediate reply') < text.index('write_file:')
        assert text.index('"content":"b"') < text.index('ASSISTANT: done')
        assert 'end_turn' not in text and text.count('TOOL_CALL') == 2
        assert 'ASSISTANT_NOTE' in text
        assert 'messages' not in turn and 'execution' not in turn
        assert turn['detail_reads'][0]['result_ref'] == 'tr_' + 'a' * 32
        assert snapshots.historical_payload(shown['result_ref'])['turns'][0]['execution']
        assert project_episode(shown) == shown
    finally:
        store.close()


def test_message_prefix_continuation_is_exact_and_preserves_quote_targets(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        setup(store)
        original = 'long text\n' * 1500
        with store._db:
            identifier = store._db.execute(
                "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) "
                "VALUES ('t','user',?,1,'[]')", (original,)).lastrowid
        tools = MemoryTools(store)
        raw = tools._episode_read({'episode_id': 'e'})
        shown = project_episode(raw)
        page = shown['episode']
        assert 'USER:' in page['transcript'] and 'messages' not in page
        ref = page['message_refs'][0]
        assert ref['id'] == identifier and ref['next_content_offset'] == 6000
        chunks = [original[:6000]]
        offset = ref['next_content_offset']
        while offset is not None:
            raw = tools._episode_read({'episode_id': 'e', 'message_id': identifier,
                                       'content_offset': offset})
            expected = raw['message']['content'][:6000]
            shown = project_episode(raw)['message']
            assert expected in shown['transcript']
            chunks.append(expected)
            offset = shown.get('next_content_offset')
        assert ''.join(chunks) == original
        sample = project_episode({'ok': True, 'message': {'id': 1, 'role': 'user', 'content': 'hello',
            'quote_targets': [{'channel': 'napcat', 'message_id': '42', 'text': 'hello'}]}})
        assert sample['message']['quote_targets'][0]['message_id'] == '42'
    finally:
        store.close()


def test_execution_order_fallback_is_explicit_and_control_tools_are_omitted():
    raw = {'ok': True, 'turns': [{'id': 't', 'execution': [
        {'sequence': 3, 'assistant_text': 'a note', 'tools': [
            {'name': 'exec', 'arguments': {'command': 'example'}, 'result': {'ok': False, 'error': 'failed'},
             'arguments_truncated': True, 'arguments_read': {'episode_id': 'e', 'turn_id': 't', 'tool_call_id': 'x'}},
            {'name': 'end_turn', 'arguments': {}, 'result': {'ok': True}}]}]}]}
    turn = project_episode(raw)['turns'][0]
    assert 'exchange #3, recorded' in turn['timeline']
    assert 'TOOL_CALL #3.1' in turn['timeline'] and 'TOOL_RESULT #3.1' in turn['timeline']
    assert '"error":"failed"' in turn['timeline']
    assert 'end_turn' not in turn['timeline']
    assert turn['detail_reads'][0]['arguments_read']['tool_call_id'] == 'x'


def test_secondary_text_fitting_updates_each_message_cursor_without_gaps():
    from momoi.tools.presentation import fit_result
    messages = [{'id': i, 'ordinal': i, 'turn_id': f't{i}', 'role': 'user',
                 'timestamp': 'time', 'content': str(i) * 6000,
                 'content_offset': 100, 'next_content_offset': 6100} for i in range(1, 4)]
    observation = project_episode({'ok': True, 'result_ref': 'tr_' + 'a' * 32,
                                   'episode': {'id': 'e', 'messages': messages}})
    fitted = fit_result(observation, 1500)
    assert len(json.dumps(fitted, ensure_ascii=False)) <= 1500
    page = fitted['episode']
    for ref in page['message_refs']:
        start, end = ref['text_range']
        visible = page['transcript'][start:end]
        assert visible == str(ref['id']) * len(visible)
        assert ref['next_content_offset'] == ref['content_offset'] + len(visible)
    assert fit_result(fitted, 1500) == fitted
    assert project_episode(fitted) == fitted


def test_parallel_tools_keep_result_pairing_when_completion_order_differs():
    raw = {'ok': True, 'turns': [{'id': 't', 'execution': [{'sequence': 5, 'recorded_at': 30,
        'tools': [
            {'name': 'slow', 'arguments': {}, 'result': {'ok': True, 'content': 'slow result'},
             'timing': {'called_at': 10, 'finished_at': 20, 'call_sequence': 1, 'result_sequence': 4}},
            {'name': 'fast', 'arguments': {}, 'result': {'ok': True, 'content': 'fast result'},
             'timing': {'called_at': 11, 'finished_at': 12, 'call_sequence': 2, 'result_sequence': 3}},
        ]}]}]}
    text = project_episode(raw)['turns'][0]['timeline']
    assert text.index('TOOL_CALL #5.1') < text.index('TOOL_CALL #5.2')
    assert text.index('TOOL_RESULT #5.2') < text.index('TOOL_RESULT #5.1')
    assert 'TOOL_RESULT #5.2: {"ok":true,"content":"fast result"}' in text


def test_nondelivered_assistant_text_is_not_presented_as_delivered_speech():
    page = project_episode({'ok': True, 'episode': {'id': 'e', 'messages': [
        {'id': 1, 'ordinal': 1, 'role': 'assistant', 'content': 'pending speech',
         'delivery_state': 'pending', 'timestamp': 'time'}]}})['episode']
    assert 'ASSISTANT [pending]: pending speech' in page['transcript']


def test_execution_refitting_never_splits_pairs_or_advances_hidden_turns():
    from momoi.tools.presentation import fit_result
    turns = [{'id': f't{i}', 'ordinal': i,
              'timeline': 'TOOL_CALL #1: {}\nTOOL_RESULT #1: ' + 'x' * 500}
             for i in range(1, 5)]
    observation = {'ok': True, 'episode_id': 'e', 'result_ref': 'tr_' + 'a' * 32,
                   'turns': turns, 'next_execution_cursor': 4}
    fitted = fit_result(observation, 1500)
    assert 0 < len(fitted['turns']) < 4
    assert fitted['next_execution_cursor'] == fitted['turns'][-1]['ordinal']
    assert fitted['turns'] == turns[:len(fitted['turns'])]
    assert len(json.dumps(fitted, ensure_ascii=False)) <= 1500
    tiny = fit_result(observation, 500)
    assert 'turns' not in tiny and 'next_execution_cursor' not in tiny
    assert tiny['result_ref'] == observation['result_ref']
    assert fit_result(fitted, 1500) == fitted


def test_default_episode_read_does_not_fetch_execution(tmp_path, monkeypatch):
    import momoi.storage.episode.episode_queries as queries
    store = Store(tmp_path / 'db')
    try:
        setup(store)
        def forbidden(*args, **kwargs):
            raise AssertionError('default dialogue read fetched execution')
        monkeypatch.setattr(queries, 'execution_turns', forbidden)
        result = MemoryTools(store)._episode_read({'episode_id': 'e'})
        assert result['ok'] and 'turns' not in result['episode']
        assert result['episode']['next_execution_cursor'] == 0
    finally:
        store.close()


def test_execution_budget_pages_all_exchanges_without_splitting_pairs():
    from momoi.tools.presentation import fit_result
    entries = [{'sequence': i, 'tools': [{'name': 'exec', 'arguments': {'i': i},
        'result': {'ok': True, 'content': str(i) * 300},
        'timing': {'called_at': i, 'finished_at': 100 - i}}]} for i in range(1, 13)]
    seen, cursor = [], 0
    while cursor < 12:
        observation = project_episode({'ok': True, 'episode_id': 'e',
            'result_ref': 'tr_' + 'a' * 32, 'turns': [{'id': 't', 'ordinal': 1,
            'execution': [entry for entry in entries if entry['sequence'] > cursor]}]})
        fitted = fit_result(observation, 4000)
        assert len(json.dumps(fitted, ensure_ascii=False)) <= 4000
        turn = fitted['turns'][0]
        units = turn['execution_units']
        for unit in units:
            seq = unit['sequence']
            assert f'TOOL_CALL #{seq}.1 ' in turn['timeline']
            assert f'TOOL_RESULT #{seq}.1:' in turn['timeline']
            seen.append(seq)
        cursor = turn.get('next_after_sequence', 12)
        assert cursor == units[-1]['sequence']
        assert fit_result(fitted, 4000) == fitted
    assert seen == list(range(1, 13))


def test_default_dialogue_pages_three_turns_at_query_boundary(tmp_path, monkeypatch):
    store = Store(tmp_path / 'db')
    try:
        for i in range(1, 8):
            setup(store, turn=f't{i}', ordinal=i)
            with store._db:
                store._db.execute(
                    "INSERT INTO messages(turn_id,role,content,created_at,source_event_ids_json) "
                    "VALUES (?,'user',?,?,'[]')", (f't{i}', f'message {i}', i))
        original = store.episode_messages
        boundaries = []
        def capture(*args, **kwargs):
            boundaries.append(kwargs['after_ordinal'])
            return original(*args, **kwargs)
        monkeypatch.setattr(store, 'episode_messages', capture)
        tools, cursor, pages = MemoryTools(store), None, []
        while True:
            args = {'episode_id': 'e'}
            if cursor is not None:
                args['before_ordinal'] = cursor
            page = tools._episode_read(args)['episode']
            pages.append([m['ordinal'] for m in page['messages']])
            cursor = page['next_before_ordinal']
            if cursor is None:
                break
        assert pages == [[5, 6, 7], [2, 3, 4], [1]]
        assert boundaries == [4, 1, 0]
    finally:
        store.close()
