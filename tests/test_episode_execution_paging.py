from momoi.storage import Store
from momoi.tools.memory import MemoryTools
from tests.test_episode_execution import setup, record


def test_execution_pages_cover_every_turn_including_silent_and_missing_journals(tmp_path):
    store = Store(tmp_path / 'db')
    try:
        for ordinal in range(1, 26):
            turn = f't{ordinal}'
            setup(store, turn, ordinal)
            if ordinal % 3:
                record(store, 'exec', turn=turn)
        tools = MemoryTools(store)
        cursor = 0
        seen = []
        while True:
            page = tools._episode_read({'episode_id': 'e', 'execution_cursor': cursor})
            assert page['ok']
            seen.extend(turn['id'] for turn in page['turns'])
            if 'next_execution_cursor' not in page:
                break
            assert page['next_execution_cursor'] > cursor
            cursor = page['next_execution_cursor']
        assert seen == [f't{i}' for i in range(1, 26)]
        assert store.conversation_episode('e')['next_execution_cursor'] == 0
        assert tools._episode_read({'episode_id': 'e', 'execution_cursor': 25})['turns'] == []
        for arguments in ({'execution_cursor': True}, {'execution_cursor': -1},
                          {'execution_cursor': 0, 'turn_id': 't1'},
                          {'execution_cursor': 0, 'before_ordinal': 2}):
            assert tools._episode_read({'episode_id': 'e', **arguments})['error'] == 'invalid_execution_page'
        assert tools._episode_read({'episode_id': 'absent', 'execution_cursor': 0})['error'] == 'episode_not_found'
    finally:
        store.close()


def test_truncated_arguments_have_exact_deep_read_and_snapshot_recovery(tmp_path):
    import json
    from types import SimpleNamespace
    from momoi.models import ToolCall
    from momoi.runtime.agent.result_store import ToolResultStore
    from momoi.runtime.agent.tool_executor import ToolExecutor
    store = Store(tmp_path / 'db')
    try:
        setup(store)
        arguments = {'path': '/data', 'content': '完整参数\n' * 3000,
                     'items': list(range(100)), 'enabled': False}
        record(store, 'write_file', args=arguments, identifier='write-1')
        tools = MemoryTools(store)
        call = tools._episode_read({'episode_id': 'e', 'turn_id': 't'})['turns'][0]['execution'][0]['tools'][0]
        recovered = tools._episode_read(call['arguments_read'])
        assert recovered['arguments'] == arguments
        snapshots = ToolResultStore(tmp_path / 'results')
        executor = ToolExecutor(SimpleNamespace(workspace=tmp_path, database=tmp_path / 'db',
                               tool_result_max_chars=1000), store, None, None, None, snapshots)
        shown = executor.normalize(ToolCall('read', 'episode_read', call['arguments_read']), recovered, 'memory')
        assert shown['truncated']
        assert snapshots.historical_payload(shown['result_ref'])['arguments'] == arguments
        chunks = []
        cursor = None
        while True:
            page = snapshots.read(shown['result_ref'], cursor, max_chars=1000, provenance={})
            chunks.append(page['content'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert json.loads(''.join(chunks))['arguments'] == arguments
        assert tools._episode_read({'episode_id': 'e', 'tool_call_id': 'write-1'})['error'] == 'turn_id_required'
        assert tools._episode_read({'episode_id': 'e', 'turn_id': 't', 'tool_call_id': 'absent'})['error'] == 'tool_call_not_found'
        store.append_turn_journal('t', 'tool_call', {'tool_call_id': 'legacy', 'name': 'exec',
            'arguments': {'command': 'only a log...'}}, visibility='internal', trust='runtime')
        # Native journals take precedence; check legacy recovery in a separate Turn.
        setup(store, 'legacy-turn', 2)
        store.append_turn_journal('legacy-turn', 'tool_call', {'tool_call_id': 'legacy', 'name': 'exec',
            'arguments': {'command': 'only a log...'}}, visibility='internal', trust='runtime')
        assert tools._episode_read({'episode_id': 'e', 'turn_id': 'legacy-turn',
                                    'tool_call_id': 'legacy'})['error'] == 'complete_arguments_unavailable'
    finally:
        store.close()
