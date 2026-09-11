"""One smoke check for model-facing schemas; behavior belongs to workflow tests."""

from jsonschema import Draft202012Validator

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
from momoi.runtime.workflows.memory_maintenance.contracts import MEMORY_MAINTENANCE_FINISH_SPEC
from momoi.runtime.workflows.memory_operation.contracts import (
    MEMORY_OPERATION_FINISH_SPEC,
    MEMORY_OPERATION_SEARCH_SPEC,
)
from momoi.tools.contracts.agenda import AGENDA_TOOL_SPECS
from momoi.tools.contracts.builtin import BUILTIN_TOOL_SPECS
from momoi.tools.contracts.memory import MEMORY_TOOL_SPECS
from momoi.tools.contracts.thinking import THINKING_TOOL_SPECS


def test_model_tool_schemas_compile_and_examples_validate():
    specs = [
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
        *(end_turn_tool_spec(stage) for stage in (
            "owner", "goal", "heartbeat", "webhook", "reply_followup",
        )),
    ]
    for spec in specs:
        schema = spec["input_schema"]
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for example in schema.get("examples", []):
            validator.validate(example)
