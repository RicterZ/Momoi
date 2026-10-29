"""Validated configuration documents shared by dashboard and runtime supervision."""

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import yaml

from .loading import parse_config, remove_retired_thinking_stages
from ..integrations.configuration import managed_catalog
from .models import ConfigError
from .workspace import atomic_write, default_config, empty_providers
from .runtime_fields import runtime_fields, migrate_budget
from ..integrations.configuration import CatalogLoader, parse_provider_catalog
from ..integrations.registry import adapter_schemas, adapter_definition
from ..integrations.configuration import CAPABILITIES
from ..mcp.config import load_mcp_servers, parse_mcp_servers

KEEP_SECRET = {"$secret": "keep"}
SECRET_NAMES = {
    "api_key",
    "secret_id",
    "secret_key",
    "token",
    "password",
    "access_token",
    "bridge_token",
}
# Host/auth/storage ownership belongs to the dashboard process and cannot hot reload.
EDITABLE = {
    "affinity",
    "budget",
    "channels",
    "context",
    "tools",
    "turn",
    "notifications",
    "heartbeat",
    "reflection",
    "episode_annealing",
    "webhooks",
    "logging",
    "thinking",
}


def managed_qq_call(app):
    """Desktop shell owns connection credentials; channel preferences stay editable."""
    reference = os.environ.get("MOMOI_QQ_CALL_MANAGED")
    if not reference:
        return app
    from urllib.parse import urlsplit

    try:
        settings = json.loads(Path(reference).read_text(encoding="utf-8"))
        address, token = settings["bridge_url"], settings["bridge_token"]
        url = urlsplit(address)
        if (url.scheme != "http" or url.hostname != "127.0.0.1"
                or not url.port or url.username or url.password
                or url.path or url.query or url.fragment
                or not isinstance(token, str) or len(token.encode()) < 32):
            raise ValueError("invalid managed connection")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ConfigError("desktop voice-call connection metadata is invalid") from None
    channel = app.get("channels", {}).get("enabled", {}).get("onebot")
    if isinstance(channel, dict):
        previous = channel.get("voice_call", {})
        channel["voice_call"] = {**previous, "bridge_url": address, "bridge_token": token}
    return app


class RevisionConflict(ConfigError):
    pass


