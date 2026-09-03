import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..config.models import AppConfig, LLMConfig


@dataclass(frozen=True)
class PromptFile:
    id: str
    path: Path
    required: bool


class DashboardSettings:
    """Persist and activate the settings explicitly supported by the dashboard."""

    def __init__(
        self,
        files: tuple[PromptFile, ...],
        *,
        config_path: Path,
        llm_config: Callable[[], LLMConfig],
        activate_llm: Callable[[LLMConfig], Awaitable[None]],
    ) -> None:
        self._files = {item.id: item for item in files}
        self._config_path = config_path
        self._llm_config = llm_config
        self._activate_llm = activate_llm

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        llm_config: Callable[[], LLMConfig],
        activate_llm: Callable[[LLMConfig], Awaitable[None]],
    ) -> "DashboardSettings":
        if (
            config.soul_prompt_path is None
            or config.heartbeat_prompt_path is None
            or config.config_path is None
        ):
            raise ValueError("dashboard settings paths are required")
        return cls(
            (
                PromptFile("soul", config.soul_prompt_path, True),
                PromptFile("heartbeat", config.heartbeat_prompt_path, False),
            ),
            config_path=config.config_path,
            llm_config=llm_config,
            activate_llm=activate_llm,
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

    def llm(self) -> dict[str, object]:
        config = self._llm_config()
        return {
            "api_format": config.api_format,
            "base_url": config.base_url,
            "model": config.model,
            "api_key_configured": bool(config.api_key),
        }

    async def update_llm(self, fields: dict[str, object]) -> dict[str, object]:
        allowed = {"api_format", "base_url", "api_key", "model"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown llm field: {sorted(unknown)[0]}")
        if not fields:
            raise ValueError("no llm fields provided")
        invalid = [name for name, value in fields.items() if not isinstance(value, str)]
        if invalid:
            raise ValueError(f"llm.{sorted(invalid)[0]} must be a string")
        current = self._llm_config()
        api_format = str(fields.get("api_format", current.api_format)).strip().lower()
        base_url = str(fields.get("base_url", current.base_url)).strip().rstrip("/")
        model = str(fields.get("model", current.model)).strip()
        api_key = str(fields.get("api_key", current.api_key)).strip()
        if api_format not in {"anthropic", "openai"}:
            raise ValueError("llm.api_format must be anthropic or openai")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("llm.base_url must be an absolute HTTP URL")
        if not model:
            raise ValueError("llm.model must not be empty")
        if not api_key:
            raise ValueError("llm.api_key must not be empty")
        updated = LLMConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=current.max_tokens,
            temperature=current.temperature,
            timeout_seconds=current.timeout_seconds,
            max_retries=current.max_retries,
            api_format=api_format,
            tool_choice=current.tool_choice,
            thinking=current.thinking,
        )
        original = self._config_path.read_text(encoding="utf-8")
        raw = json.loads(original)
        if not isinstance(raw, dict) or not isinstance(raw.get("llm"), dict):
            raise ValueError("config.json llm section is invalid")
        raw_llm = raw["llm"]
        if "api_format" in fields:
            raw_llm["api_format"] = updated.api_format
        if "base_url" in fields:
            raw_llm["base_url"] = updated.base_url
        if "model" in fields:
            raw_llm["model"] = updated.model
        if "api_key" in fields:
            raw_llm["api_key"] = updated.api_key
        self._write(
            self._config_path,
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        )
        try:
            await self._activate_llm(updated)
        except BaseException:
            self._write(self._config_path, original)
            raise
        return self.llm()

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
                temporary = file.name
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _file(self, prompt_id: str) -> PromptFile:
        try:
            return self._files[prompt_id]
        except KeyError:
            raise KeyError(f"unknown prompt: {prompt_id}") from None
