"""Exercise private Python, startup, authentication, BGE and graceful cleanup."""
import argparse
import json
import os
import socket
import subprocess
import tempfile
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
        dashboard, embedding = port(), port()
        while embedding == dashboard:
            embedding = port()
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([
                str(args.python.absolute()), "-I", "-B", "-X", "utf8", str(args.entry.resolve()), "--workspace", directory,
                "--model-path", str(args.model_path.resolve()),
                "--dashboard-port", str(dashboard), "--embedding-port", str(embedding),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONUTF8": "1"})
            lines = Queue()
            def capture():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)
            Thread(target=capture, daemon=True).start()
            try:
                line = lines.get(timeout=180)
                if line is None:
                    raise RuntimeError("backend exited before ready")
                ready = json.loads(line)
                assert ready["event"] == "ready"
                with httpx.Client(trust_env=False) as client:
                    response = client.get(ready["url"] + "/")
                    response.raise_for_status()
                    response = client.get(ready["url"] + "/api/settings", headers={"Authorization": "Bearer " + ready["token"]})
                    response.raise_for_status()
                    response = client.post(f"http://127.0.0.1:{embedding}/v1/embeddings", json={"model": "BAAI/bge-small-zh-v1.5", "input": ["中文记忆", "English memory"]}, timeout=30)
                    response.raise_for_status()
                    assert len(response.json()["data"]) == 2
                    assert all(len(item["embedding"]) == 512 for item in response.json()["data"])
                process.stdin.write("stop\n")
                process.stdin.flush()
                assert process.wait(timeout=30) == 0
                for value in (dashboard, embedding):
                    with socket.socket() as connection:
                        assert connection.connect_ex(("127.0.0.1", value)) != 0, "service survived shutdown"
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
