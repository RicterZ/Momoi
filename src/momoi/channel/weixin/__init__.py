from pathlib import Path


from .channel import WeixinChannel, render_segments
from .config import WeixinConfig, WeixinState


def load_config(value: object, workspace: Path) -> WeixinConfig:
    return WeixinConfig.from_mapping(value, workspace)


def create_channel(config: object) -> WeixinChannel:
    if not isinstance(config, WeixinConfig):
        raise ValueError("weixin requires WeixinConfig")
    return WeixinChannel(config)


__all__ = [
    "WeixinChannel",
    "WeixinConfig",
    "WeixinState",
    "create_channel",
    "load_config",
    "render_segments",
]
