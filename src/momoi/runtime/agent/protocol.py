import copy
from dataclasses import dataclass
from typing import Literal
from typing import Any

from ...models import AgentReply
from ..parsing import parse_response
from ..turn_support import ExternalToolTurnError
from .workflow import TurnExecutionSpec, WorkflowProtocolError

MAX_CONSECUTIVE_THOUGHT_ROUNDS = 6
MAX_CONSECUTIVE_EXECUTION_FAILURES = 8
NO_TOOL_GUIDANCE = (
    "[运行时提示] 仅刚才这条纯文本是内部思考，不会发送给用户。"
    "此前工具调用及结果仍然有效，不要因此重复 reply。"
    "若本轮已完成，请调用当前阶段的结束工具；否则继续调用所需工具。"
)

_PRIVATE_REASONING_BLOCK_TYPES = frozenset(
    {"reasoning", "thinking", "redacted_thinking"}
)


def assistant_history_content(content: object) -> object:
    """Keep protocol output for the next round without replaying private thought."""

    if not isinstance(content, list):
        return copy.deepcopy(content)
    return [
        copy.deepcopy(block)
        for block in content
        if not (
            isinstance(block, dict)
            and block.get("type") in _PRIVATE_REASONING_BLOCK_TYPES
        )
    ]


def assistant_history_message(content: object, continuation: dict | None = None) -> dict:
    """Retain provider-owned continuation without turning it into visible text."""
    message = {"role": "assistant", "content": assistant_history_content(content)}
    if continuation:
        message["provider_continuation"] = copy.deepcopy(continuation)
    return message


@dataclass(frozen=True)
class NoToolResolution:
    action: Literal["retry", "return"]
    failed_rounds: int
    log_rejection: bool = False


def handle_no_tool_response(
    messages: list[dict[str, Any]],
    content: object,
    *,
    workflow_correction: str | None,
    heartbeat_turn: bool,
    harness_started: bool,
    goal_turn: bool,
    require_response: bool,
    owner_turn: bool,
    failed_rounds: int,
    last_tool_error: str,
    external_effect: bool = False,
    continuation: dict | None = None,
    max_failures: int = MAX_CONSECUTIVE_THOUGHT_ROUNDS,
) -> NoToolResolution:
    if not (workflow_correction is not None or heartbeat_turn or goal_turn or require_response):
        return NoToolResolution("return", failed_rounds)
    failed_rounds += 1
    messages.append(assistant_history_message(content, continuation))
    if failed_rounds >= max_failures:
        error_type = ExternalToolTurnError if external_effect and workflow_correction is None else WorkflowProtocolError
        raise error_type("consecutive_thought_round_limit")
    guidance = workflow_correction or NO_TOOL_GUIDANCE
    if failed_rounds >= max_failures - 1:
        guidance += " 已接近连续无行动轮次上限，请在下一轮行动或结束。"
    messages.append({"role": "user", "content": guidance})
    return NoToolResolution("retry", failed_rounds)


def owner_request_messages(
    messages: list[dict[str, Any]], *, remind_bubbles: bool
) -> list[dict[str, Any]]:
    """Build an Owner-only wire copy without changing canonical Turn history."""

    return copy.deepcopy(messages)


def parse_end_turn(
    arguments: dict[str, Any],
    *,
    execution: TurnExecutionSpec,
    visible_since_owner_update: bool,
) -> tuple[AgentReply | None, str | None]:
    if not execution.require_response:
        return None, "end_turn_not_allowed"
    reply, error = parse_response(arguments)
    if reply is None:
        return None, error
    if reply.expects_reply and not visible_since_owner_update:
        return None, "reply_expectation_without_visible_bubble"
    if execution.reply_followup:
        if reply.should_schedule_reply_wait:
            return None, "reply_followup_cannot_schedule_another_wait"
    return reply, None
