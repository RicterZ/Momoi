import re
from typing import Any

from .models import ConfigError


def mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table/object")
    return value


def positive(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ConfigError(f"{name} must be positive")
    return number


def nonnegative(value: Any, name: str) -> float:
    number = float(value)
    if number < 0:
        raise ConfigError(f"{name} must not be negative")
    return number


def integer(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return value


def boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be boolean")
    return value


def clock(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ConfigError(f"{name} must use HH:MM")
    return text
