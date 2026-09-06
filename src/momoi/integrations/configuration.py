"""Versioned provider catalog; the only provider configuration source."""

import copy
import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config.models import ConfigError

CAPABILITIES = frozenset({"llm", "asr", "tts", "embedding", "balance"})


class CatalogLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently replacing credentials or bindings."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError("providers YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


CatalogLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def table(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{name} must be a mapping with string keys")
    return value


def keys(raw: dict[str, Any], allowed: set[str], name: str) -> None:
    if unknown := set(raw) - allowed:
        raise ConfigError(f"unknown {name} field: {sorted(unknown)[0]}")


@dataclass(frozen=True)
class ProviderBinding:
    service: str
    adapter: str
    enabled: bool
    options: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class ProviderCatalog:
    path: Path
    bindings: dict[str, ProviderBinding] = field(repr=False)

    def enabled(self, capability: str) -> bool:
        binding = self.bindings.get(capability)
        return bool(binding and binding.enabled)

    def adapter_for(self, capability: str) -> str:
        binding = self.bindings.get(capability)
        return binding.adapter if binding else ""

    def options_for(self, capability: str) -> dict[str, Any]:
        return (
            copy.deepcopy(self.bindings[capability].options)
            if capability in self.bindings
            else {}
        )


def load_provider_catalog(path: Path) -> ProviderCatalog:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=CatalogLoader)
    except (OSError, yaml.YAMLError) as error:
        # YAML exceptions may include source lines containing secrets.
        raise ConfigError(
            f"cannot load providers YAML {path}: {type(error).__name__}"
        ) from None
    raw = table(raw, "providers")
    keys(
        raw, {"version", "plugins", "credentials", "services", "bindings"}, "providers"
    )
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ConfigError("providers.version must be 1")
    plugins = raw.get("plugins", [])
    if not isinstance(plugins, list) or any(
        not isinstance(name, str) or not name for name in plugins
    ):
        raise ConfigError("providers.plugins must be a list of Python module names")
    for name in plugins:
        try:
            importlib.import_module(name)
        except Exception as error:
            raise ConfigError(
                f"cannot load provider plugin {name}: {type(error).__name__}"
            ) from None
    from .registry import adapter_definition

    credentials = table(raw.get("credentials", {}), "credentials")
    for name, secrets in credentials.items():
        for key, secret in table(secrets, f"credentials.{name}").items():
            if isinstance(secret, dict):
                keys(secret, {"env"}, f"credentials.{name}.{key}")
                if not isinstance(secret.get("env"), str) or not secret["env"]:
                    raise ConfigError(
                        f"credentials.{name}.{key}.env must name an environment variable"
                    )
            elif not isinstance(secret, str):
                raise ConfigError(
                    f"credentials.{name}.{key} must be a string or env reference"
                )
    services = table(raw.get("services"), "services")
    bindings = table(raw.get("bindings"), "bindings")
    keys(bindings, set(CAPABILITIES), "bindings")
    for name, definition in services.items():
        definition = table(definition, f"services.{name}")
        keys(
            definition,
            {"adapter", "credentials", "base_url", "timeout_seconds", "settings"},
            f"services.{name}",
        )
        if (
            not isinstance(definition.get("adapter"), str)
            or not definition["adapter"].strip()
        ):
            raise ConfigError(f"services.{name}.adapter is required")
        table(definition.get("settings", {}), f"services.{name}.settings")
        credential = definition.get("credentials")
        if credential is not None and (
            not isinstance(credential, str) or credential not in credentials
        ):
            raise ConfigError(
                f"services.{name}.credentials references an unknown credential"
            )
    resolved = {}
    for capability, binding in bindings.items():
        binding = table(binding, f"bindings.{capability}")
        keys(binding, {"service", "enabled", "options"}, f"bindings.{capability}")
        enabled = binding.get("enabled", True)
        if type(enabled) is not bool:
            raise ConfigError(f"bindings.{capability}.enabled must be boolean")
        name = binding.get("service")
        if not isinstance(name, str) or name not in services:
            raise ConfigError(
                f"bindings.{capability}.service references an unknown service"
            )
        service = services[name]
        adapter = service["adapter"]
        try:
            definition = adapter_definition(adapter, capability)
        except ValueError as error:
            raise ConfigError(str(error)) from None
        options = dict(service.get("settings", {}))
        options.update(
            {
                key: service[key]
                for key in ("base_url", "timeout_seconds")
                if key in service
            }
        )
        options.update(
            table(binding.get("options", {}), f"bindings.{capability}.options")
        )
        credential = service.get("credentials")
        if credential is not None:
            secrets = table(credentials[credential], f"credentials.{credential}")
            if set(secrets) & options.keys():
                raise ConfigError(f"bindings.{capability} duplicates credential fields")
            for key, secret in secrets.items():
                if isinstance(secret, dict):
                    keys(secret, {"env"}, f"credentials.{credential}.{key}")
                    env = secret.get("env")
                    if not isinstance(env, str) or not env:
                        raise ConfigError(
                            f"credentials.{credential}.{key}.env must name an environment variable"
                        )
                    secret = os.environ.get(env, "")
                    if enabled and not secret:
                        raise ConfigError(
                            f"missing credential environment variable: {env}"
                        )
                if not isinstance(secret, str):
                    raise ConfigError(
                        f"credentials.{credential}.{key} must be a string or env reference"
                    )
                options[key] = secret
        if enabled:
            try:
                definition.validate(options)
            except ConfigError:
                raise
            except (TypeError, ValueError):
                raise ConfigError(
                    f"invalid options for {adapter}/{capability}"
                ) from None
        resolved[capability] = ProviderBinding(name, adapter, enabled, options)
    catalog = ProviderCatalog(path, resolved)
    if not catalog.enabled("llm"):
        raise ConfigError("providers.bindings.llm must be enabled")
    return catalog


def resolve_provider_config(raw: dict[str, Any], config_path: Path) -> ProviderCatalog:
    reference = raw.get("providers")
    if not isinstance(reference, str) or not reference.strip():
        raise ConfigError("providers must be a YAML file path")
    path = Path(reference).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    catalog = load_provider_catalog(path.resolve())
    return catalog
