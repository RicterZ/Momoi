"""Shared request lifecycle for HTTP LLM protocol adapters.

Each wire protocol supplies its endpoint, headers, payload and response
parsing; transport, retries, dumps and observability are shared here.
"""

import dataclasses
import logging
from pathlib import Path
from time import time
from typing import Any, Callable

import aiohttp

from ..media import text_only_messages
from ...integrations.models import LLMConfig
from ...llm.dumps import dump_request
from ...llm.errors import ProviderResponseError
from ...llm.request_metrics import RequestMetric, response_headers
from ...llm.streaming import StreamingProvider, response_data, stream_payload
from ...llm.telemetry import (
    compact_response_text,
    log_tool_schema,
    persist_thinking,
    record_response,
    thinking_effort,
)
from ...llm.transport import ProviderCircuit, http_error, retry_request
from ...models import ProviderResponse
from ...observability.events import log_event

logger = logging.getLogger(__name__)


class ProtocolProvider(StreamingProvider):
    protocol: str
    label: str

    def __init__(self, config: LLMConfig, dump_dir: Path | None = None) -> None:
        self.config = config
        self._circuit = ProviderCircuit()
        self.dump_dir = dump_dir
        self.request_metrics_sink = None
        self.usage_sink: Callable[..., None] | None = None
        self.usage_parser: Callable[[dict], dict | None] | None = None
        self.thinking_sink: Callable[..., None] | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.config.api_key}", "content-type": "application/json"}

    def _payload(
        self, system: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
        *, require_tool: bool, required_tool: str | None, effort: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, data: dict[str, Any], response: aiohttp.ClientResponse) -> ProviderResponse:
        raise NotImplementedError

    def _thinking(self, parsed: ProviderResponse) -> str:
        return parsed.reasoning

    def _check_content(self, parsed: ProviderResponse) -> None:
        """Reject unusable output after its reasoning has been recorded."""

    async def _response_json(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            data = await response_data(response, self.protocol)
        except (aiohttp.ClientError, ValueError) as error:
            raise ProviderResponseError(f"{self.label} endpoint returned invalid JSON") from error
        if not isinstance(data, dict):
            raise ProviderResponseError(f"{self.label} endpoint returned non-object JSON")
        return data

    async def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        require_tool: bool = False,
        required_tool: str | None = None,
    ) -> ProviderResponse:
        if self._session is None:
            raise RuntimeError("provider is not started")
        config = self.config
        if not config.multimodal:
            messages = text_only_messages(messages)
        effort = thinking_effort(config)
        payload = self._payload(
            system, messages, tools, require_tool=require_tool, required_tool=required_tool, effort=effort,
        )
        log_tool_schema(self.protocol, payload.get("tools"))
        stream_payload(payload, self.protocol)
        dump_path = dump_request(self.dump_dir, self.protocol, payload, require_tool)
        headers = self._headers()

        async def request(attempt_started: float) -> ProviderResponse:
            billed_at = time()
            assert self._session is not None
            async with self._session.post(self._url(), json=payload, headers=headers) as response:
                response_headers(response.status)
                if response.status != 200:
                    raise await http_error(response, self.label)
                data = await self._response_json(response)
                duration_ms, metrics = record_response(
                    data,
                    protocol=self.protocol,
                    attempt_started=attempt_started,
                    dump_path=dump_path,
                    usage_sink=self.usage_sink,
                    model=config.model,
                    parse_usage=self.usage_parser,
                    created_at=billed_at,
                )
                parsed = self._parse(data, response)
                text = "\n".join(
                    block["text"] for block in parsed.content
                    if block.get("type") == "text" and isinstance(block.get("text"), str)
                )
                calls = parsed.tool_calls
                persist_thinking(
                    self.thinking_sink,
                    reasoning=self._thinking(parsed),
                    assistant_text=text,
                    tools=[call.name for call in calls],
                    tool_calls=[{"id": call.id, "name": call.name, "arguments": call.arguments,
                                 **({"argument_error": call.argument_error} if call.argument_error else {})}
                                for call in calls],
                    dump_file=dump_path.name if dump_path else "",
                    protocol=self.protocol,
                    model=config.model,
                )
                self._check_content(parsed)
                if calls:
                    log_event(logger, logging.DEBUG, "llm_response", protocol=self.protocol,
                              response_kind="tools", tool_count=len(calls),
                              tool_names=",".join(call.name for call in calls), duration_ms=duration_ms)
                else:
                    log_event(logger, logging.DEBUG, "llm_response", protocol=self.protocol,
                              response_kind="text", text=compact_response_text(text), duration_ms=duration_ms)
                return dataclasses.replace(parsed, usage=metrics)

        return await retry_request(
            protocol=self.protocol,
            circuit=self._circuit,
            max_retries=config.max_retries,
            request_fields={
                "model": config.model,
                "messages": len(messages),
                "tools": len(tools or []),
                "require_tool": require_tool,
                "tool_choice": payload.get("tool_choice"),
                "thinking_effort": effort or None,
            },
            operation=request,
            monitor=RequestMetric(self.request_metrics_sink, payload=payload,
                                  protocol=self.protocol, endpoint=config.base_url),
        )
