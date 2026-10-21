from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class WechatyConfig:
    plugin: ClassVar[str] = "wechaty"
    workspace: Path
    data_dir: Path
    owner_id: str
    quiet_seconds: float = 6
    max_batch_seconds: float = 60

    @classmethod
    def from_mapping(cls, value: object, workspace: Path) -> "WechatyConfig":
        if not isinstance(value, dict):
            raise ValueError("channel settings must be a table/object")
        unknown = value.keys() - {"owner_id", "quiet_seconds", "max_batch_seconds"}
        if unknown:
            raise ValueError(f"unknown Wechaty setting: {sorted(unknown)[0]}")
        owner_id = value.get("owner_id", "")
        if not isinstance(owner_id, str) or len(owner_id) > 256 or "\0" in owner_id:
            raise ValueError("channel.settings.owner_id must be a valid string")
        def positive(name: str, default: float) -> float:
            number = float(value.get(name, default))
            if not 0 < number <= 3600:
                raise ValueError(f"channel.settings.{name} must be between 0 and 3600")
            return number
        root = workspace.expanduser().resolve()
        return cls(root, root / "channel" / "wechaty", owner_id.strip(),
                   positive("quiet_seconds", 6), positive("max_batch_seconds", 60))
