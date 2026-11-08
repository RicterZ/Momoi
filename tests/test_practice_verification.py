from copy import deepcopy
import json
from xml.etree.ElementTree import fromstring

import pytest

from momoi.memory import Memory, PlanningContext, TagCatalog
from momoi.memory.text import format_memory
from momoi.models import ToolCall, TurnDraft
from momoi.runtime.workflows.memory_rendering import memory_element
from momoi.tools.memory import MemoryTools
from tests.test_memory_writing import decision, request


@pytest.mark.parametrize('meta', [
    {'verification': 'maybe'}, {'verification': []}, {'basis': ''}, {'basis': 3},
    {'basis': 'x' * 501}, {'verification': 'verified'}, {'pitfalls': 'oops'},
    {'pitfalls': ['a'] * 4}, {'pitfalls': ['']}, {'pitfalls': [None]}, {'pitfalls': ['x' * 201]},
])
def test_invalid_practice_fields(meta):
    with pytest.raises(ValueError):
        TagCatalog().validate(meta)


def practice(meta):
    item = decision()
    item['memory'].update(kind='practice', content='重启前先运行配置检查', meta={'tags': [], **meta})
    return item


def review(memory, item, evidence=None):
    ctx = PlanningContext([request()], {'owner:new': '我喜欢无糖咖啡', **(evidence or {})}, {})
    return memory.writing.review(ctx, {'decisions': [item]})


@pytest.mark.parametrize('meta', [{}, {'verification': 'verified', 'basis': 'owner:new'},
                                 {'verification': 'verified', 'basis': 'execution:fake:c'}])
def test_review_requires_status_and_real_execution_reference(database, meta):
    with pytest.raises(ValueError, match='practice'):
        review(Memory(database), practice(meta))


def test_write_read_render_and_tag_edit_preserve_practice_metadata(database):
    memory = Memory(database)
    meta = {'verification': 'verified', 'basis': 'execution:turn:c', 'pitfalls': ['检查失败时不要重启 <服务>']}
    planned = review(memory, practice(meta), {'execution:turn:c': '{"ok":true}'})
    identifier = memory.apply(planned, operation_id='practice')['decisions'][0]['memory_ids'][0]
    row = memory.snapshots([identifier])[identifier]
    assert all(row['meta'][key] == value for key, value in meta.items())
    result = MemoryTools(None, memory=memory).execute(ToolCall('r', 'memory_read', {'ids': [identifier]}), [], TurnDraft())
    assert result['memories'][0]['meta'] == row['meta']
    root = fromstring(format_memory(row))
    assert root.findtext('basis') == meta['basis']
    assert root.findtext('pitfall') == meta['pitfalls'][0]
    assert root.find('caution') is None
    assert memory_element(row).findtext('verification') == 'verified'
    memory.repository.update_meta(row, {'tags': []})
    assert memory.snapshots([identifier])[identifier]['meta'] == row['meta']


def test_unverified_warning_and_legacy_compatibility(database):
    memory = Memory(database)
    planned = review(memory, practice({'verification': 'unverified'}))
    identifier = memory.apply(planned, operation_id='unverified')['decisions'][0]['memory_ids'][0]
    row = memory.snapshots([identifier])[identifier]
    assert '执行前先向用户确认' in fromstring(format_memory(row)).findtext('caution')
    legacy = deepcopy(row)
    legacy['meta'] = {'tags': []}
    assert fromstring(format_memory(legacy)).find('verification') is None
    assert MemoryTools(None, memory=memory).execute(
        ToolCall('r', 'memory_read', {'ids': [identifier]}), [], TurnDraft())['ok']


def test_other_kinds_need_no_verification_and_cannot_claim_it(database):
    memory = Memory(database, tags=TagCatalog({'food_drink': '饮食'}))
    review(memory, decision())
    item = decision()
    item['memory']['meta']['verification'] = 'unverified'
    with pytest.raises(ValueError, match='only for practice'):
        review(memory, item)


