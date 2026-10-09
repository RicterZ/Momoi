"""Process-local MCP toolchains; never mutate the machine/user PATH."""
import os
from pathlib import Path


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
    env["MOMOI_MEDIA_CACHE"] = str(cache / "media")
    env["UV_PYTHON"] = str(runtime / "python/python.exe")
    env["PYTHONUTF8"] = "1"
    paths = [runtime / "node", runtime / "uv", directories["UV_TOOL_BIN_DIR"], directories["npm_config_prefix"]]
    env["PATH"] = os.pathsep.join(str(path) for path in paths) + os.pathsep + env.get("PATH", "")
    env["MOMOI_NODE"] = str(runtime / "node/node.exe")
