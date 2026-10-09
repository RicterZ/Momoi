"""Exercise private Python, startup, authentication, BGE and graceful cleanup."""
import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Thread
from queue import Queue

import httpx


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="Momoi 桃井 ") as directory:
        dashboard = port()
        check = subprocess.run([
            str(args.python.absolute()), "-I", "-B", "-X", "utf8", "-c", """
import asyncio
import json
import runpy
import sys
runpy.run_path(sys.argv[1])
from momoi.integrations.adapters.local_embedding import local_embedding
vectors = asyncio.run(local_embedding(sys.argv[2]).encode(["中文记忆", "English memory"], query=True))
assert len(vectors) == 2 and all(len(vector) == 512 for vector in vectors)
print(json.dumps({"ok": True, "dimensions": 512, "vectors": len(vectors)}))
""", str(args.entry.resolve()), str(args.model_path.resolve()),
        ], capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
        assert json.loads(check.stdout) == {"ok": True, "dimensions": 512, "vectors": 2}
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([
                str(args.python.absolute()), "-I", "-B", "-X", "utf8", str(args.entry.resolve()), "--workspace", directory,
                "--dashboard-port", str(dashboard),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONUTF8": "1", "MOMOI_EMBEDDING_MODEL_PATH": str(args.model_path.resolve())})
            lines = Queue()
            def capture():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)
            Thread(target=capture, daemon=True).start()
            try:
                deadline = time.monotonic() + 180
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("backend did not become ready within 180 seconds")
                    line = lines.get(timeout=remaining)
                    if line is None:
                        raise RuntimeError("backend exited before ready")
                    ready = json.loads(line)
                    if ready.get("event") == "startup_progress":
                        continue
                    assert ready["event"] == "ready", "Unexpected backend stdout event"
                    break
                with httpx.Client(trust_env=False) as client:
                    response = client.get(ready["url"] + "/")
                    response.raise_for_status()
                    response = client.get(ready["url"] + "/api/settings", headers={"Authorization": "Bearer " + ready["token"]})
                    response.raise_for_status()
                    response = client.get(ready["url"] + "/api/emotions", headers={"Authorization": "Bearer " + ready["token"]})
                    response.raise_for_status()
                    emotions = response.json()["items"]
                    assert {item["slug"] for item in emotions} == {"cry", "normal", "happy", "smug", "confused", "stunned"}
                    for item in emotions:
                        asset = client.get(ready["url"] + item["asset_url"], headers={"Authorization": "Bearer " + ready["token"]})
                        asset.raise_for_status()
                        assert asset.content.startswith(b"\x89PNG\r\n\x1a\n")
                process.stdin.write("stop\n")
                process.stdin.flush()
                assert process.wait(timeout=30) == 0
                with socket.socket() as connection:
                    assert connection.connect_ex(("127.0.0.1", dashboard)) != 0, "service survived shutdown"
            except BaseException:
                errors.seek(0)
                print(errors.read().decode("utf-8", errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    print("Private backend smoke passed: Unicode workspace, authenticated dashboard, BGE, shutdown")


if __name__ == "__main__":
    main()
