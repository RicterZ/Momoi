"""Model-facing schemas must describe the runtime's argument boundaries."""

import copy

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC, heartbeat_begin_spec
from momoi.runtime.tool_contracts.conversation import (
    HEARTBEAT_ACTIVITY_TOOL_SPEC,
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
    HEARTBEAT_ACTIVITY_TOOL_SPEC,
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


@pytest.mark.parametrize("spec", [RECALL_TOOL_SPEC, *(
    end_turn_tool_spec(stage, heartbeat_min_interval_seconds=180, heartbeat_max_interval_seconds=600)
    for stage in ("owner", "heartbeat", "goal", "reply_followup", "webhook")
)])
def test_recall_and_end_turn_examples_match_actual_stage_schema(spec):
    check = validator(spec)
    for example in spec["input_schema"]["examples"]:
        check.validate(example)


def test_recall_rejects_observed_flattened_and_stringified_arguments():
    check = validator(RECALL_TOOL_SPEC)
    unit = copy.deepcopy(RECALL_TOOL_SPEC["input_schema"]["examples"][0]["units"][0])
    assert not check.is_valid(unit)
    for key, value in (("recall_queries", "[]"), ("episode", '{"action":"none"}')):
        assert not check.is_valid({"units": [{**unit, key: value}]})
    for episode in ({"action": "continue"}, {"action": "new", "ref": "plain-id"},
                    {"action": "new", "ref": "new:valid", "title": " "}):
        assert not check.is_valid({"units": [{**unit, "episode": episode}]})


def test_end_turn_correction_explains_wrong_types_and_uses_stage_example():
    from momoi.runtime.tool_contracts.conversation import end_turn_correction
    for stage in ("owner", "heartbeat", "goal", "reply_followup"):
        spec = end_turn_tool_spec(stage)
        detail = end_turn_correction("invalid_mood_decision", spec["input_schema"], {})
        validator(spec).validate(detail["example_arguments"])
        assert "Supply all missing fields:" in detail["message"]
    schema = end_turn_tool_spec("owner")["input_schema"]
    assert "A boolean is invalid" in end_turn_correction("invalid_reply_wait_decision", schema, {})["message"]
    assert "A string is invalid" in end_turn_correction("invalid_mood_decision", schema, {})["message"]


def test_heartbeat_correction_uses_configured_interval():
    from momoi.runtime.tool_contracts.conversation import end_turn_correction
    schema = end_turn_tool_spec("heartbeat", heartbeat_min_interval_seconds=180,
                                heartbeat_max_interval_seconds=600)["input_schema"]
    detail = end_turn_correction("heartbeat_interval_out_of_range", schema, {})
    assert "integer 3-10" in detail["message"]


def test_shared_end_turn_examples_remain_fixed_after_private_corrections():
    import json
    from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC, end_turn_correction
    before = json.dumps(END_TURN_TOOL_SPEC, ensure_ascii=False)
    for example in END_TURN_TOOL_SPEC["input_schema"]["examples"]:
        validator(END_TURN_TOOL_SPEC).validate(example)
    for stage in ("owner", "heartbeat", "goal", "reply_followup", "webhook"):
        schema = end_turn_tool_spec(stage, heartbeat_min_interval_seconds=180,
                                    heartbeat_max_interval_seconds=600)["input_schema"]
        detail = end_turn_correction("invalid_end_turn_arguments", schema, {})
        detail["example_arguments"].clear()
        assert json.dumps(END_TURN_TOOL_SPEC, ensure_ascii=False) == before


def test_end_turn_branch_examples_are_unambiguous_and_valid():
    from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC
    schema = END_TURN_TOOL_SPEC["input_schema"]
    for branch in schema["oneOf"]:
        for example in branch["examples"]:
            validator(END_TURN_TOOL_SPEC).validate(example)
            assert sum(Draft202012Validator(candidate).is_valid(example)
                       for candidate in schema["oneOf"]) == 1


def test_observed_unchanged_with_update_fields_is_invalid_and_correction_is_precise():
    from momoi.runtime.parsing import parse_response
    from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC, end_turn_correction
    args = {"reply_wait": {"wait": False}, "mood": {
        "decision": "unchanged", "state": "content_peaceful", "intensity": 0.7,
        "cause": "在老师怀里相拥入睡",
    }}
    assert not validator(END_TURN_TOOL_SPEC).is_valid(args)
    reply, error = parse_response(args)
    assert reply is None and error == "invalid_mood_decision"
    detail = end_turn_correction(error, end_turn_tool_spec("owner")["input_schema"], args)
    assert {item["path"] for item in detail["field_errors"]} == {
        "$.mood.state", "$.mood.intensity", "$.mood.cause",
    }
    assert "permits ONLY decision" in detail["message"]
    repaired = copy.deepcopy(args)
    for item in detail["field_errors"]:
        repaired["mood"].pop(item["path"].split(".")[-1])
    validator(END_TURN_TOOL_SPEC).validate(repaired)
    assert parse_response(repaired)[1] is None


def test_shared_schema_heartbeat_shape_still_requires_runtime_stage_check():
    from momoi.runtime.parsing import parse_response
    from momoi.runtime.tool_contracts.conversation import END_TURN_TOOL_SPEC, end_turn_correction
    args = {"reply_wait": {"wait": False}, "mood": {"decision": "unchanged"},
            "heartbeat": {"next_check_minutes": 60, "reason": "稍后检查"}}
    # The shared schema must accept a valid Heartbeat shape. It cannot know
    # which trusted workflow is executing; runtime must enforce that boundary.
    validator(END_TURN_TOOL_SPEC).validate(args)
    assert parse_response(args, require_heartbeat=True)[1] is None
    reply, error = parse_response(args)
    assert reply is None and error == "heartbeat_state_not_allowed"
    detail = end_turn_correction(error, end_turn_tool_spec("owner")["input_schema"], args)
    assert detail["field_errors"] == [{"path": "$.heartbeat",
        "issue": "forbidden_in_current_workflow", "expected": "field omitted"}]
