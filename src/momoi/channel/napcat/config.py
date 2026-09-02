from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class NapCatConfig:
    plugin: ClassVar[str] = "napcat"
    url: str
    owner_qq: str
    quiet_seconds: float
    max_batch_seconds: float
    heartbeat_seconds: float
    reconnect_max_seconds: float
    send_timeout_seconds: float
    media_max_bytes: int = 20 * 1024 * 1024
    media_download_timeout_seconds: float = 15

    @classmethod
    def from_mapping(cls, value: object) -> "NapCatConfig":
        if not isinstance(value, dict):
            raise ValueError("channel settings must be a table/object")
        owner_qq = str(value.get("owner_qq") or "")
        if not owner_qq.isdigit():
            raise ValueError("channel.settings.owner_qq must contain digits only")

        def positive(name: str, default: float) -> float:
            number = float(value.get(name, default))
            if number <= 0:
                raise ValueError(f"channel.settings.{name} must be positive")
            return number

        media_max_bytes = int(value.get("media_max_bytes", 20 * 1024 * 1024))
        if media_max_bytes <= 0:
            raise ValueError("channel.settings.media_max_bytes must be positive")

        url = str(value.get("url") or "")
        if not url:
            raise ValueError("channel.settings.url is required")
        return cls(
            url=url,
            owner_qq=owner_qq,
            quiet_seconds=positive("quiet_seconds", 1),
            max_batch_seconds=positive("max_batch_seconds", 60),
            heartbeat_seconds=positive("heartbeat_seconds", 30),
            reconnect_max_seconds=positive("reconnect_max_seconds", 30),
            send_timeout_seconds=positive("send_timeout_seconds", 20),
            media_max_bytes=media_max_bytes,
            media_download_timeout_seconds=positive(
                "media_download_timeout_seconds", 15
            ),
        )
