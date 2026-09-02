from pathlib import Path

from .. import ChannelDependencies
from .channel import NapCatChannel
from .config import NapCatConfig
from .parsing import image_blocks, incoming_segments, render_segments


def load_config(value: object, _workspace: Path) -> NapCatConfig:
    return NapCatConfig.from_mapping(value)


def create_channel(
    config: object, dependencies: ChannelDependencies | None = None
) -> NapCatChannel:
    if not isinstance(config, NapCatConfig):
        raise ValueError("napcat requires NapCatConfig")
    dependencies = dependencies or ChannelDependencies()
    return NapCatChannel(
        config,
        dependencies.asr_provider,
        dependencies.asr_max_audio_bytes,
    )


__all__ = [
    "NapCatChannel",
    "NapCatConfig",
    "create_channel",
    "image_blocks",
    "incoming_segments",
    "load_config",
    "render_segments",
]
