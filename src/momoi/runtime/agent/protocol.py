import copy
from dataclasses import dataclass
from typing import Literal
from typing import Any

from ...models import AgentReply, ToolCall
from ..parsing import parse_response
from ..turn_support import tool_error_block
from ..turn_support import ExternalToolTurnError, MAX_CONSECUTIVE_TOOL_FAILURES
from .workflow import TurnExecutionSpec, WorkflowProtocolError

OWNER_BUBBLE_REQUEST_REMINDER = (
    "Native tool calls only: if bubbles are warranted, call send_bubbles with "
    "them; otherwise call the next work or terminal tool."
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
) -> NoToolResolution:
    if workflow_correction is not None or heartbeat_turn or goal_turn or require_response:
        failed_rounds += 1
        if failed_rounds >= MAX_CONSECUTIVE_TOOL_FAILURES:
            error_type = (
                ExternalToolTurnError
                if external_effect and workflow_correction is None
                else WorkflowProtocolError
            )
            raise error_type(
                last_tool_error or (
                    "repeated workflow protocol failures"
                    if workflow_correction is not None
                    else "native_tool_call_required"
                )
            )
    if workflow_correction is not None:
        assistant_content = assistant_history_content(content)
        messages.extend(
            [
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": workflow_correction},
            ]
        )
        return NoToolResolution("retry", failed_rounds)
    if heartbeat_turn and not harness_started:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": assistant_history_content(content),
                },
                {
                    "role": "user",
                    "content": (
                        "[Trusted runtime protocol error: no native tool call was "
                        "returned. Call heartbeat_begin alone before any other "
                        "Heartbeat action.]"
                    ),
                },
            ]
        )
        return NoToolResolution("retry", failed_rounds)
    if goal_turn:
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": assistant_history_content(content),
                },
                {
                    "role": "user",
                    "content": (
                        "[Trusted runtime protocol error. Plain text was not stored. "
                        "Continue with native tools, or call end_turn alone with the "
                        "current Goal outcome in goal when ready.]"
                    ),
                },
            ]
        )
        return NoToolResolution("retry", failed_rounds)
    if not require_response:
        return NoToolResolution("return", failed_rounds)
    if owner_turn and not harness_started:
        correction = (
            "[Trusted runtime protocol error: no native tool call was returned. Call "
            "recall first and alone as a native tool call; never write or imitate tool "
            "syntax in text.]"
        )
    elif owner_turn:
        correction = (
            "[Trusted runtime protocol error: no native tool call was returned. "
            "Call send_bubbles with the owner-visible bubbles, without end_turn. "
            "After its result, call end_turn alone on the next step.]"
        )
    else:
        correction = (
            "[Trusted runtime protocol error: no native tool call was returned. "
            "Retry using native tool calls only, following the current workflow.]"
        )
    messages.extend(
        [
            {
                "role": "assistant",
                "content": assistant_history_content(content),
            },
            {"role": "user", "content": correction},
        ]
    )
    return NoToolResolution("retry", failed_rounds, log_rejection=True)


def owner_request_messages(
    messages: list[dict[str, Any]], *, remind_bubbles: bool
) -> list[dict[str, Any]]:
    """Build an Owner-only wire copy without changing canonical Turn history."""

    request_messages = copy.deepcopy(messages)
    if not remind_bubbles:
        return request_messages
    user_message = next(
        (
            message
            for message in reversed(request_messages)
            if message.get("role") == "user"
        ),
        None,
    )
    if user_message is None:
        return request_messages
    content = user_message.get("content")
    if isinstance(content, str):
        user_message["content"] = (
            f"{content}\n\n{OWNER_BUBBLE_REQUEST_REMINDER}".lstrip()
        )
        return request_messages
    if not isinstance(content, list):
        user_message["content"] = OWNER_BUBBLE_REQUEST_REMINDER
        return request_messages
    text_block = next(
        (
            block
            for block in reversed(content)
            if isinstance(block, dict) and block.get("type") == "text"
        ),
        None,
    )
    if text_block is None:
        content.append({"type": "text", "text": OWNER_BUBBLE_REQUEST_REMINDER})
    else:
        text = str(text_block.get("text") or "")
        text_block["text"] = (
            f"{text}\n\n{OWNER_BUBBLE_REQUEST_REMINDER}".lstrip()
        )
    return request_messages


def harness_correction(
    calls: list[ToolCall], error: str, *, owner_turn: bool
) -> list[dict[str, Any]]:
    correction: list[dict[str, Any]] = [
        tool_error_block(call.id, error) for call in calls
    ]
    if error != "assistant_text_forbidden":
        return correction
    if owner_turn and len(calls) == 1 and calls[0].name == "end_turn":
        text = (
            "[Trusted runtime protocol error: assistant text accompanied tool calls. "
            "Call send_bubbles with the owner-visible bubbles, without "
            "end_turn. After its result, call end_turn alone on the next step.]"
        )
    else:
        text = (
            "[Trusted runtime protocol error: assistant text accompanied tool calls. "
            "Repeat the intended action using native tool calls only.]"
        )
    correction.append({"type": "text", "text": text})
    return correction


def parse_end_turn(
    arguments: dict[str, Any],
    *,
    execution: TurnExecutionSpec,
    visible_since_owner_update: bool,
    heartbeat_min_interval_seconds: int,
    heartbeat_max_interval_seconds: int,
) -> tuple[AgentReply | None, str | None]:
    if not execution.require_response:
        return None, "end_turn_not_allowed"
    reply, error = parse_response(
        arguments,
        require_heartbeat=execution.heartbeat,
        allow_activity_update=execution.stage == "owner",
    )
    if reply is None:
        return None, error
    if execution.heartbeat and reply.heartbeat:
        seconds = int(reply.heartbeat["next_check_minutes"]) * 60
        if not (
            heartbeat_min_interval_seconds
            <= seconds
            <= heartbeat_max_interval_seconds
        ):
            return None, "heartbeat_interval_out_of_range"
    if reply.expects_reply and not visible_since_owner_update:
        return None, "reply_expectation_without_visible_bubble"
    if execution.reply_followup:
        if not visible_since_owner_update:
            return None, "reply_followup_bubble_required"
        if reply.should_schedule_reply_wait:
            return None, "reply_followup_cannot_schedule_another_wait"
    return reply, None
