import json
from types import SimpleNamespace

from momoi.models import ToolCall
from momoi.runtime.agent.tool_executor import ToolExecutor
from momoi.runtime.agent.result_store import ToolResultStore
from momoi.runtime.turn_support import tool_result_block
from momoi.tools.presentation import present_result


def normalized(tmp_path, name, raw):
    snapshots = ToolResultStore(tmp_path / 'results')
    executor = ToolExecutor(SimpleNamespace(workspace=tmp_path, database=tmp_path / 'db',
                           tool_result_max_chars=12000), None, None, None, None, snapshots)
    result = executor.normalize(ToolCall('c', name, {}), raw, 'builtin')
    return json.loads(tool_result_block('c', result)['content']), snapshots


def test_memory_search_hides_ranking_without_losing_source_or_snapshot(tmp_path):
    item = {'id': 1, 'kind': 'preference', 'content': 'fact', 'source': 'reflection',
            'confidence': .5, 'evidence': 'quote', 'updated_at': 10,
            'search_score': .8, 'dense_cosine': .9, 'unit_ids': ['u']}
    shown, snapshots = normalized(tmp_path, 'memory_search', {'ok': True, 'results': [item]})
    assert shown['results'] == [{k: v for k, v in item.items() if k not in {'search_score', 'dense_cosine', 'unit_ids'}}]
    assert snapshots.historical_payload(shown['result_ref'])['results'] == [item]
    refitted = json.loads(snapshots.refit(json.dumps(shown), max_chars=1000))
    assert 'dense_cosine' not in refitted['results'][0]
    assert present_result(shown, tool_name='memory_search') == shown


def test_thinking_read_keeps_excerpts_and_full_trace_in_snapshot(tmp_path):
    call = {'turn_id': 't', 'call_id': 'c', 'stage': 'owner', 'round': 1,
            'reasoning': '长记录' * 3000, 'assistant_text': 'full reply',
            'trace': {'tool_calls': [{'arguments': {'large': 'x' * 10000}}]}, 'model': 'debug-model'}
    shown, snapshots = normalized(tmp_path, 'thinking_read', {'ok': True, 'calls': [call]})
    assert len(shown['calls'][0]['reasoning']) < len(call['reasoning'])
    assert shown['calls'][0]['call_id'] == 'c'
    assert 'trace' not in shown['calls'][0] and 'assistant_text' not in shown['calls'][0]
    assert snapshots.historical_payload(shown['result_ref'])['calls'] == [call]
    assert present_result(shown, tool_name='thinking_read') == shown


def test_episode_default_shows_message_pages_and_execution_entry(tmp_path):
    messages = [{'id': i, 'ordinal': i, 'turn_id': f't{i}', 'role': 'user',
                 'timestamp': 'today', 'created_at': i, 'content': str(i) * 4000} for i in range(1, 6)]
    episode = {'id': 'e', 'title': 'topic', 'status': 'closed', 'narrative_summary': 'large' * 1000,
               'messages': messages, 'turns': [{'execution': 'large'}], 'next_before_ordinal': None}
    shown, snapshots = normalized(tmp_path, 'episode_read', {'ok': True, 'episode': episode})
    page = shown['episode']
    assert 'turns' not in page and 'narrative_summary' not in page
    assert page['next_execution_cursor'] == 0 and page['next_before_ordinal'] == 3
    assert [item['ordinal'] for item in page['messages']] == [3, 4, 5]
    assert all(item['next_content_offset'] == len(item['content']) == 2000 for item in page['messages'])
    assert all('created_at' not in item for item in page['messages'])
    assert snapshots.historical_payload(shown['result_ref'])['episode'] == episode
    assert present_result(shown, tool_name='episode_read') == shown


def test_plan_writes_return_version_status_and_identity_not_full_plan(tmp_path):
    raw = {'ok': True, 'plan_id': 'p', 'title': 'work', 'status': 'awaiting_approval', 'version': 2,
           'step_index': 0, 'resume_safety': 'requires_review', 'request': 'long request',
           'steps': [{'task': 'long task'}], 'review': {'validation': 'long review'}}
    for name in ['plan_create', 'plan_submit', 'plan_start', 'plan_update', 'plan_cancel', 'plan_resume']:
        shown, snapshots = normalized(tmp_path, name, raw)
        assert shown['status'] == 'awaiting_approval' and shown['version'] == 2
        assert shown['plan_id'] == 'p' and shown['resume_safety'] == 'requires_review'
        assert not {'request', 'steps', 'review'} & shown.keys()
        assert snapshots.historical_payload(shown['result_ref'])['steps'] == raw['steps']
    shown, _ = normalized(tmp_path, 'plan_get', raw)
    assert shown['steps'] == raw['steps']


def test_goal_write_preserves_staged_state_and_calculated_schedule(tmp_path):
    goal = {'id': 'g', 'title': 'remind', 'status': 'active', 'next_review_at': 12345,
            'schedule': {'kind': 'interval', 'every_seconds': 3600}, 'source_event_id': 'private-event',
            'success_criteria': 'long criteria', 'waiting_for': '', 'latest_result': ''}
    for name in ['goal_create', 'goal_update', 'goal_finish', 'goal_cancel', 'goal_review']:
        shown, snapshots = normalized(tmp_path, name, {'ok': True, 'state': 'staged', 'goal': goal})
        assert shown['goal_id'] == 'g' and shown['state'] == 'staged'
        assert shown['next_review_at'] == 12345 and shown['schedule'] == goal['schedule']
        assert 'goal' not in shown and 'source_event_id' not in shown
        assert snapshots.historical_payload(shown['result_ref'])['goal'] == goal


def test_file_projection_preserves_exact_text_hash_and_resume_offsets(tmp_path):
    from momoi.tools.builtin import BuiltinTools
    from momoi.runtime.agent.budget import ToolResultFitter
    path = tmp_path / 'note'
    path.write_text('line\n' * 250)
    tools = BuiltinTools(tmp_path)
    raw = tools._read_file({'path': 'note'})
    assert len(raw['lines']) == 200 and raw['next_content_offset'] == 1000
    shown, snapshots = normalized(tmp_path, 'read_file', raw)
    assert 'lines' not in shown
    assert shown['content'] == 'line\n' * 200
    assert (shown['start_line'], shown['end_line']) == (1, 200)
    assert shown['sha256'] == raw['sha256']
    fitted = json.loads(ToolResultFitter().fit(json.dumps(shown), 700))
    resumed = tools._read_file({'path': 'note', 'content_offset': fitted['next_content_offset']})
    assert fitted['content'] + ''.join(item['text'] for item in resumed['lines']) == path.read_text()
    assert fitted['end_line'] <= 200
    assert snapshots.historical_payload(shown['result_ref'])['lines'] == raw['lines']
