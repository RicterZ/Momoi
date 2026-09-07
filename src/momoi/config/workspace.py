"""Workspace bootstrap and atomic persistence; never overwrite existing user files."""

import json
import os
import secrets
import tempfile
from pathlib import Path

import yaml


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def default_config() -> dict:
    return {
        "providers": "providers.yaml",
        "timezone": "UTC",
        "channels": {"primary": "", "enabled": {}},
        "context": {},
        "storage": {"database": "data/momoi.sqlite3"},
        "logging": {"level": "INFO"},
        "tools": {"mcp_config": None},
        "heartbeat": {"enabled": False},
        "reflection": {"enabled": False},
        "episode_annealing": {"enabled": False},
    }


def empty_providers() -> dict:
    return {"version": 1, "credentials": {}, "services": {}, "bindings": {}}


def _create(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    return True


def bootstrap(config_path: Path) -> bool:
    """Initialize a new workspace. Return whether a new dashboard secret was created."""
    if config_path.exists():
        return False
    config = default_config()
    config["dashboard"] = {"token": secrets.token_urlsafe(32)}
    root = config_path.parent
    _create(root / "providers.yaml", yaml.safe_dump(empty_providers(), sort_keys=False))
    _create(
        root / "prompts/SOUL.md",
        "你是 Momoi，是主人的个人助手。请用自然、清晰的语言交流。\n",
    )
    _create(root / "prompts/HEARTBEAT.md", "")
    return _create(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
