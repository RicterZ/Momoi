from dataclasses import dataclass
from pathlib import Path

from ..config.models import AppConfig
from ..config.workspace import atomic_write


@dataclass(frozen=True)
class PromptFile:
    id: str
    path: Path
    required: bool


class DashboardSettings:
    """Persist and activate the settings explicitly supported by the dashboard."""

    def __init__(self, files: tuple[PromptFile, ...]) -> None:
        self._files = {item.id: item for item in files}

    @classmethod
    def from_config(cls, config: AppConfig) -> "DashboardSettings":
        if config.soul_prompt_path is None or config.heartbeat_prompt_path is None:
            raise ValueError("dashboard settings paths are required")
        return cls(
            (
                PromptFile("soul", config.soul_prompt_path, True),
                PromptFile("heartbeat", config.heartbeat_prompt_path, False),
            )
        )

    def prompts(self) -> list[dict[str, object]]:
        return [self.read(prompt_id) for prompt_id in self._files]

    def read(self, prompt_id: str) -> dict[str, object]:
        item = self._file(prompt_id)
        content = item.path.read_text(encoding="utf-8") if item.path.is_file() else ""
        return {
            "id": item.id,
            "filename": item.path.name,
            "content": content,
        }

    def update(self, prompt_id: str, content: str) -> dict[str, object]:
        item = self._file(prompt_id)
        if item.required and not content.strip():
            raise ValueError(f"{prompt_id} prompt must not be empty")
        self._write(item.path, content)
        return self.read(prompt_id)

    _write = staticmethod(atomic_write)

    def _file(self, prompt_id: str) -> PromptFile:
        try:
            return self._files[prompt_id]
        except KeyError:
            raise KeyError(f"unknown prompt: {prompt_id}") from None
