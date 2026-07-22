import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"

try:
    MOMOI_VERSION = version("momoi")
except PackageNotFoundError:
    MOMOI_VERSION = "0.0.0"


@dataclass(frozen=True)
class WeixinConfig:
    plugin: ClassVar[str] = "weixin"
    workspace: Path
    state_path: Path
    media_dir: Path
    quiet_seconds: float
    max_batch_seconds: float
    reconnect_max_seconds: float
    send_timeout_seconds: float
    media_max_bytes: int

    @classmethod
    def from_mapping(cls, value: object, workspace: Path) -> "WeixinConfig":
        if not isinstance(value, dict):
            raise ValueError("channel settings must be a table/object")

        def positive(name: str, default: float) -> float:
            number = float(value.get(name, default))
            if number <= 0:
                raise ValueError(f"channel.settings.{name} must be positive")
            return number

        maximum = int(value.get("media_max_bytes", 100 * 1024 * 1024))
        if maximum <= 0:
            raise ValueError("channel.settings.media_max_bytes must be positive")
        workspace = workspace.expanduser().resolve()
        directory = workspace / "channel" / "weixin"
        return cls(
            workspace=workspace,
            state_path=directory / "state.json",
            media_dir=directory / "media" / "inbound",
            quiet_seconds=positive("quiet_seconds", 6),
            max_batch_seconds=positive("max_batch_seconds", 60),
            reconnect_max_seconds=positive("reconnect_max_seconds", 30),
            send_timeout_seconds=positive("send_timeout_seconds", 20),
            media_max_bytes=maximum,
        )


@dataclass
class WeixinState:
    account_id: str
    user_id: str
    token: str
    base_url: str = DEFAULT_BASE_URL
    get_updates_buf: str = ""
    context_token: str = ""
    version: int = 1
    saved_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "WeixinState | None":
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Weixin state.json is unreadable") from error
        if not isinstance(value, dict) or int(value.get("version", 0)) != 1:
            raise ValueError("Weixin state.json has an unsupported format")
        required = {
            name: str(value.get(name) or "").strip()
            for name in ("account_id", "user_id", "token", "base_url")
        }
        if not all(required.values()):
            raise ValueError("Weixin state.json is missing login credentials")
        if urlparse(required["base_url"]).scheme not in {"http", "https"}:
            raise ValueError("Weixin state.json has an invalid base_url")
        return cls(
            **required,
            get_updates_buf=str(value.get("get_updates_buf") or ""),
            context_token=str(value.get("context_token") or ""),
            saved_at=str(value.get("saved_at") or ""),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.saved_at = datetime.now(UTC).isoformat()
        data = json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False
            ) as file:
                temporary = file.name
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