def redact(value, *, credential=False):
    if isinstance(value, dict):
        return {
            key: (
                copy.deepcopy(KEEP_SECRET)
                if (credential or key in SECRET_NAMES)
                and item not in ("", None)
                and not (isinstance(item, dict) and set(item) == {"env"})
                else redact(item, credential=False)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def binding_options(raw, capability):
    binding = raw.get("bindings", {}).get(capability, {})
    service = raw.get("services", {}).get(binding.get("service"), {})
    options = copy.deepcopy(service.get("settings", {}))
    options.update(
        {key: service[key] for key in ("base_url", "timeout_seconds") if key in service}
    )
    options.update(binding.get("options", {}))
    options.update(raw.get("credentials", {}).get(service.get("credentials"), {}))
    if capability == "tts":
        try:
            definition = adapter_definition(service.get("adapter"), capability)
        except ValueError:
            pass  # Keep malformed configuration inspectable in the dashboard.
        else:
            if "base_url" not in definition.schema:
                options.pop("base_url", None)
    return copy.deepcopy(options)


def restore_secrets(value, previous):
    if value == KEEP_SECRET:
        if previous is None:
            raise ConfigError(
                "secret placeholder has no saved value; enter a credential"
            )
        return copy.deepcopy(previous)
    if isinstance(value, dict):
        old = previous if isinstance(previous, dict) else {}
        return {key: restore_secrets(item, old.get(key)) for key, item in value.items()}
    if isinstance(value, list):
        old = previous if isinstance(previous, list) else []
        return [
            restore_secrets(item, old[i] if i < len(old) else None)
            for i, item in enumerate(value)
        ]
    return value


class ConfigurationManager:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.provider_path = self._provider_path(self.read_app())

    def read_app(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ConfigError("cannot read config.json") from None
        if not isinstance(raw, dict):
            raise ConfigError("config.json must be an object")
        remove_retired_thinking_stages(raw)
        from .loading import migrate_qq_channel
        migrate_qq_channel(raw)
        return managed_qq_call(raw)

    def _provider_path(self, raw):
        reference = raw.get("providers")
        if not isinstance(reference, str) or not reference.strip():
            raise ConfigError("providers must be a YAML file path")
        path = Path(reference).expanduser()
        return (path if path.is_absolute() else self.path.parent / path).resolve()

    def read_providers(self):
        try:
            raw = yaml.load(
                self.provider_path.read_text(encoding="utf-8"), Loader=CatalogLoader
            )
        except (OSError, yaml.YAMLError):
            raise ConfigError("cannot read providers YAML") from None
        if not isinstance(raw, dict):
            raise ConfigError("providers must be an object")
        return managed_catalog(raw)

    def revision(self):
        digest = hashlib.sha256()
        paths = [self.path, self.provider_path]
        try:
            reference = self.read_app().get("tools", {}).get("mcp_config", "mcp.json")
            if reference:
                paths.append(self.mcp_path())
        except (ConfigError, AttributeError, TypeError):
            # Invalid config.json must produce a new revision, not stop the watcher.
            pass
        for path in paths:
            try:
                data = b"file:" + path.read_bytes()
            except OSError as error:
                data = f"unreadable:{type(error).__name__}".encode()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        return digest.hexdigest()

    def mcp_path(self, app=None):
        app = self.read_app() if app is None else app
        reference = app.get("tools", {}).get("mcp_config") or "mcp.json"
        path = Path(reference)
        return (path if path.is_absolute() else self.path.parent / path).resolve()

    def save_mcp(self, document, revision=None):
        """Replace the MCP document; the supervisor applies the saved generation."""
        expected = self.revision() if revision is None else revision
        if expected != self.revision():
            raise RevisionConflict("configuration changed; reload before saving")
        content = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        servers = parse_mcp_servers(content)
        app = self.read_app()
        path = self.mcp_path(app)
        if path in (self.path, self.provider_path):
            raise ConfigError("MCP configuration must use a separate file")
        tools = app.setdefault("tools", {})
        enable = not tools.get("mcp_config", "mcp.json")
        if enable:
            tools["mcp_config"] = "mcp.json"
        self.validate(app, mcp_servers=servers)
        if expected != self.revision():
            raise RevisionConflict("configuration changed; reload before saving")
        atomic_write(path, content)
        if enable:
            atomic_write(self.path, json.dumps(app, ensure_ascii=False, indent=2) + "\n")
        return self.snapshot()

    def validate(self, app=None, providers=None, *, mcp_servers=None):
        app = self.read_app() if app is None else app
        if self._provider_path(app) != self.provider_path:
            raise ConfigError(
                "provider catalog path changed; restart the dashboard process to apply"
            )
        providers = self.read_providers() if providers is None else providers
        catalog = parse_provider_catalog(providers, self.provider_path)
        config = parse_config(app, self.path, providers=catalog)
        servers = load_mcp_servers(config.mcp_config) if mcp_servers is None else mcp_servers
        return replace(config, mcp_servers=copy.deepcopy(servers))

    def dashboard_config(self):
        # A broken provider or channel must not take away the repair UI.
        saved = self.read_app()
        raw = default_config()
        for key in ("storage", "dashboard", "timezone", "logging", "context"):
            if key in saved:
                raw[key] = saved[key]
        catalog = parse_provider_catalog(empty_providers(), self.provider_path)
        return parse_config(raw, self.path, providers=catalog)

    def snapshot(self):
        app, providers = self.read_app(), self.read_providers()
        app_fields = runtime_fields()
        app_values = {key: value for key, value in app.items() if key in EDITABLE}
        if "budget" in app_values:
            app_values["budget"] = migrate_budget(app_values["budget"])
        for section, schema in app_fields.items():
            current = app_values.get(section, {})
            if isinstance(current, dict):
                app_values[section] = {
                    **{key: spec["default"] for key, spec in schema["fields"].items()},
                    **current,
                }
        problem = ""
        try:
            self.validate(app, providers)
        except (ValueError, OSError, TypeError, KeyError) as error:
            problem = (
                str(error) if isinstance(error, ConfigError) else type(error).__name__
            )
        return {
            "desktop_embedding_managed": bool(os.environ.get("MOMOI_DESKTOP_EMBEDDING")),
            "desktop_qq_call_managed": bool(os.environ.get("MOMOI_QQ_CALL_MANAGED")),
            "revision": self.revision(),
            "providers": copy.deepcopy(providers),
            "app": copy.deepcopy(app_values),
            "app_fields": app_fields,
            "adapters": adapter_schemas(),
            "validation_error": problem,
            "environment_overrides": sorted(
                key
                for key in os.environ
                if key.startswith("MOMOI_") and os.environ[key]
            ),
            "capabilities": {
                capability: {
                    "adapter": providers.get("services", {})
                    .get(binding.get("service"), {})
                    .get("adapter", ""),
                    "enabled": binding.get("enabled", True),
                    "options": binding_options(providers, capability),
                }
                for capability, binding in providers.get("bindings", {}).items()
            },
            "model_providers": self.model_providers(providers),
            "active_model_provider": providers.get("bindings", {}).get("llm", {}).get("service", ""),
            "image_model_provider": providers.get("bindings", {}).get("vision", {}).get("service", ""),
            "background_model_provider": providers.get("bindings", {}).get("background", {}).get("service", ""),
        }

    def model_providers(self, raw):
        active = raw.get("bindings", {}).get("llm", {}).get("service")
        models = []
        for identifier, service in raw.get("services", {}).items():
            try:
                definition = adapter_definition(service.get("adapter"), "llm")
            except ValueError:
                continue
            if identifier != active and any(
                binding.get("service") == identifier
                for capability, binding in raw.get("bindings", {}).items() if capability not in {"llm", "vision", "background"}
            ):
                continue
            if identifier != active and not service.get("name") and not service.get("settings", {}).get("model"):
                continue
            candidate = copy.deepcopy(raw)
            candidate.setdefault("bindings", {})["llm"] = (
                raw["bindings"]["llm"] if identifier == active else {"service": identifier}
            )
            options = binding_options(candidate, "llm")
            for key, spec in definition.schema.items():
                if "default" in spec and (key not in options or options[key] == ""):
                    options[key] = copy.deepcopy(spec["default"])
            models.append({"id": identifier, "name": service.get("name", ""),
                           "adapter": service["adapter"], "enabled": True,
                           "options": options})
        return models

    @staticmethod
    def _assign_stage_models(raw, models, replyer, document):
        """Replyer, planner and vision models combine freely; empty reuses the replyer or the model itself."""
        bindings = raw.setdefault("bindings", {})
        planner = document.get("planner") or ""
        vision = document.get("vision") or ""
        for value in (planner, vision):
            if not isinstance(value, str) or (value and value not in models):
                raise ConfigError("model provider not found")
        if planner and planner != replyer:
            bindings["background"] = {"service": planner, "enabled": True}
        else:
            bindings.pop("background", None)
        if vision:
            if not models[vision]["options"].get("multimodal", True):
                raise ConfigError("请选择支持多模态的图片识别 Provider")
            bindings["vision"] = {"service": vision, "enabled": True}
        else:
            bindings.pop("vision", None)
            if any(not models[item]["options"].get("multimodal", True) for item in {replyer, planner or replyer}):
                raise ConfigError("所选模型不支持图片输入，请选择图片识别模型")

    def save_model_providers(self, document, revision):
        if revision != self.revision():
            raise RevisionConflict("configuration changed; reload before saving")
        if not isinstance(document, dict) or set(document) - {"action", "id", "name", "adapter", "options", "vision", "planner"}:
            raise ConfigError("invalid model provider request")
        raw = self.read_providers()
        models = {item["id"]: item for item in self.model_providers(raw)}
        identifier = document.get("id")
        action = document.get("action")
        active = raw.get("bindings", {}).get("llm", {}).get("service")
        if action == "background":
            if identifier in {None, "", active}:
                raw.get("bindings", {}).pop("background", None)
            elif identifier not in models:
                raise ConfigError("model provider not found")
            else:
                raw.setdefault("bindings", {})["background"] = {"service": identifier, "enabled": True}
            return self.save("providers", raw, revision)
        if action in {"delete", "activate", "assign"}:
            if identifier not in models:
                raise ConfigError("model provider not found")
            if action == "delete":
                if any(b.get("service") == identifier for b in raw.get("bindings", {}).values()):
                    raise ConfigError("正在使用的 Provider 不能删除，请先切换模型")
                service = raw["services"].pop(identifier)
                credential = service.get("credentials")
                if credential and not any(s.get("credentials") == credential for s in raw["services"].values()):
                    raw.get("credentials", {}).pop(credential, None)
            elif identifier != active:
                previous = raw.get("bindings", {}).get("llm", {})
                # Preserve legacy model overrides before replacing its active binding.
                if previous.get("options"):
                    preserved = active
                    if any(b.get("service") == active for cap, b in raw["bindings"].items() if cap not in {"llm", "vision", "background"}):
                        preserved = f"{active}_model"
                        while preserved in raw["services"]:
                            preserved += "_new"
                        raw["services"][preserved] = copy.deepcopy(raw["services"][active])
                    raw["services"][preserved].setdefault("settings", {}).update(previous["options"])
                    for role in ("vision", "planner"):
                        if document.get(role) == active:
                            document = {**document, role: preserved}
                            models[preserved] = models[active]
                raw.setdefault("bindings", {})["llm"] = {"service": identifier, "enabled": True}
                if raw["bindings"].get("background", {}).get("service") == identifier:
                    raw["bindings"].pop("background")
            if action == "assign":
                self._assign_stage_models(raw, models, identifier, document)
            if action == "activate" and "vision" in document:
                if models[identifier]["options"].get("multimodal", True):
                    raw["bindings"].pop("vision", None)
                else:
                    vision = document["vision"]
                    if not isinstance(vision, str) or vision not in models or not models[vision]["options"].get("multimodal", True):
                        raise ConfigError("请选择支持多模态的图片识别 Provider")
                    raw["bindings"]["vision"] = {"service": vision, "enabled": True}
        elif action == "save":
            name = document.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ConfigError("请填写 Provider 名称（最多 80 个字符）")
            if identifier is not None and identifier not in models:
                raise ConfigError("model provider not found")
            if any(item["name"] == name.strip() and key != identifier for key, item in models.items()):
                raise ConfigError("Provider 名称已存在")
            definition = adapter_definition(document.get("adapter"), "llm")
            options = restore_secrets(document.get("options", {}), models.get(identifier, {}).get("options", {}))
            if not isinstance(options, dict):
                raise ConfigError("options must be an object")
            for key, spec in definition.schema.items():
                if "default" in spec and (key not in options or options[key] == ""):
                    options[key] = copy.deepcopy(spec["default"])
            # Validate inactive profiles too, using the normal Agent configuration path.
            candidate = copy.deepcopy(raw)
            self._set_binding(candidate, "llm", {"adapter": document["adapter"], "enabled": True, "options": options})
            self.validate(providers=candidate)
            previous = raw.get("bindings", {}).get("llm")
            if identifier is None:
                identifier = "model_provider"
                while identifier in raw.get("services", {}) or identifier in raw.get("credentials", {}):
                    identifier += "_new"
            updating_active = identifier == active
            shared = any(b.get("service") == identifier for cap, b in raw.get("bindings", {}).items() if cap not in {"llm", "vision", "background"})
            if shared:
                identifier += "_model"
                while identifier in raw.get("services", {}) or identifier in raw.get("credentials", {}):
                    identifier += "_new"
            old_credential = raw.get("services", {}).get(identifier, {}).get("credentials")
            credential = f"{identifier}_model_key"
            while credential in raw.get("credentials", {}) and any(
                key != identifier and s.get("credentials") == credential for key, s in raw.get("services", {}).items()
            ):
                credential += "_new"
            secrets = {k: v for k, v in options.items() if definition.schema.get(k, {}).get("secret")}
            service = {"name": name.strip(), "adapter": document["adapter"],
                       "settings": {k: v for k, v in options.items() if k not in secrets}}
            if secrets:
                raw.setdefault("credentials", {})[credential] = secrets
                service["credentials"] = credential
            raw.setdefault("services", {})[identifier] = service
            if old_credential and old_credential != service.get("credentials") and not any(
                s.get("credentials") == old_credential for s in raw["services"].values()
            ):
                raw.get("credentials", {}).pop(old_credential, None)
            if previous is None or updating_active:
                raw.setdefault("bindings", {})["llm"] = {"service": identifier, "enabled": True}
        else:
            raise ConfigError("invalid model provider action")
        return self.save("providers", raw, revision)

    def save_binding(self, capability, document, revision):
        return self.save_bindings({capability: document}, revision)

    def save_bindings(self, documents, revision):
        if not isinstance(documents, dict) or not documents:
            raise ConfigError("capability configurations must be a nonempty object")
        if revision != self.revision():
            raise RevisionConflict("configuration changed; reload before saving")
        raw = self.read_providers()
        for capability, document in documents.items():
            self._set_binding(raw, capability, document)
        return self.save("providers", raw, revision)

    def save_runtime(self, document, revision):
        if not isinstance(document, dict) or set(document) - EDITABLE:
            raise ConfigError("only runtime settings can be changed here")
        current = {
            key: value for key, value in self.read_app().items() if key in EDITABLE
        }
        controls = runtime_fields()
        for section, value in document.items():
            if section in controls and isinstance(value, dict):
                previous = current.get(section, {})
                if section == "budget":
                    previous = migrate_budget(previous)
                if section == "thinking" and isinstance(value.get("stages"), dict):
                    value = {
                        **value,
                        "stages": {
                            **(previous.get("stages", {}) if isinstance(previous, dict) else {}),
                            **value["stages"],
                        },
                    }
                current[section] = {
                    **(previous if isinstance(previous, dict) else {}),
                    **value,
                }
            else:
                current[section] = value
        tools = document.get("tools", {})
        if isinstance(tools, dict) and tools.get("deepseek_web_enabled") is True:
            from ..tools.deepseek_web import deepseek_web_key
            catalog = parse_provider_catalog(self.read_providers(), self.provider_path)
            if not deepseek_web_key(catalog):
                raise ConfigError("请先配置 DeepSeek API Key")
        return self.save("app", current, revision)

    def _set_binding(self, raw, capability, document):
        if capability == "embedding" and os.environ.get("MOMOI_DESKTOP_EMBEDDING"):
            raise ConfigError("Embedding 由客户端自动维护")
        if capability not in CAPABILITIES or not isinstance(document, dict):
            raise ConfigError("invalid capability configuration")
        if set(document) - {"adapter", "enabled", "options"}:
            raise ConfigError("unknown capability field")
        try:
            definition = adapter_definition(document.get("adapter"), "llm" if capability in {"vision", "background"} else capability)
        except ValueError as error:
            raise ConfigError(str(error)) from None
        options = copy.deepcopy(document.get("options", {}))
        if not isinstance(options, dict):
            raise ConfigError("options must be an object")
        if capability == "tts" and "base_url" not in definition.schema:
            options.pop("base_url", None)
        # Copy on write: changing one capability never mutates a shared service or credential.
        name = f"configured_{capability}"
        existing = raw.get("bindings", {}).get(capability, {}).get("service")
        occupied = set(raw.get("services", {})) | set(raw.get("credentials", {}))
        shared = {
            b.get("service")
            for c, b in raw.get("bindings", {}).items()
            if c != capability
        }
        shared_credentials = {
            service.get("credentials")
            for service_name, service in raw.get("services", {}).items()
            if service_name != existing
        }
        if (
            existing
            and existing.startswith(name)
            and existing not in shared
            and existing not in shared_credentials
        ):
            name = existing
        else:
            suffix = 1
            while name in occupied:
                name = f"configured_{capability}_{suffix}"
                suffix += 1
        secrets = {
            key: value
            for key, value in options.items()
            if definition.schema.get(key, {}).get("secret")
        }
        settings = {key: value for key, value in options.items() if key not in secrets}
        service = {"adapter": document.get("adapter"), "settings": settings}
        if secrets:
            raw.setdefault("credentials", {})[name] = secrets
            service["credentials"] = name
        raw.setdefault("services", {})[name] = service
        raw.setdefault("bindings", {})[capability] = {
            "service": name,
            "enabled": document.get("enabled", capability == "llm"),
        }

    def save(self, section, document, revision):
        if revision != self.revision():
            raise RevisionConflict("configuration changed; reload before saving")
        if not isinstance(document, dict):
            raise ConfigError("document must be an object")
        app, providers = self.read_app(), self.read_providers()
        if section == "providers":
            candidate = managed_catalog(copy.deepcopy(document))
            if "llm" in providers.get("bindings", {}) and "llm" not in candidate.get(
                "bindings", {}
            ):
                raise ConfigError(
                    "the model cannot be removed; configure or replace it"
                )
            # Module installation/import is a local deployment concern, never an HTTP operation.
            if candidate.get("plugins", []) != providers.get("plugins", []):
                raise ConfigError(
                    "provider plugins must be installed and configured locally"
                )
            self.validate(app, candidate)
            path, content = (
                self.provider_path,
                yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False),
            )
        elif section == "app":
            if set(document) - EDITABLE:
                raise ConfigError("only runtime settings can be changed here")
            candidate = {
                key: value for key, value in app.items() if key not in EDITABLE
            }
            candidate.update(restore_secrets(document, app))
            previous_context = app.get("context", {})
            for key in ("soul_prompt", "heartbeat_prompt"):
                if candidate.get("context", {}).get(key) != previous_context.get(key):
                    raise ConfigError(
                        "prompt file paths require a process restart; edit prompt content in the prompt editor"
                    )
            candidate = managed_qq_call(candidate)
            self.validate(candidate, providers)
            path, content = (
                self.path,
                json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            )
        else:
            raise ConfigError("unknown configuration section")
        # Recheck after validation; an external editor may have changed either document.
        if revision != self.revision():
            raise RevisionConflict("configuration changed; reload before saving")
        atomic_write(path, content)
        return self.snapshot()