def test_execution_evidence_comes_from_actual_journal_not_owner_claims(store):
    store.begin_turn('source', 'owner', [])
    content, results = [], []
    for name, result in [('check', {'ok': True, 'exit_code': 0}), ('failed', {'ok': False}),
                         ('queued', {'ok': True, 'state': 'queued'}),
                         ('ambiguous', {'ok': True, 'ambiguous': True}),
                         ('memory_operation', {'ok': True})]:
        content.append({'type': 'tool_use', 'id': name, 'name': name, 'input': {'config': 'demo'}})
        results.append({'type': 'tool_result', 'tool_use_id': name, 'content': json.dumps(result)})
    content.append({'type': 'tool_use', 'id': 'missing', 'name': 'check', 'input': {}})
    store.append_turn_journal('source', 'assistant_exchange', {'content': content, 'results': results})
    evidence = store.memory_operation_execution_evidence('source')
    assert list(evidence) == ['execution:source:check']
    assert json.loads(evidence['execution:source:check'])['result']['exit_code'] == 0
    assert not store.memory_operation_execution_evidence('other')
    item = practice({'verification': 'verified', 'basis': 'execution:source:check'})
    review(store.memories, item, evidence)
    # Execution receipts cannot replace the authenticated owner citation.
    item['evidence'] = [{'event_id': 'execution:source:check', 'quote': 'ok'}]
    with pytest.raises(ValueError, match='owner event'):
        review(store.memories, item, evidence)


def test_eval_measures_actions_and_rejects_execution_before_confirmation(tmp_path):
    import asyncio
    from evals.practice_verification import evaluate_practice, scripted_complete
    from momoi.models import ProviderResponse

    assert asyncio.run(evaluate_practice(scripted_complete, tmp_path))['passed']

    async def always_execute(*args):
        return ProviderResponse([], [ToolCall('exec', 'execute_method', {})])

    second = tmp_path / 'negative'
    second.mkdir()
    result = asyncio.run(evaluate_practice(always_execute, second))
    assert not result['passed']
    assert all(not row['passed'] for row in result['results'] if row['verification'] == 'unverified')


@pytest.mark.parametrize('verified', [False, True])
def test_private_workflow_passes_execution_evidence_to_review_and_commit(daemon, verified):
    import asyncio
    from tests.test_memory_operations import event, submit, write, response

    source = event(daemon.store, text='记住：先检查演示配置再重启')
    daemon.store.begin_turn('source', 'owner', [source.event_id])
    if verified:
        daemon.store.append_turn_journal('source', 'assistant_exchange', {
            'content': [{'type': 'tool_use', 'id': 'check', 'name': 'config_check', 'input': {}}],
            'results': [{'type': 'tool_result', 'tool_use_id': 'check',
                         'content': '{"ok":true,"exit_code":0}'}],
        })
    submit(daemon.store, source)

    async def complete(system, messages, tools=None, **kwargs):
        root = fromstring('<request>' + messages[1]['content'][0]['text'] + '</request>')
        entries = root.findall('execution_evidence/execution')
        assert len(entries) == int(verified)
        item = write(source)
        item['memory']['kind'] = 'practice'
        meta = {'tags': [], 'verification': 'verified' if verified else 'unverified'}
        if verified:
            meta['basis'] = entries[0].get('id')
        item['memory']['meta'] = meta
        return response(ToolCall('finish', 'memory_operation_finish', {'decisions': [item]}))

    daemon.provider = type('Provider', (), {'complete': staticmethod(complete)})()
    asyncio.run(daemon._complete_memory_operation_turn('source', asyncio.Event()))
    row = daemon.store.memories.repository.active('practice', 'drink')
    assert row['meta']['verification'] == ('verified' if verified else 'unverified')


@pytest.mark.parametrize('basis,text', [('execution:source:fake', '{"ok":true}'),
                                       ('execution:other:check', '{"ok":true}')])
def test_commit_rejects_forged_execution_evidence_atomically(store, basis, text):
    from tests.test_memory_operations import event, submit, write

    source = event(store)
    submit(store, source)
    batch = store.claim_memory_operation('source')
    item = write(source)
    item['memory'].update(kind='practice', meta={'tags': [], 'verification': 'verified', 'basis': basis})
    ctx = PlanningContext(batch['operations'], {source.event_id: source.text, basis: text}, {})
    plan = store.memories.writing.review(ctx, {'decisions': [item]})
    with pytest.raises(ValueError, match='evidence_changed'):
        store.apply_memory_operation(batch, plan)
    assert not store.memories.repository.inventory()


def test_review_schema_requires_practice_verification_and_verified_basis():
    from jsonschema import Draft202012Validator
    from momoi.runtime.workflows.memory_operation.contracts import MEMORY_OPERATION_FINISH_SPEC

    validator = Draft202012Validator(MEMORY_OPERATION_FINISH_SPEC['input_schema'])
    for meta in ({}, {'verification': 'verified'}):
        assert list(validator.iter_errors({'decisions': [practice(meta)]}))
    for meta in ({'verification': 'unverified'}, {'verification': 'verified', 'basis': 'execution:t:c'}):
        assert not list(validator.iter_errors({'decisions': [practice(meta)]}))
