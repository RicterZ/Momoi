"""Python business backend owned by the native desktop host.

The host owns stdin; a stop line or EOF requests graceful shutdown. Only the
ready message goes to stdout; diagnostics go to stderr. BGE uses the same FastEmbed implementation as the container.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
from contextlib import nullcontext
from pathlib import Path

from .workspace import prepare_workspace
from .embedding import load_encoder, create_app


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-model", action="store_true")
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--snapshot", type=Path)
    maintenance.add_argument("--restore", type=Path)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--dashboard-port", type=int)
    parser.add_argument("--embedding-port", type=int)
    return parser.parse_args()


async def serve(args):
    import httpx
    from uvicorn import Config, Server
    from ..cli.service import run
    from ..config.manager import ConfigurationManager
    from ..dashboard.auth import issue_dashboard_jwt

    for port in (args.dashboard_port, args.embedding_port):
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
    if args.dashboard_port == args.embedding_port:
        raise ValueError("dashboard and embedding ports must differ")
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
    encoder = await asyncio.to_thread(load_encoder, args.model_path.resolve())
    if stop.is_set():
        return
    prepare_workspace(workspace, f"http://127.0.0.1:{args.embedding_port}/v1/embeddings")

    class EmbeddedServer(Server):
        def capture_signals(self):
            return nullcontext()

    server = EmbeddedServer(Config(create_app(encoder), host="127.0.0.1", port=args.embedding_port, access_log=False, log_config=None))

    async def embedding_service():
        try:
            await server.serve()
            if not stop.is_set():
                raise RuntimeError("embedding service stopped unexpectedly")
        finally:
            stop.set()

    async def stop_embedding():
        await stop.wait()
        server.should_exit = True
    url = f"http://127.0.0.1:{args.dashboard_port}"

    async def ready():
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(300):
                if stop.is_set():
                    return
                try:
                    if not server.started:
                        await asyncio.sleep(0.1)
                        continue
                    response = await client.get(url + "/", timeout=1)
                    response.raise_for_status()
                    config = ConfigurationManager(workspace / "config.json").dashboard_config()
                    print(json.dumps({"event": "ready", "url": url, "token": issue_dashboard_jwt(config.dashboard.token)}), flush=True)
                    return
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
        raise TimeoutError("dashboard did not become ready")

    async with asyncio.TaskGroup() as group:
        group.create_task(embedding_service())
        group.create_task(stop_embedding())
        group.create_task(run(workspace / "config.json", dashboard_host="127.0.0.1", dashboard_port=args.dashboard_port, stop=stop, announce_token=False))
        group.create_task(ready())


def main():
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
    elif args.check_model:
        if args.model_path is None:
            raise ValueError("model-path is required")
        load_encoder(args.model_path.resolve())
        print(json.dumps({"ok": True, "dimensions": 512}))
    else:
        if args.workspace is None or args.model_path is None or args.dashboard_port is None or args.embedding_port is None:
            raise ValueError("workspace, dashboard-port and embedding-port are required")
        asyncio.run(serve(args))


if __name__ == "__main__":
    main()
