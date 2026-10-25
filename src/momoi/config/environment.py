import os
from pathlib import Path
from typing import Any

from .models import ConfigError


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _token_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _env_bool(name: str) -> bool | None:
    value = _env(name).lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def apply_env_overrides(raw: dict[str, Any]) -> None:
    channels = raw.get("channels")
    if isinstance(channels, dict):
        if value := _env("MOMOI_PRIMARY"):
            channels["primary"] = value
        enabled = channels.get("enabled")
        onebot = enabled.get("onebot") if isinstance(enabled, dict) else None
        if isinstance(onebot, dict):
            if value := _env("MOMOI_ONEBOT_URL") or _env("MOMOI_NAPCAT_URL"):
                onebot["url"] = value
            if value := _env("MOMOI_OWNER_QQ"):
                onebot["owner_qq"] = value
            if not str(onebot.get("access_token") or "").strip():
                token = _token_file(_env("MOMOI_ONEBOT_TOKEN_FILE") or _env("MOMOI_NAPCAT_TOKEN_FILE"))
                if token:
                    onebot["access_token"] = token
            if value := _env("MOMOI_ONEBOT_ACCESS_TOKEN") or _env("MOMOI_NAPCAT_ACCESS_TOKEN"):
                onebot["access_token"] = value

    if value := _env("MOMOI_TIMEZONE"):
        raw["timezone"] = value

    dashboard = raw.setdefault("dashboard", {})
    if isinstance(dashboard, dict) and (value := _env("MOMOI_DASHBOARD_TOKEN")):
        dashboard["token"] = value

    webhooks = raw.setdefault("webhooks", {})
    if isinstance(webhooks, dict):
        enabled = _env_bool("MOMOI_WEBHOOKS_ENABLED")
        if enabled is not None:
            webhooks["enabled"] = enabled
        if value := _env("MOMOI_WEBHOOKS_HOST"):
            webhooks["host"] = value
        if value := _env("MOMOI_WEBHOOKS_TOKEN"):
            webhooks["token"] = value
