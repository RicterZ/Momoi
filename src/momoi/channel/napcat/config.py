from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlsplit


@dataclass(frozen=True)
class QQCallConfig:
    enabled: bool = False
    bridge_url: str = ""
    bridge_token: str = field(default="", repr=False)
    request_timeout_seconds: float = 5

    @classmethod
    def from_mapping(cls, value, *, access_token=""):
        if not isinstance(value, dict) or value.keys() - {"enabled", "bridge_url", "bridge_token", "request_timeout_seconds"}:
            raise ValueError("invalid napcat voice_call configuration")
        enabled = value.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("voice_call.enabled must be boolean")
        url = value.get("bridge_url", "")
        token = value.get("bridge_token", "") or access_token
        if not isinstance(url, str) or not isinstance(token, str):
            raise ValueError("voice_call address and token must be strings")
        url = url.rstrip("/")
        parsed = urlsplit(url)
        if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("voice_call.bridge_url must be an HTTP(S) service address")
        if enabled and (not url or len(token.encode()) < 32):
            raise ValueError("enabled voice_call requires bridge_url and a token of at least 32 bytes")
        timeout = value.get("request_timeout_seconds", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("voice_call.request_timeout_seconds must be between 0 and 60")
        return cls(enabled, url, token, float(timeout))


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
    media_download_timeout_seconds: float = 60

    access_token: str = ""
    bot_qq: str = ""
    voice_call: QQCallConfig = QQCallConfig()

    @classmethod
    def from_mapping(cls, value: object) -> "NapCatConfig":
        if not isinstance(value, dict):
            raise ValueError("channel settings must be a table/object")
        owner_qq = str(value.get("owner_qq") or "")
        if not owner_qq.isdigit():
            raise ValueError("channel.settings.owner_qq must contain digits only")

        bot_qq = str(value.get("bot_qq") or "")
        if bot_qq and (not bot_qq.isascii() or not bot_qq.isdigit()):
            raise ValueError("channel.settings.bot_qq must contain ASCII digits only")

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
            access_token=str(value.get("access_token") or ""),
            bot_qq=bot_qq,
            voice_call=QQCallConfig.from_mapping(value.get("voice_call", {}),
                access_token=str(value.get("access_token") or "")),
            owner_qq=owner_qq,
            quiet_seconds=positive("quiet_seconds", 1),
            max_batch_seconds=positive("max_batch_seconds", 60),
            heartbeat_seconds=positive("heartbeat_seconds", 30),
            reconnect_max_seconds=positive("reconnect_max_seconds", 30),
            send_timeout_seconds=positive("send_timeout_seconds", 20),
            media_max_bytes=media_max_bytes,
            media_download_timeout_seconds=positive(
                "media_download_timeout_seconds", 60
            ),
        )
