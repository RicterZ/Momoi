from pathlib import Path

from .channel import WechatyChannel
from .config import WechatyConfig


def load_config(value: object, workspace: Path) -> WechatyConfig:
    return WechatyConfig.from_mapping(value, workspace)


def create_channel(config: object) -> WechatyChannel:
    if not isinstance(config, WechatyConfig):
        raise ValueError("wechaty requires WechatyConfig")
    return WechatyChannel(config)


__all__ = ["WechatyChannel", "WechatyConfig", "create_channel", "load_config"]
