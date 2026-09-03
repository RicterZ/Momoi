import os
from typing import Any

from .models import ConfigError


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


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
        napcat = enabled.get("napcat") if isinstance(enabled, dict) else None
        if isinstance(napcat, dict):
            if value := _env("MOMOI_NAPCAT_URL"):
                napcat["url"] = value
            if value := _env("MOMOI_OWNER_QQ"):
                napcat["owner_qq"] = value

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

    usage = raw.setdefault("usage", {})
    if isinstance(usage, dict) and (value := _env("MOMOI_USAGE_API_KEY")):
        usage["api_key"] = value

    asr = raw.setdefault("asr", {})
    if isinstance(asr, dict):
        settings = asr.setdefault("settings", {})
        if isinstance(settings, dict):
            if value := _env("MOMOI_ASR_SECRET_ID"):
                settings["secret_id"] = value
            if value := _env("MOMOI_ASR_SECRET_KEY"):
                settings["secret_key"] = value
