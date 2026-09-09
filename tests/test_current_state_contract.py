from jsonschema import Draft202012Validator
import pytest

from momoi.storage import Store


@pytest.fixture
def manager(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    yield store.current_state
    store.close()


def arguments():
    return {
        "delete": [],
        "add": [
            {
                "subject": "owner",
                "key": "availability",
                "value": "busy",
                "ttl_seconds": 60,
            }
        ],
    }


def test_schema_and_manager_accept_same_input_and_enforce_replacement(manager):
    schema = manager.change_schema()
    Draft202012Validator.check_schema(schema)
    check = Draft202012Validator(schema)
    args = arguments()
    assert check.is_valid(args)
    first = manager.apply_arguments(
        args, source_turn_id="a", operation_id="a", expected_revision=0
    )
    replacement = arguments()
    replacement["delete"] = [first.added[0].id]
    replacement["add"][0]["value"] = "free"
    assert check.is_valid(replacement)
    second = manager.apply_arguments(
        replacement, source_turn_id="b", operation_id="b", expected_revision=1
    )
    assert second.removed == first.added
    assert manager.snapshot().slots[0].value == "free"
    assert (
        manager.apply_arguments(
            replacement, source_turn_id="b", operation_id="b", expected_revision=1
        )
        == second
    )
    assert check.is_valid({"add": [], "delete": []})


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", ""),
        ("subject", " "),
        ("subject", "s" * 129),
        ("key", "UPPER"),
        ("key", "k" * 65),
        ("key", "line\n"),
        ("value", " "),
        ("value", "v" * 513),
        ("ttl_seconds", 0),
        ("ttl_seconds", True),
        ("ttl_seconds", 86401),
        ("ttl_seconds", 604801),
        ("extra", "field"),
    ],
)
def test_schema_and_manager_reject_bad_slot_fields(manager, field, value):
    args = arguments()
    args["add"][0][field] = value
    assert not Draft202012Validator(manager.change_schema()).is_valid(args)
    with pytest.raises(ValueError):
        manager.apply_arguments(
            args, source_turn_id="a", operation_id="a", expected_revision=0
        )
    assert manager.snapshot().revision == 0


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"add": []},
        {"add": {}, "delete": []},
        {"add": [], "delete": ""},
        {"add": [], "delete": [], "source_turn_id": "model-owned"},
        {"add": [], "delete": [" "]},
        {"add": [], "delete": ["same", "same"]},
        {"add": [], "delete": ["i" * 129]},
    ],
)
def test_schema_and_manager_reject_bad_change_shape(manager, args):
    assert not Draft202012Validator(manager.change_schema()).is_valid(args)
    with pytest.raises(ValueError):
        manager.apply_arguments(
            args, source_turn_id="a", operation_id="a", expected_revision=0
        )
    assert manager.snapshot().revision == 0


def test_callers_cannot_modify_shared_schema(manager):
    original = manager.change_schema()
    changed = manager.change_schema()
    changed["properties"]["add"]["items"]["properties"]["key"]["pattern"] = "anything"
    assert manager.change_schema() == original


def test_exactly_24_hours_is_accepted_by_schema_and_manager(manager):
    args = arguments()
    args["add"][0]["ttl_seconds"] = 86400
    assert Draft202012Validator(manager.change_schema()).is_valid(args)
    change = manager.apply_arguments(
        args, source_turn_id="day", operation_id="day", expected_revision=0,
    )
    slot = change.added[0]
    assert slot.expires_at - slot.created_at == 86400
