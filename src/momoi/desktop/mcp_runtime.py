"""Process-local MCP toolchains; never mutate the machine/user PATH."""
import json
import os
from pathlib import Path

from ..config.workspace import atomic_write


def prepare_mcp_environment(install: Path, workspace: Path, environ=None) -> None:
    env = os.environ if environ is None else environ
    runtime = install / "runtime"
    cache = workspace / "tool-cache"
    directories = {
        "UV_CACHE_DIR": cache / "uv",
        "UV_TOOL_DIR": cache / "uv-tools",
        "UV_TOOL_BIN_DIR": cache / "bin",
        "UV_PYTHON_INSTALL_DIR": cache / "python",
        "npm_config_cache": cache / "npm",
        "npm_config_prefix": workspace / "node-global",
    }
    for name, directory in directories.items():
        directory.mkdir(parents=True, exist_ok=True)
        env[name] = str(directory)
    env["UV_PYTHON"] = str(runtime / "python/python.exe")
    env["PYTHONUTF8"] = "1"
    paths = [runtime / "node", runtime / "uv", directories["UV_TOOL_BIN_DIR"], directories["npm_config_prefix"]]
    env["PATH"] = os.pathsep.join(str(path) for path in paths) + os.pathsep + env.get("PATH", "")
    env["MOMOI_NODE"] = str(runtime / "node/node.exe")
    env["MOMOI_BRAVE_MCP"] = str(runtime / "mcp/node_modules/@brave/brave-search-mcp-server/dist/index.js")


def seed_brave_config(workspace: Path) -> None:
    path = workspace / "mcp.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw["mcpServers"]
    if "brave-search" in servers:
        return
    servers["brave-search"] = {
        "command": "${MOMOI_NODE}",
        "args": ["${MOMOI_BRAVE_MCP}", "--transport", "stdio"],
        "disabled": True,
        "optional": True,
        "description": "Brave 官方搜索工具；填写 BRAVE_API_KEY 后启用。",
        "env": {"BRAVE_API_KEY": ""},
        "enabled_tools": ["brave_web_search", "brave_local_search"],
        "readOnlyTools": ["brave_web_search", "brave_local_search"],
    }
    atomic_write(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
