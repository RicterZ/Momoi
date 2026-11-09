from momoi.tools.validation import validate_tool_arguments
from momoi.tools.contracts.agenda import GOAL_REVIEW_SCHEMA


def test_nested_paths_and_array_items():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"count": {"type": "integer", "minimum": 1}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
            }
        },
    }
    _, error = validate_tool_arguments(
        "test", {"items": [{"count": False}, {"extra": 1}]}, schema
    )
    assert "items.0.count" in error["invalid_fields"]
    assert "items.1.count" in error["missing_fields"]
    assert "items.1.extra" in error["unexpected_fields"]


def test_conditions_and_uniqueness_are_enforced():
    for args in [
        {"status": "active", "result": "checked"},
        {
            "status": "blocked",
            "result": "checked",
            "blocked_reason": "x",
            "next_review_at": "2030-01-01T00:00:00Z",
        },
        {
            "status": "active",
            "result": "checked",
            "next_action": "again",
            "schedule": {"kind": "daily", "times": ["10:30", "10:30"]},
        },
    ]:
        assert validate_tool_arguments("goal_review", args, GOAL_REVIEW_SCHEMA)[1]


def test_validation_preserves_omission_null_and_open_maps():
    schema = {
        "type": "object",
        "properties": {"optional": {"type": "integer", "default": 2}, "payload": {}},
        "additionalProperties": True,
    }
    args = {"payload": None, "extension": {"nested": 1}}
    assert validate_tool_arguments("test", args, schema) == (args, None)
