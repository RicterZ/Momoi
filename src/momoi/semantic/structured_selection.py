"""Bounded protocol repair for semantic selection; data validation stays strict."""
import asyncio

from ..integrations.request_context import model_request


class SelectionProtocolError(ValueError):
    """The response cannot be mapped to the supplied structured candidates."""


async def select_structured(provider, system, messages, spec, parse, *, timeout):
    async def run():
        conversation = list(messages)
        for attempt in range(2):
            with model_request(thinking_effort="low"):
                response = await provider.complete(
                    system, conversation, [spec], required_tool=spec["name"],
                )
            try:
                if len(response.tool_calls) != 1 or response.tool_calls[0].name != spec["name"]:
                    raise SelectionProtocolError("exactly one selection tool response required")
                return parse(response.tool_calls[0].arguments), attempt + 1
            except SelectionProtocolError as error:
                if attempt:
                    raise
                conversation.append({"role": "user", "content": (
                    "Runtime protocol correction: " + str(error) + ". "
                    "Submit the complete result using only " + spec["name"] +
                    ". Copy local references from the supplied data; do not return plain text."
                )})
        raise AssertionError("unreachable selection state")
    return await asyncio.wait_for(run(), timeout=timeout)
