import asyncio
from pathlib import Path

import aiohttp

from .. import ChannelDependencies
from .api import login as _login
from .channel import WeixinChannel, render_segments
from .config import WeixinConfig, WeixinState


def load_config(value: object, workspace: Path) -> WeixinConfig:
    return WeixinConfig.from_mapping(value, workspace)


def create_channel(
    config: object, _dependencies: ChannelDependencies | None = None
) -> WeixinChannel:
    if not isinstance(config, WeixinConfig):
        raise ValueError("weixin requires WeixinConfig")
    return WeixinChannel(config)


async def login(config: object) -> None:
    if not isinstance(config, WeixinConfig):
        raise ValueError("weixin login requires WeixinConfig")
    try:
        await _login(config)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
        raise ValueError(f"Weixin login failed: {type(error).__name__}") from error


__all__ = [
    "WeixinChannel",
    "WeixinConfig",
    "WeixinState",
    "create_channel",
    "load_config",
    "login",
    "render_segments",
]
