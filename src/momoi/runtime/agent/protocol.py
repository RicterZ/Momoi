import copy
from collections.abc import Callable
from typing import Any

from ...channel import ChannelMessage
from ...models import AgentReply, ToolCall
from ..parsing import parse_response
from ..turn_support import tool_error_block

OWNER_BUBBLE_REQUEST_REMINDER = (
    "Native tool calls only: if bubbles are warranted, call send_bubbles with "
    "them; otherwise call the next work or terminal tool."
)


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
    calls: list[ToolCall], error: str, *, require_response: bool
) -> list[dict[str, Any]]:
    correction: list[dict[str, Any]] = [
        tool_error_block(call.id, error) for call in calls
    ]
    if error != "assistant_text_forbidden":
        return correction
    if require_response and len(calls) == 1 and calls[0].name == "end_turn":
        text = (
            "[Trusted runtime protocol correction: plain assistant text is not "
            "delivered. Call send_bubbles with the owner-visible bubbles, without "
            "end_turn. After its result, call end_turn alone on the next step.]"
        )
    else:
        text = (
            "[Trusted runtime protocol correction: assistant text is forbidden. "
            "Repeat the intended action using native tool calls only.]"
        )
    correction.append({"type": "text", "text": text})
    return correction


def parse_end_turn(
    arguments: dict[str, Any],
    *,
    heartbeat_turn: bool,
    owner_turn: bool,
    reply_followup_turn: bool,
    visible_since_owner_update: bool,
    heartbeat_min_interval_seconds: int,
    heartbeat_max_interval_seconds: int,
    validate_emotions: Callable[[list[ChannelMessage]], str | None],
) -> tuple[AgentReply | None, str | None]:
    reply, error = parse_response(
        arguments,
        require_heartbeat=heartbeat_turn,
        allow_activity_update=owner_turn,
    )
    if reply is not None and heartbeat_turn and reply.heartbeat:
        seconds = int(reply.heartbeat["next_check_minutes"]) * 60
        if not (
            heartbeat_min_interval_seconds
            <= seconds
            <= heartbeat_max_interval_seconds
        ):
            reply = None
            error = "heartbeat_interval_out_of_range"
    if reply is not None:
        error = validate_emotions(reply.messages)
        if error is not None:
            reply = None
    if (
        reply is not None
        and reply.expects_reply
        and not reply.messages
        and not visible_since_owner_update
    ):
        reply = None
        error = "reply_expectation_without_visible_bubble"
    if reply is not None and reply_followup_turn and not visible_since_owner_update:
        reply = None
        error = "reply_followup_bubble_required"
    if reply is not None and reply_followup_turn and reply.should_schedule_reply_wait:
        reply = None
        error = "reply_followup_cannot_schedule_another_wait"
    return reply, error
