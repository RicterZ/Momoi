import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..logging_context import log_event

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
