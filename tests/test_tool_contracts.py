"""Model-facing schemas must describe the runtime's argument boundaries."""

import copy

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC, heartbeat_begin_spec
from momoi.runtime.tool_contracts.conversation import (
    end_turn_tool_spec,
    send_bubbles_tool_spec,
)
from momoi.runtime.tool_contracts.reflection import REFLECTION_FINISH_SPEC
from momoi.runtime.tool_contracts.runtime import READ_TOOL_RESULT_SPEC, tool_enable_spec
from momoi.runtime.tool_contracts.voice import SEND_VOICE_TOOL_SPEC
from momoi.runtime.workflows.episode.contracts import (
    EPISODE_CLASSIFY_TURNS_SPEC,
    EPISODE_CONSOLIDATION_FINISH_SPEC,
    EPISODE_SUMMARY_FINISH_SPEC,
)
from momoi.runtime.workflows.memory_maintenance.contracts import (
    MEMORY_MAINTENANCE_FINISH_SPEC,
)
from momoi.runtime.workflows.memory_operation.contracts import (
    MEMORY_OPERATION_FINISH_SPEC,
    MEMORY_OPERATION_SEARCH_SPEC,
)
from momoi.tools.contracts.agenda import AGENDA_TOOL_SPECS
from momoi.tools.contracts.builtin import BUILTIN_TOOL_SPECS
from momoi.tools.contracts.memory import MEMORY_TOOL_SPECS
from momoi.tools.contracts.thinking import THINKING_TOOL_SPECS


SPECS = [
    RECALL_TOOL_SPEC,
    heartbeat_begin_spec({}),
    heartbeat_begin_spec({"web": "Browse"}),
    send_bubbles_tool_spec(["napcat"]),
    SEND_VOICE_TOOL_SPEC,
    READ_TOOL_RESULT_SPEC,
    tool_enable_spec({"web": "Browse"}),
    REFLECTION_FINISH_SPEC,
    EPISODE_CLASSIFY_TURNS_SPEC,
    EPISODE_CONSOLIDATION_FINISH_SPEC,
    EPISODE_SUMMARY_FINISH_SPEC,
    MEMORY_MAINTENANCE_FINISH_SPEC,
    MEMORY_OPERATION_FINISH_SPEC,
    MEMORY_OPERATION_SEARCH_SPEC,
    *AGENDA_TOOL_SPECS,
    *BUILTIN_TOOL_SPECS,
    *MEMORY_TOOL_SPECS,
    *THINKING_TOOL_SPECS,
    *(
        end_turn_tool_spec(stage)
        for stage in ("owner", "goal", "heartbeat", "webhook", "reply_followup")
    ),
]


def validator(spec):
    return Draft202012Validator(spec["input_schema"], format_checker=FormatChecker())


@pytest.mark.parametrize("spec", SPECS, ids=lambda spec: spec["name"])
def test_tool_schema_is_valid(spec):
    Draft202012Validator.check_schema(spec["input_schema"])


def test_recall_search_reuse_and_skip_have_distinct_arguments():
    check = validator(RECALL_TOOL_SPEC)
    unit = {
        "intent": "饮品偏好",
        "recall_mode": "search",
        "recall_queries": [{"semantic": "主人的饮品偏好"}],
        "recall_from_turn_id": "",
        "episode": {"action": "none"},
    }
    assert check.is_valid({"units": [unit]})
    assert not check.is_valid({"units": [{**unit, "recall_queries": []}]})
    reuse = {
        **unit,
        "recall_mode": "reuse",
        "recall_queries": [],
        "recall_from_turn_id": "T1",
    }
    assert check.is_valid({"units": [reuse]})
    assert not check.is_valid({"units": [{**reuse, "recall_from_turn_id": ""}]})
    assert not check.is_valid(
        {"units": [{**reuse, "recall_queries": unit["recall_queries"]}]}
    )
    skip = {**unit, "recall_mode": "skip", "recall_queries": []}
    assert check.is_valid({"units": [skip]})
    assert not check.is_valid({"units": [{**skip, "recall_queries": unit["recall_queries"]}]})
    assert not check.is_valid({"units": [{**skip, "recall_from_turn_id": "T1"}]})


