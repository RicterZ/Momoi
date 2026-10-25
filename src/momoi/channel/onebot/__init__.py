from pathlib import Path
from dataclasses import replace

from .channel import OneBotChannel
from .config import OneBotConfig
from .parsing import image_blocks, incoming_segments, render_segments


def load_config(value: object, workspace: Path) -> OneBotConfig:
    legacy = workspace / "channel" / "napcat" / "files"
    current = workspace / "channel" / "onebot" / "files"
    if legacy.is_dir() and not current.exists():
        current.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(current)
    return replace(OneBotConfig.from_mapping(value), attachment_directory=current)


def create_channel(config: object) -> OneBotChannel:
    if not isinstance(config, OneBotConfig):
        raise ValueError("onebot requires OneBotConfig")
    return OneBotChannel(config)


__all__ = [
    "OneBotChannel",
    "OneBotConfig",
    "create_channel",
    "image_blocks",
    "incoming_segments",
    "load_config",
    "render_segments",
]
