"""Capability factories and resource ownership at application composition boundaries."""

import inspect
from contextlib import AsyncExitStack
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .contracts.asr import ASRProvider
from .contracts.balance import BalanceProvider
from .contracts.embedding import Embedder
from .contracts.llm import LanguageModel
from .contracts.tts import TTSProvider
from .transport import HTTPTransport
from .builtins import register_builtins
from .fields import normalize_fields, validate_schema
from .models import EmbeddingSpaceConfig

from ..policies import SemanticPolicy

if TYPE_CHECKING:
    from .configuration import ProviderCatalog


@dataclass(frozen=True)
class AdapterContext:
    transport: HTTPTransport
    dump_dir: Path | None = None
    semantic_policy: SemanticPolicy = SemanticPolicy()


Factory = Callable[[dict[str, Any], AdapterContext], Any]
Validator = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Adapter:
    name: str
    capability: str
    factory: Factory
    validate: Validator
    schema: dict[str, Any]


_ADAPTERS: dict[tuple[str, str], Adapter] = {}


def register_adapter(adapter: Adapter) -> None:
    """Register one complete adapter contract, including its configuration schema."""
    name, capability = adapter.name, adapter.capability
    if not name or capability not in {"llm", "asr", "tts", "embedding", "balance"}:
        raise ValueError("invalid adapter name or capability")
    if (name, capability) in _ADAPTERS:
        raise ValueError(f"adapter already registered: {name}/{capability}")
    if (
        not callable(adapter.factory)
        or not callable(adapter.validate)
        or not isinstance(adapter.schema, dict)
    ):
        raise ValueError("adapter requires a factory, validator and field schema")
    validate_schema(adapter.schema)
    _ADAPTERS[name, capability] = copy.deepcopy(adapter)


def adapter_schemas() -> list[dict[str, Any]]:
    return [
        {
            "adapter": name,
            "capability": capability,
            "fields": copy.deepcopy(adapter.schema),
        }
        for (name, capability), adapter in sorted(_ADAPTERS.items())
    ]


def adapter_definition(name: str, capability: str) -> Adapter:
    try:
        return _ADAPTERS[name, capability]
    except KeyError:
        raise ValueError(f"adapter {name!r} does not support {capability}") from None


class ServiceRegistry:
    """Lazily compose services; own pools and adapter lifetimes in one async scope."""

    def __init__(
        self,
        catalog: "ProviderCatalog",
        *,
        dump_dir: Path | None = None,
        semantic_policy: SemanticPolicy = SemanticPolicy(),
        overrides: dict[str, Any] | None = None,
    ):
        self.catalog = catalog
        self.transport = HTTPTransport()
        self.context = AdapterContext(self.transport, dump_dir, semantic_policy)
        self._instances = dict(overrides or {})
        self._owned: list[Any] = []
        self._stack: AsyncExitStack | None = None

    @property
    def embedding_config(self) -> EmbeddingSpaceConfig:
        encoder = self.embedding
        return encoder.space if encoder is not None else EmbeddingSpaceConfig()

    def get(self, capability: str):
        if capability in self._instances:
            return self._instances[capability]
        if not self.catalog.enabled(capability):
            return None
        if self._stack is not None:
            raise RuntimeError("Resolve services before entering the registry")
        binding = self.catalog.bindings[capability]
        adapter = adapter_definition(binding.adapter, capability)
        options = normalize_fields(adapter.schema, self.catalog.options_for(capability))
        adapter.validate(options)
        instance = adapter.factory(options, self.context)
        methods = {
            "llm": ("complete",),
            "asr": ("transcribe",),
            "tts": ("synthesize",),
            "embedding": ("encode", "health", "close"),
            "balance": ("balance",),
        }[capability]
        if any(not callable(getattr(instance, method, None)) for method in methods):
            raise TypeError(
                f"{binding.adapter}/{capability} must implement {', '.join(methods)}"
            )
        if capability == "llm":
            attributes = (
                "usage_sink",
                "thinking_sink",
                "usage_parser",
            )
            if any(not hasattr(instance, attribute) for attribute in attributes):
                raise TypeError(
                    f"{binding.adapter}/llm must supply the LanguageModel attributes"
                )
        if capability == "balance" and (
            not hasattr(instance, "accounting")
            or (instance.accounting is not None and any(
                not callable(getattr(instance.accounting, method, None))
                for method in ("parse_usage", "estimate_cost")
            ))
        ):
            raise TypeError(
                f"{binding.adapter}/balance must supply accounting as None or a usage accounting strategy"
            )
        if capability == "embedding" and not isinstance(
            getattr(instance, "space", None), EmbeddingSpaceConfig
        ):
            raise TypeError(
                f"{binding.adapter}/embedding must supply EmbeddingSpaceConfig as space"
            )
        if capability == "embedding" and not instance.space.enabled:
            raise ValueError(
                f"{binding.adapter}/embedding must declare an enabled space"
            )
        if capability == "asr" and (
            type(getattr(instance, "max_audio_bytes", None)) is not int
            or instance.max_audio_bytes <= 0
        ):
            raise TypeError(
                f"{binding.adapter}/asr must supply a positive max_audio_bytes"
            )
        self._instances[capability] = instance
        self._owned.append(instance)
        return instance

    @property
    def llm(self) -> LanguageModel:
        return self.get("llm")

    @property
    def asr(self) -> ASRProvider | None:
        return self.get("asr")

    @property
    def tts(self) -> TTSProvider | None:
        return self.get("tts")

    @property
    def embedding(self) -> Embedder | None:
        return self.get("embedding")

    @property
    def balance(self) -> BalanceProvider | None:
        return self.get("balance")

    async def __aenter__(self):
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(self.transport)
            for instance in self._owned:
                if hasattr(instance, "__aenter__"):
                    await stack.enter_async_context(instance)
                elif callable(getattr(instance, "close", None)):
                    stack.push_async_callback(self._close, instance)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    @staticmethod
    async def _close(instance):
        result = instance.close()
        if inspect.isawaitable(result):
            await result

    async def __aexit__(self, *exc):
        if self._stack is not None:
            await self._stack.__aexit__(*exc)
            self._stack = None


register_builtins()
