from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class WechatyConfig:
    plugin: ClassVar[str] = "wechaty"
    workspace: Path
    data_dir: Path
    owner_alias: str
    quiet_seconds: float = 6
    max_batch_seconds: float = 60

    @classmethod
    def from_mapping(cls, value: object, workspace: Path) -> "WechatyConfig":
        if not isinstance(value, dict):
            raise ValueError("channel settings must be a table/object")
        # owner_id is a retired key: wechat4u contact ids change on every login.
        unknown = value.keys() - {"owner_alias", "owner_id", "quiet_seconds", "max_batch_seconds"}
        if unknown:
            raise ValueError(f"unknown Wechaty setting: {sorted(unknown)[0]}")
        owner_alias = value.get("owner_alias", "")
        if not isinstance(owner_alias, str) or len(owner_alias) > 256 or "\0" in owner_alias:
            raise ValueError("channel.settings.owner_alias must be a valid string")
        def positive(name: str, default: float) -> float:
            number = float(value.get(name, default))
            if not 0 < number <= 3600:
                raise ValueError(f"channel.settings.{name} must be between 0 and 3600")
            return number
        root = workspace.expanduser().resolve()
        return cls(root, root / "channel" / "wechaty", owner_alias.strip(),
                   positive("quiet_seconds", 6), positive("max_batch_seconds", 60))
