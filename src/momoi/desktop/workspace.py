"""Desktop-only defaults; preserve user-selected providers and credentials."""

import json
import os
from zoneinfo import ZoneInfo
from importlib.resources import files
from pathlib import Path

import yaml

from ..config.workspace import atomic_write, bootstrap
from .embedding import MODEL, DIMENSIONS


MARKER = ".desktop-embedding.yaml"


def prepare_workspace(workspace: Path, endpoint: str) -> None:
    config_path = workspace / "config.json"
    fresh = not config_path.exists()
    app = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    soul_path = workspace / app.get("context", {}).get("soul_prompt", "prompts/SOUL.md")
    for name, path in (
        ("SOUL", soul_path),
        ("PLANNER", soul_path.parent / "PLANNER.md"),
        ("REPLYER", soul_path.parent / "REPLYER.md"),
    ):
        if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
            content = files("momoi.desktop").joinpath(f"default_prompts/{name}.md").read_text(encoding="utf-8")
            atomic_write(path, content)
    bootstrap(config_path)
    if fresh and (timezone := os.environ.get("MOMOI_DESKTOP_TIMEZONE")):
        ZoneInfo(timezone)
        initialized = json.loads(config_path.read_text(encoding="utf-8"))
        initialized["timezone"] = timezone
        atomic_write(config_path, json.dumps(initialized, ensure_ascii=False, indent=2) + "\n")
    provider_path = workspace / "providers.yaml"
    from ..config.manager import ConfigurationManager

    # Respect the configured provider filename instead of assuming providers.yaml.
    manager = ConfigurationManager(workspace / "config.json")
    provider_path = manager.provider_path
    catalog = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    before = yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False)
    bindings = catalog.setdefault("bindings", {})
    services = catalog.setdefault("services", {})
    for capability, adapter, defaults in (
        ("llm", "openai", {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"}),
        ("tts", "fish", {"reference_id": "9bb8ad542dc44d148c21c73a0884e9ae"}),
    ):
        if capability not in bindings:
            name = f"desktop_{capability}"
            while name in services:
                name += "_default"
            services[name] = {"adapter": adapter}
            bindings[capability] = {"service": name, "enabled": capability == "llm", "options": {}}
        configured = bindings[capability]
        service = services.get(configured.get("service"), {})
        if service.get("adapter") != adapter:
            continue
        options = configured.setdefault("options", {})
        for key, default in defaults.items():
            value = options.get(key, service.get("settings", {}).get(key, service.get(key)))
            if value is None or (isinstance(value, str) and not value.strip()):
                options[key] = default
    if yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False) != before:
        atomic_write(provider_path, yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False))
    binding = catalog.get("bindings", {}).get("embedding")
    marker_path = workspace / MARKER
    previous = yaml.safe_load(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None
    options = binding.get("options", {}) if isinstance(binding, dict) else {}
    service = catalog.get("services", {}).get(binding.get("service")) if isinstance(binding, dict) else None
    original = options.get("endpoint") == "http://embedding:8002/v1/embeddings"
    managed = isinstance(previous, dict) and options.get("endpoint") == previous.get("endpoint")
    if (original or managed) and service and service.get("adapter") == "openai" and options.get("model") == MODEL and options.get("dimensions") == DIMENSIONS:
        # Only change the address. Preserve disabled bindings and encoding identity.
        options["endpoint"] = endpoint
        atomic_write(provider_path, yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False))
        atomic_write(marker_path, yaml.safe_dump({"endpoint": endpoint}))
