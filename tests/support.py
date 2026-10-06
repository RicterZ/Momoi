from types import SimpleNamespace
from typing import Any

from momoi.models import ProviderResponse, ToolCall
from momoi.observability.context import current_log_context
from momoi.runtime.tool_contracts.context import RECALL_TOOL_SPEC


def recall_response(units: int = 1) -> ProviderResponse:
    call = ToolCall(
        "submit-context",
        RECALL_TOOL_SPEC["name"],
        {
            "units": [
                {
                    "intent": "test owner intent",
                    "recall_mode": "search",
                    "recall_queries": [
                        {
                            "semantic": "Retrieve history for the test owner intent",
                            "keywords": ["test owner intent"],
                        }
                    ],
                    "recall_from_turn_id": "",
                }
                for _index in range(max(1, units))
            ]
        },
    )
    return ProviderResponse(
        [
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        ],
        [call],
    )


class ContextAwareProvider:
    """Answer the Owner Turn's opening context decision, then delegate.

    Owner Turns must submit a recall decision before acting, so a fake provider
    that goes straight to send_bubbles would spend its first rounds being
    refused. This keeps that protocol out of every individual test.
    """

    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.recalled_turns: set[str] = set()

    @property
    def config(self) -> object:
        return getattr(self.delegate, "config", SimpleNamespace(api_format="anthropic"))

    async def complete(
        self,
        system: object,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> ProviderResponse:
        names = {str(spec.get("name") or "") for spec in tools or []}
        context = current_log_context()
        turn_id = str(context.get("turn_id", ""))
        if (RECALL_TOOL_SPEC["name"] in names and context.get("stage") == "owner"
                and turn_id not in self.recalled_turns):
            self.recalled_turns.add(turn_id)
            return recall_response()
        return await self.delegate.complete(  # type: ignore[attr-defined,no-any-return]
            system, messages, tools, **kwargs
        )


def with_owner_recall(provider: object) -> ContextAwareProvider:
    return ContextAwareProvider(provider)


def provider_catalog(config):
    """Build the catalog used by runtime tests from explicit adapter options."""
    from dataclasses import asdict
    from pathlib import Path
    from momoi.integrations.configuration import ProviderCatalog, ProviderBinding

    options = asdict(config)
    adapter = options.pop("api_format")
    return ProviderCatalog(
        Path("providers.yaml"),
        {
            "llm": ProviderBinding("chat", adapter, True, options),
        },
    )


def write_app_config(path, value):
    """Write a main config and the default provider fixture for app-only tests."""
    import json

    path.write_text(json.dumps(value))
    catalog = path.parent / "providers.yaml"
    if not catalog.exists():
        catalog.write_text("""version: 1
services:
  chat:
    adapter: anthropic
    base_url: https://example.com
bindings:
  llm:
    service: chat
    options:
      api_key: key
      model: model
""")


def seed_memory(store, event, *, key, content, kind='preference', activation='recall', expires_at=None):
    """Insert an effective memory fixture, independent of foreground request tools."""
    import time
    now = time.time()
    with store._db:
        cursor = store._db.execute(
            """INSERT INTO memories(kind,key,content,activation,authority,source_event_id,
               evidence_quote,created_at,updated_at,expires_at)
               VALUES (?,?,?,?,'owner',?,?,?,?,?)""",
            (kind,key,content,activation,event.event_id,event.text,now,now,expires_at),
        )
        store._add_memory_evidence(cursor.lastrowid,event.event_id,event.text,now)
    return cursor.lastrowid


def reply_call(identifier, *, bubbles=None, text=None, mode="text", channel=None):
    """Script a Planner dispatch with separately generated speech for delivery tests."""
    import json
    content = bubbles if bubbles is not None else [text]
    speech = [value for value in content if isinstance(value, str) and not value.startswith("emotion://")]
    attachments = [value for value in content if value not in speech]
    arguments = {"intent": "测试回应", "reference": json.dumps(speech, ensure_ascii=False), "mode": mode}
    if channel:
        arguments["channel"] = channel
    if attachments:
        arguments["attachments"] = attachments
    return ToolCall(identifier, "reply", arguments)


def install_scripted_replyer(daemon):
    """Stub expression only; Planner, validation, delivery, and writeback stay real."""
    import json
    from unittest.mock import AsyncMock

    async def generate(call, request):
        return json.loads(call.arguments["reference"])

    daemon.tool_batch.replyer.generate = AsyncMock(side_effect=generate)
