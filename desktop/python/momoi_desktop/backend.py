"""Python business backend owned by the native desktop host.

The host owns stdin; a stop line or EOF requests graceful shutdown. Only the
startup messages go to stdout; diagnostics go to stderr. BGE shares the
in-process encoder used by Linux.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from .workspace import prepare_workspace


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-dir", type=Path)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--snapshot", type=Path)
    maintenance.add_argument("--restore", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--dashboard-port", type=int)
    return parser.parse_args()


async def serve(args):
    from momoi.__main__ import run
    from momoi.config.manager import ConfigurationManager
    from momoi.dashboard.auth import issue_dashboard_jwt
    from momoi.integrations import media
    from .media_runtime import ffmpeg_executable

    media.ffmpeg_executable = ffmpeg_executable

    if not 1 <= args.dashboard_port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    workspace = args.workspace.resolve()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def watch_parent():
        sys.stdin.readline()
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            pass  # Event loop has already exited.

    threading.Thread(target=watch_parent, daemon=True).start()
    if args.install_dir is not None:
        os.environ["MOMOI_EMBEDDING_MODEL_PATH"] = str(args.install_dir.resolve() / "models" / "bge-small-zh-v1.5")
    os.environ["MOMOI_DESKTOP_EMBEDDING"] = "1"
    print(json.dumps({"event": "startup_progress", "stage": "workspace"}), flush=True)
    prepare_workspace(workspace)
    from .emotions import seed_emotions

    seed_emotions(workspace, ConfigurationManager(workspace / "config.json").dashboard_config().database)
    if args.install_dir is not None:
        from .mcp_runtime import prepare_mcp_environment
        prepare_mcp_environment(args.install_dir.resolve(), workspace)
        from .media_runtime import prepare_configured_media
        await asyncio.to_thread(prepare_configured_media, workspace / "config.json")

    url = f"http://127.0.0.1:{args.dashboard_port}"

    dashboard_ready = asyncio.Event()

    async def announce_ready():
        async with asyncio.timeout(30):
            await dashboard_ready.wait()
        if stop.is_set():
            return
        config = ConfigurationManager(workspace / "config.json").dashboard_config()
        print(json.dumps({"event": "ready", "url": url, "token": issue_dashboard_jwt(config.dashboard.token)}), flush=True)

    print(json.dumps({"event": "startup_progress", "stage": "services"}), flush=True)
    async with asyncio.TaskGroup() as group:
        group.create_task(run(workspace / "config.json", dashboard_host="127.0.0.1", dashboard_port=args.dashboard_port, stop=stop, announce_token=False, dashboard_ready=dashboard_ready))
        group.create_task(announce_ready())


def main():
    if sys.platform == "win32":
        from .proxy import prepare_proxy_environment
        prepare_proxy_environment()
    os.environ["NO_COLOR"] = "1"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    args = parse_args()
    if args.snapshot:
        from .snapshot import snapshot
        if args.workspace is None:
            raise ValueError("workspace is required for snapshot")
        snapshot(args.workspace.resolve(), args.snapshot.resolve())
    elif args.restore:
        from .snapshot import restore
        restore(args.restore.resolve())
    else:
        if args.workspace is None or args.dashboard_port is None:
            raise ValueError("workspace and dashboard-port are required")
        asyncio.run(serve(args))


if __name__ == "__main__":
    main()
