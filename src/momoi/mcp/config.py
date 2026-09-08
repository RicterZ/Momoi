import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..observability.events import log_event

logger = logging.getLogger(__name__)


def expand_mcp_value(value: str) -> str:
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
    return parse_mcp_servers(path.read_text(encoding="utf-8"))


def parse_mcp_servers(content: str) -> dict[str, dict[str, Any]]:
    """Validate a complete snapshot before changing any live connections."""
    raw = json.loads(content)
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("mcp.json must contain an mcpServers object")
    loaded = {}
    for name, config in servers.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("MCP server names must be non-empty strings")
        if not isinstance(config, dict):
            raise ValueError(f"MCP server {name} must be an object")
        if not isinstance(config.get("disabled", False), bool):
            raise ValueError(f"MCP server {name} disabled must be boolean")
        if config.get("disabled", False):
            continue
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
        command = config.get("command")
        url = config.get("url") or config.get("baseUrl")
        for key in ("command", "url", "baseUrl", "cwd"):
            if key in config and (
                not isinstance(config[key], str) or not config[key].strip()
            ):
                raise ValueError(f"MCP server {name} {key} must be a non-empty string")
        if not any(isinstance(value, str) and value.strip() for value in (command, url)):
            raise ValueError(f"MCP server {name} requires command or url")
        for key in ("args", "readOnlyTools", "enabled_tools", "enabledTools"):
            if key in config and (
                not isinstance(config[key], list)
                or not all(isinstance(value, str) for value in config[key])
            ):
                raise ValueError(f"MCP server {name} {key} must be an array of strings")
        for key in ("env", "headers"):
            if key in config and not isinstance(config[key], dict):
                raise ValueError(f"MCP server {name} {key} must be an object")
        for key in ("enabled_tools", "enabledTools", "readOnlyTools"):
            if any(not value.strip() for value in config.get(key, [])):
                raise ValueError(f"MCP server {name} {key} names must not be empty")
        loaded[name] = config
    log_event(
        logger,
        logging.INFO,
        "mcp_config_loaded",
        servers=len(loaded),
        names=",".join(sorted(loaded)) or None,
    )
    return loaded
