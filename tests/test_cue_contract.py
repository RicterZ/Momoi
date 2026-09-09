from momoi.semantic.cue_contract import (
    EVENT_RETRIEVAL_CONTRACT, CUE_ARCHIVE_CONTRACT, CUE_QUERY_CONTRACT,
)
from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC
from momoi.runtime.workflows.episode.contracts import EPISODE_SUMMARY_FINISH_SPEC


def descriptions(value, name):
    if isinstance(value, dict):
        if name in value and isinstance(value[name], dict):
            yield value[name].get('description', '')
        for child in value.values():
            yield from descriptions(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from descriptions(child, name)


def test_archive_and_owner_query_share_event_contract():
    assert CUE_ARCHIVE_CONTRACT.startswith(EVENT_RETRIEVAL_CONTRACT)
    assert CUE_QUERY_CONTRACT.startswith(EVENT_RETRIEVAL_CONTRACT)
    assert list(descriptions(RECALL_TOOL_SPEC, 'semantic')) == [CUE_QUERY_CONTRACT]
    cues = list(descriptions(EPISODE_SUMMARY_FINISH_SPEC, 'recall_cues'))
    assert len(cues) == 1 and cues[0].startswith(CUE_ARCHIVE_CONTRACT)


def test_heartbeat_uses_same_query_contract_as_owner():
    from momoi.runtime.tool_contracts.context import heartbeat_begin_spec
    assert list(descriptions(heartbeat_begin_spec({}), 'semantic')) == [CUE_QUERY_CONTRACT]
