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
