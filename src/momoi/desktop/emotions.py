"""Seed desktop emotion assets once; preserve edits and intentional deletions."""
import json
from importlib.resources import files
from pathlib import Path

from ..config.workspace import atomic_write
from ..storage.delivery.emotions import managed_emotion_bytes
from ..storage.store import Store

MARKER = ".desktop-emotions-v1.json"


def seed_emotions(workspace: Path, database: Path) -> None:
    marker = workspace / MARKER
    if marker.exists():
        return
    resources = files("momoi.desktop").joinpath("default_emotions")
    catalog = json.loads(resources.joinpath("catalog.json").read_text(encoding="utf-8"))
    database.parent.mkdir(parents=True, exist_ok=True)
    store = Store(database, workspace=workspace)
    try:
        for item in catalog:
            if store.emotion(item["slug"]) is not None:
                continue
            asset = managed_emotion_bytes(workspace, resources.joinpath(item["file"]).read_bytes(), item["file"])
            store.add_emotion(item["slug"], asset, item["description"])
        atomic_write(marker, json.dumps({"version": 1, "slugs": [item["slug"] for item in catalog]}) + "\n")
    finally:
        store.close()
