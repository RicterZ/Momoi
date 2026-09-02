import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD
from .logging_context import (
    TRACE,
    captured_log_context,
    current_log_context,
    log_event,
    safe_preview,
)

logger = logging.getLogger(__name__)
MCPRequest = tuple[
    str,
    dict[str, Any],
    asyncio.Future[dict[str, Any]],
    dict[str, Any],
]

MCP_TOOL_POLICY = """### External MCP tools

- MCP tools are real capabilities. Use them when the owner's request requires
  current external information or an action they provide; do not pretend to
  have searched or acted without a successful tool result.
- For non-trivial research, retry with a better query when the first result is
  insufficient, then answer from the evidence actually returned.
"""


def _mcp_error_message(payload: dict[str, Any]) -> str:
    structured = payload.get("structuredContent")
    if isinstance(structured, dict):
        for key in ("message", "error", "detail"):
            if structured.get(key):
                return safe_preview(structured[key], 500)
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return safe_preview(item["text"], 500)
    return "The MCP server reported a tool error."


def _expand(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"environment variable {name} is not set")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def load_mcp_servers(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    if not path.exists():
        log_event(
            logger,
            logging.ERROR,
            "mcp_config_missing",
            path=str(path),
        )
        raise FileNotFoundError(f"MCP configuration file does not exist: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("mcp.json must contain an mcpServers object")
    loaded = {
        str(name): config
        for name, config in servers.items()
        if isinstance(config, dict) and not config.get("disabled", False)
    }
    for name, config in loaded.items():
        optional = config.get("optional", False)
        if not isinstance(optional, bool):
            raise ValueError(f"MCP server {name} optional must be boolean")
        description = config.get("description")
        if description is not None and (
            not isinstance(description, str)
            or not description.strip()
            or len(description.strip()) > 500
        ):
            raise ValueError(
                f"MCP server {name} description must be 1 to 500 characters"
            )
    log_event(
        logger,
        logging.INFO,
        "mcp_config_loaded",
        path=str(path),
        servers=len(loaded),
        names=",".join(sorted(loaded)) or None,
    )
    return loaded


class MCPManager:
    def __init__(self, path: Path | None) -> None:
        self.configs = load_mcp_servers(path)
        self.tool_specs: list[dict[str, Any]] = []
        self._tools: dict[str, tuple[str, str]] = {}
        self._capabilities: dict[str, str] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._queues: dict[str, asyncio.Queue[MCPRequest]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    async def __aenter__(self) -> "MCPManager":
        self._closing = False
        loop = asyncio.get_running_loop()
        for name, config in self.configs.items():
            queue: asyncio.Queue[MCPRequest] = asyncio.Queue()
            ready: asyncio.Future[None] = loop.create_future()
            self._queues[name] = queue
            self._workers[name] = asyncio.create_task(
                self._serve(name, config, queue, ready),
                name=f"momoi-mcp-{name}",
            )
            try:
                await ready
            except Exception as error:
                log_event(
                    logger,
                    logging.ERROR,
                    "mcp_connect_failure",
                    server=name,
                    error_type=type(error).__name__,
                    optional=bool(config.get("optional", False)),
                )
                if not config.get("optional", False):
                    await self.__aexit__()
                    raise RuntimeError(
                        f"required MCP server failed to connect: {name}"
                    ) from error
        return self

    async def __aexit__(self, *_: object) -> None:
        self._closing = True
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        results = await asyncio.gather(*workers, return_exceptions=True)
        for name, result in zip(self._workers, results, strict=False):
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                log_event(
                    logger,
                    logging.WARNING,
                    "mcp_worker_failure",
                    server=name,
                    error_type=type(result).__name__,
                )
        self._workers.clear()
        self._queues.clear()
        if not workers:
            for name in list(self._stacks):
                await self._disconnect(name)

    async def _serve(
        self,
        name: str,
        config: dict[str, Any],
        queue: asyncio.Queue[MCPRequest],
        ready: asyncio.Future[None],
    ) -> None:
        try:
            try:
                await self._connect(name, config)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as error:
                if not ready.done():
                    ready.set_exception(RuntimeError(type(error).__name__))
                return
            if not ready.done():
                ready.set_result(None)
            connected = True
            while True:
                try:
                    request = await queue.get()
                except asyncio.CancelledError:
                    if self._closing:
                        raise
                    log_event(
                        logger,
                        logging.WARNING,
                        "mcp_connection_interrupted",
                        server=name,
                    )
                    await self._disconnect(name)
                    connected = False
                    continue
                tool, arguments, future, log_snapshot = request
                started = monotonic()
                if future.cancelled():
                    continue
                if not connected:
                    connected = await self._try_connect(name, config)
                    if not connected:
                        if not future.done():
                            future.set_result(
                                {
                                    "ok": False,
                                    "error": "mcp_unavailable",
                                    "message": (
                                        "The MCP server is unavailable after a "
                                        "reconnect attempt."
                                    ),
                                    "ambiguous": True,
                                    "connection_recovered": False,
                                }
                            )
                        with captured_log_context(log_snapshot):
                            log_event(
                                logger,
                                logging.WARNING,
                                "mcp_call_end",
                                server=name,
                                tool_name=self._wire_name(name, tool),
                                ok=False,
                                error_type="mcp_unavailable",
                                duration_ms=int((monotonic() - started) * 1000),
                            )
                        continue
                with captured_log_context(log_snapshot):
                    try:
                        result = await self._invoke(name, tool, arguments)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except BaseException as error:
                        if self._closing:
                            raise
                        log_event(
                            logger,
                            logging.WARNING,
                            "mcp_call_failure",
                            server=name,
                            tool_name=self._wire_name(name, tool),
                            error_type=type(error).__name__,
                        )
                        await self._disconnect(name)
                        connected = await self._try_connect(name, config)
                        result = {
                            "ok": False,
                            "error": "mcp_transport_error",
                            "message": (
                                safe_preview(str(error), 500)
                                or type(error).__name__
                            ),
                            "upstream_error_type": type(error).__name__,
                            "ambiguous": True,
                            "connection_recovered": connected,
                        }
                    if not future.done():
                        future.set_result(result)
                    log_event(
                        logger,
                        TRACE,
                        "mcp_call_end",
                        server=name,
                        tool_name=self._wire_name(name, tool),
                        ok=bool(result["ok"]),
                        duration_ms=int((monotonic() - started) * 1000),
                    )
        finally:
            await self._disconnect(name)

    async def _try_connect(self, name: str, config: dict[str, Any]) -> bool:
        try:
            await self._connect(name, config)
            return True
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            log_event(
                logger,
                logging.WARNING,
                "mcp_reconnect_failure",
                server=name,
                error_type=type(error).__name__,
            )
            return False

    async def _disconnect(self, name: str) -> None:
        self._sessions.pop(name, None)
        stack = self._stacks.pop(name, None)
        if stack is None:
            return
        await self._close_stack(name, stack, "disconnect")

    @staticmethod
    async def _close_stack(name: str, stack: AsyncExitStack, phase: str) -> None:
        try:
            await stack.aclose()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            log_event(
                logger,
                logging.WARNING,
                "mcp_cleanup_failure",
                server=name,
                phase=phase,
                error_type=type(error).__name__,
            )

    async def _invoke(
        self, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._sessions[server].call_tool(tool, arguments)
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        is_error = bool(result.isError)
        message = _mcp_error_message(payload) if is_error else None
        response = {
            "ok": not is_error,
            "error": "mcp_tool_error" if is_error else None,
            "truncated": bool(payload.get("truncated", False)),
            "result": payload,
        }
        if message is not None:
            response["message"] = message
        return response

    async def _connect(self, name: str, config: dict[str, Any]) -> None:
        stack = AsyncExitStack()
        try:
            configured_read_only = config.get("readOnlyTools", [])
            if not isinstance(configured_read_only, list) or not all(
                isinstance(item, str) for item in configured_read_only
            ):
                raise ValueError("readOnlyTools must be an array of tool names")
            read_only_tools = set(configured_read_only)
            if command := config.get("command"):
                env = {
                    **os.environ,
                    **{
                        str(key): _expand(str(value))
                        for key, value in (config.get("env") or {}).items()
                    },
                }
                transport = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=str(command),
                            args=[str(item) for item in config.get("args", [])],
                            env=env,
                            cwd=config.get("cwd"),
                        )
                    )
                )
                read, write = transport
            elif url := config.get("url") or config.get("baseUrl"):
                headers = {
                    str(key): _expand(str(value))
                    for key, value in (config.get("headers") or {}).items()
                }
                client = await stack.enter_async_context(
                    httpx.AsyncClient(headers=headers, timeout=60)
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(_expand(str(url)), http_client=client)
                )
            else:
                raise ValueError("server requires command or url")

            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60))
            )
            await session.initialize()
            tools = []
            cursor = None
            while True:
                page = await session.list_tools(cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            configured_tools = config.get(
                "enabled_tools",
                config.get("enabledTools", ["*"]),
            )
            if not isinstance(configured_tools, list) or not all(
                isinstance(item, str) and item
                for item in configured_tools
            ):
                raise ValueError(
                    f"MCP server {name} enabled_tools must be an array of names"
                )
            enabled_tools = set(configured_tools)
            allow_all_tools = "*" in enabled_tools
            matched_enabled_tools: set[str] = set()
            available_raw_names = sorted(str(tool.name) for tool in tools)
            available_wire_names = sorted(
                self._wire_name(name, str(tool.name)) for tool in tools
            )
            discovered: dict[str, tuple[str, dict[str, Any]]] = {}
            discovered_capabilities: dict[str, str] = {}
            for tool in sorted(tools, key=lambda item: str(item.name)):
                wire_name = self._wire_name(name, tool.name)
                if (
                    not allow_all_tools
                    and tool.name not in enabled_tools
                    and wire_name not in enabled_tools
                ):
                    continue
                if tool.name in enabled_tools:
                    matched_enabled_tools.add(tool.name)
                if wire_name in enabled_tools:
                    matched_enabled_tools.add(wire_name)
                existing = self._tools.get(wire_name)
                if wire_name in discovered or (existing and existing[0] != name):
                    raise ValueError(f"duplicate MCP tool name: {wire_name}")
                discovered[wire_name] = (
                    tool.name,
                    {
                        "name": wire_name,
                        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
                        "description": f"[MCP:{name}] {tool.description or tool.name}",
                        "input_schema": tool.inputSchema,
                    },
                )
                annotations = getattr(tool, "annotations", None)
                values = (
                    annotations.model_dump(mode="json", by_alias=True)
                    if annotations is not None
                    else {}
                )
                discovered_capabilities[wire_name] = (
                    "read"
                    if values.get("readOnlyHint") or tool.name in read_only_tools
                    else "external_effect"
                )
            if enabled_tools and not allow_all_tools:
                unmatched = sorted(enabled_tools - matched_enabled_tools)
                if unmatched:
                    log_event(
                        logger,
                        logging.WARNING,
                        "mcp_enabled_tools_unmatched",
                        server=name,
                        unmatched=unmatched,
                        available_raw_names=available_raw_names,
                        available_wire_names=available_wire_names,
                    )
            old_names = {
                wire_name
                for wire_name, target in self._tools.items()
                if target[0] == name
            }
            self.tool_specs = [
                spec for spec in self.tool_specs if spec["name"] not in old_names
            ]
            for wire_name in old_names:
                self._tools.pop(wire_name, None)
                self._capabilities.pop(wire_name, None)
            for wire_name, (tool_name, spec) in discovered.items():
                self._tools[wire_name] = (name, tool_name)
                self._capabilities[wire_name] = discovered_capabilities[wire_name]
                self.tool_specs.append(spec)
            self.tool_specs.sort(key=lambda item: str(item.get("name") or ""))
            old_stack = self._stacks.get(name)
            self._sessions[name] = session
            self._stacks[name] = stack
            if old_stack is not None:
                await self._close_stack(name, old_stack, "replace")
            log_event(
                logger,
                logging.INFO,
                "mcp_connected",
                server=name,
                tools=len(discovered),
                discovered_tools=len(tools),
            )
        except BaseException:
            await self._close_stack(name, stack, "failed_connect")
            raise

    def _wire_name(self, server: str, tool: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]
        if not base:
            raise ValueError("invalid MCP tool name")
        return base

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def capability(self, name: str) -> str:
        return self._capabilities.get(name, "external_effect")

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = self._tools.get(name)
        if target is None:
            return {"ok": False, "error": "tool_not_allowed"}
        server, tool = target
        queue = self._queues.get(server)
        worker = self._workers.get(server)
        if queue is None or worker is None or worker.done():
            return {"ok": False, "error": "mcp_unavailable"}
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        await queue.put((tool, arguments, future, current_log_context()))
        return await asyncio.shield(future)
