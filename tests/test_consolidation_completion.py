import asyncio
from types import SimpleNamespace

import pytest

from momoi.models import AgentReply, ProviderResponse
from tests.test_episode_annealing import workflow_response


@pytest.mark.parametrize("reprocess_deferred", [False, True])
def test_no_tools_completes_only_after_all_durable_decisions(daemon, reprocess_deferred):
    for number in range(1, 3):
        daemon.store.commit_turn([], f"pending-{number}", AgentReply([]),
                                 turn_id=f"pending-{number}")
    if reprocess_deferred:
        daemon.store.apply_episode_consolidation(["pending-2"], [{
            "action": "defer", "turn_ids": ["pending-2"], "reason": "等待后续",
        }], [])
        daemon.store.begin_turn("later", "owner", [])
        daemon.store.complete_background_turn("later")
    candidate = daemon.store.claim_episode_consolidation_candidate(minimum=1)
    assert [turn["turn_id"] for turn in candidate["turns"]] == ["pending-1", "pending-2"]
    rounds = 0

    async def complete(system, messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        assert {tool["name"] for tool in tools} == {"episode_classify_turns"}
        state = daemon.store._db.execute(
            "SELECT state FROM turns WHERE workflow_kind='episode_consolidate'"
        ).fetchone()["state"]
        assert state != "completed"
        if rounds in {1, 3, 5}:
            # Initial, partial, and fully covered responses all use the same protocol.
            return ProviderResponse([], [])
        assert "remaining Turns" in str(messages[-1])
        return workflow_response("episode_classify_turns", {"decisions": [{
            "action": "ignore" if rounds == 2 else "defer",
            "turn_ids": ["T-1" if rounds == 2 else "T-2"],
            "reason": "分类决定",
        }]})

    daemon.provider = SimpleNamespace(complete=complete)
    assert asyncio.run(daemon._consolidate_episode_turns(candidate))
    assert rounds == 5
    assert daemon.store.episode_consolidation_remaining(["pending-1", "pending-2"]) == []
    assert daemon.store._db.execute(
        "SELECT state FROM turns WHERE workflow_kind='episode_consolidate'"
    ).fetchone()["state"] == "completed"


def test_no_tools_without_decisions_never_marks_consolidation_complete(daemon):
    daemon.store.commit_turn([], "pending", AgentReply([]), turn_id="pending")
    candidate = daemon.store.claim_episode_consolidation_candidate(minimum=1)

    async def complete(*args, **kwargs):
        return ProviderResponse([], [])

    daemon.provider = SimpleNamespace(complete=complete)
    with pytest.raises(RuntimeError, match="consecutive_thought_round_limit"):
        asyncio.run(daemon._consolidate_episode_turns(candidate))
    assert daemon.store.episode_consolidation_remaining(["pending"]) == ["pending"]
    assert daemon.store._db.execute(
        "SELECT state FROM turns WHERE workflow_kind='episode_consolidate'"
    ).fetchone()["state"] != "completed"
