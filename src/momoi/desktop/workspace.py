"""Desktop-only defaults; preserve user-selected providers and credentials."""

from pathlib import Path

import yaml

from ..config.workspace import atomic_write, bootstrap
from .embedding import MODEL, DIMENSIONS


MARKER = ".desktop-embedding.yaml"


def prepare_workspace(workspace: Path, endpoint: str) -> None:
    bootstrap(workspace / "config.json")
    provider_path = workspace / "providers.yaml"
    from ..config.manager import ConfigurationManager

    # Respect the configured provider filename instead of assuming providers.yaml.
    manager = ConfigurationManager(workspace / "config.json")
    provider_path = manager.provider_path
    catalog = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
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
