import hashlib
import re
import shutil
from pathlib import Path
from typing import Protocol


EMOTION_PREFIX = "emotion://"
EMOTION_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class EmotionStore(Protocol):
    def emotion_path_referenced(
        self, path: str, *, exclude_slug: str | None = None
    ) -> bool: ...


def valid_emotion_slug(value: str) -> bool:
    return EMOTION_SLUG.fullmatch(value) is not None


def emotion_slug(message: str) -> str | None:
    if not message.startswith(EMOTION_PREFIX):
        return None
    slug = message[len(EMOTION_PREFIX) :]
    return slug if valid_emotion_slug(slug) else None


def emotion_directory(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / "emotion"


def managed_emotion_path(workspace: str | Path, source_value: str | Path) -> Path:
    source = Path(source_value).expanduser().resolve()
    if not source.is_file():
        raise ValueError("path must be an existing file")
    extension = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ValueError("emotion file needs a simple extension")
    digest = hashlib.md5(usedforsecurity=False)
    with source.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    directory = emotion_directory(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest.hexdigest()}{extension}"
    if not destination.exists():
        shutil.copy2(source, destination)
    return destination


def managed_emotion_bytes(
    workspace: str | Path, data: bytes, filename: str
) -> Path:
    if not data:
        raise ValueError("emotion file must not be empty")
    extension = Path(filename).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ValueError("emotion file needs a simple extension")
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    directory = emotion_directory(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}{extension}"
    if not destination.exists():
        destination.write_bytes(data)
    return destination


def remove_unreferenced_emotion_asset(
    store: EmotionStore, path: str, workspace: str | Path
) -> None:
    asset = Path(path)
    directory = emotion_directory(workspace)
    if asset.is_relative_to(directory) and not store.emotion_path_referenced(str(asset)):
        asset.unlink(missing_ok=True)
