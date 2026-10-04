"""Update rollback snapshot taken only after the backend has stopped."""
import json
import shutil
import sqlite3
from pathlib import Path

from ..config.manager import ConfigurationManager


def snapshot(workspace: Path, destination: Path) -> None:
    manager = ConfigurationManager(workspace / "config.json")
    config = manager.dashboard_config()
    destination.mkdir(parents=True, exist_ok=False)
    files = [manager.path, manager.provider_path, workspace / ".desktop-embedding.yaml"]
    metadata = {"files": [], "database": {"path": str(config.database), "exists": config.database.exists()}}
    for index, source in enumerate(files):
        name = f"file-{index}"
        exists = source.exists()
        metadata["files"].append({"path": str(source.resolve()), "name": name, "exists": exists})
        if exists:
            shutil.copy2(source, destination / name)
    if config.database.exists():
        with sqlite3.connect(config.database) as source, sqlite3.connect(destination / "database.sqlite3") as target:
            source.backup(target)
    (destination / "snapshot.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def restore(destination: Path) -> None:
    metadata = json.loads((destination / "snapshot.json").read_text(encoding="utf-8"))
    for item in metadata["files"]:
        target = Path(item["path"])
        if item["exists"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination / item["name"], target)
        else:
            target.unlink(missing_ok=True)
    database = Path(metadata["database"]["path"])
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    if metadata["database"]["exists"]:
        database.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination / "database.sqlite3", database)
    else:
        database.unlink(missing_ok=True)
