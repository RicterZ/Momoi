"""Download a fixed BGE snapshot and verify actual offline inference."""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from huggingface_hub import snapshot_download

from momoi.integrations.adapters.local_embedding import MODEL, MODEL_REPOSITORY, MODEL_REVISION, load_encoder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "models" / "bge-small-zh-v1.5")
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    snapshot = Path(snapshot_download(
        MODEL_REPOSITORY,
        revision=MODEL_REVISION,
        allow_patterns=["*.json", "*.onnx", "*.txt", "README.md"],
    ))
    for path in snapshot.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    encoder = load_encoder(target)
    vectors = list(encoder.embed(["中文记忆检索", "Momoi offline embedding", "中文与 English 混合：你好！"]))
    assert len(vectors) == 3 and all(len(vector) == 512 for vector in vectors)
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    manifest = {
        "model": MODEL, "repository": MODEL_REPOSITORY, "revision": MODEL_REVISION,
        "dimensions": 512,
        "files": {
            path.name: digest(path)
            for path in sorted(target.iterdir()) if path.is_file() and path.name != "manifest.json"
        },
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Verified offline BGE: {target}")


if __name__ == "__main__":
    main()