def test_heartbeat_requires_strategy_for_work_and_queries_for_search():
    check = validator(heartbeat_begin_spec({}))
    rest = {
        "activity": "休息",
        "mode": "rest",
        "recall_mode": "skip",
        "recall_queries": [],
        "tool_groups": [],
        "strategy": [],
    }
    work = {**rest, "mode": "work", "strategy": ["检查结果"]}
    search = {
        **work,
        "recall_mode": "search",
        "recall_queries": [{"semantic": "此前约定", "keywords": []}],
    }
    for valid in (rest, work, search):
        assert check.is_valid(valid)
    for invalid in (
        {**rest, "strategy": ["检查结果"]},
        {**work, "strategy": []},
        {**search, "recall_queries": []},
        {**search, "recall_mode": "skip"},
    ):
        assert not check.is_valid(invalid)


def test_mixed_recall_modes_search_only_requested_units():
    from momoi.runtime.context.retrieval import select_plan_recall_queries

    query = {"semantic": "已有项目的截止日期", "keywords": ["项目"]}
    selected, reused, emitted, skipped = select_plan_recall_queries({"intent_units": [
        {"id": "u1", "recall": {"mode": "skip"}, "recall_queries": []},
        {"id": "u2", "recall": {"mode": "search"}, "recall_queries": [query]},
        {"id": "u3", "recall": {"mode": "reuse", "from_turn_id": "prior"}, "recall_queries": []},
    ]})
    assert len(selected) == 1
    assert selected[0]["unit_ids"] == ["u2"]
    assert reused == {"prior": ["u3"]}
    assert emitted == {query["semantic"]}
    assert skipped == set()


def test_goal_creation_requires_one_schedule_and_timezone():
    check = validator(
        next(spec for spec in AGENDA_TOOL_SPECS if spec["name"] == "goal_create")
    )
    base = {"title": "检查", "success_criteria": "检查完成", "next_action": "执行检查"}
    recurring = {**base, "schedule": {"kind": "daily", "times": ["10:30"]}}
    once = {**base, "next_review_at": "2099-09-09T10:30:00+08:00"}
    assert check.is_valid(recurring)
    assert check.is_valid(once)
    assert not check.is_valid(base)
    assert not check.is_valid({**recurring, **once})
    assert not check.is_valid({**once, "next_review_at": "2099-09-09T10:30:00"})


def test_goal_update_can_keep_existing_state_but_cannot_mix_schedule_changes():
    update = validator(
        next(spec for spec in AGENDA_TOOL_SPECS if spec["name"] == "goal_update")
    )
    review = validator(end_turn_tool_spec("goal"))
    schedule = {"kind": "interval", "every_seconds": 3600}
    assert update.is_valid({"goal_id": "existing", "status": "active"})
    assert not update.is_valid({"goal_id": "existing", "status": "waiting"})
    assert not update.is_valid(
        {
            "goal_id": "existing",
            "status": "active",
            "schedule": schedule,
            "clear_schedule": True,
        }
    )
    assert not review.is_valid(
        {
            "goal": {
                "status": "active",
                "result": "已检查",
                "next_action": "再检查",
                "schedule": schedule,
                "clear_schedule": True,
            }
        }
    )


@pytest.mark.parametrize(
    "activation,kind,expiry,valid",
    [
        ("recent", "preference", 4092595200, True),
        ("recent", "preference", None, False),
        ("recall", "preference", None, True),
        ("recall", "preference", 4092595200, False),
        ("always", "preference", None, True),
        ("always", "episodic", None, False),
    ],
)
def test_memory_write_activation_matches_runtime(activation, kind, expiry, valid):
    from momoi.runtime.workflows.memory_operation.parsing import parse_decisions
    from unittest.mock import patch

    args = {
        "decisions": [
            {
                "action": "write",
                "operation_ids": ["op"],
                "reason": "主人要求",
                "target_ids": [],
                "evidence": [{"event_id": "owner", "quote": "喝茶"}],
                "memory": {
                    "kind": kind,
                    "key": "drink",
                    "content": "喝茶",
                    "activation": activation,
                    "expires_at": expiry,
                },
            }
        ]
    }
    assert validator(MEMORY_OPERATION_FINISH_SPEC).is_valid(args) == valid
    with patch(
        "momoi.runtime.workflows.memory_operation.parsing.time.time",
        return_value=4092595100,
    ):
        if valid:
            assert parse_decisions(
                copy.deepcopy(args),
                [{"id": "op", "type": "add", "event_id": "owner"}],
                {},
                {"owner": "喝茶"},
                720,
            )
        else:
            with pytest.raises(ValueError):
                parse_decisions(
                    copy.deepcopy(args),
                    [{"id": "op", "type": "add", "event_id": "owner"}],
                    {},
                    {"owner": "喝茶"},
                    720,
                )
