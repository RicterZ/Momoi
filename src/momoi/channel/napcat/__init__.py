from pathlib import Path
from dataclasses import replace

from .channel import NapCatChannel
from .config import NapCatConfig
from .parsing import image_blocks, incoming_segments, render_segments


def load_config(value: object, _workspace: Path) -> NapCatConfig:
    return replace(NapCatConfig.from_mapping(value),
                   attachment_directory=_workspace / "channel" / "napcat" / "files")


def create_channel(config: object) -> NapCatChannel:
    if not isinstance(config, NapCatConfig):
        raise ValueError("napcat requires NapCatConfig")
    return NapCatChannel(config)


__all__ = [
    "NapCatChannel",
    "NapCatConfig",
    "create_channel",
    "image_blocks",
    "incoming_segments",
    "load_config",
    "render_segments",
]
