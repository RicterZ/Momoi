"""Capability factories and resource ownership at application composition boundaries."""

import inspect
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from .contracts.asr import ASRProvider
from .contracts.balance import BalanceProvider
from .contracts.embedding import Embedder
from .contracts.llm import LanguageModel
from .contracts.tts import TTSProvider
from .transport import HTTPTransport
from .validation import (
    embedding_space_config,
    embedding_config,
    fields,
    llm_config,
    number,
    text,
    url,
)
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
    factory: Factory
    validate: Validator


_ADAPTERS: dict[tuple[str, str], Adapter] = {}


def register_adapter(
    name: str, capability: str, factory: Factory, *, validate: Validator
) -> None:
    """Register a factory and offline validator, once per capability, during startup."""
    if not name or capability not in {"llm", "asr", "tts", "embedding", "balance"}:
        raise ValueError("invalid adapter name or capability")
    if (name, capability) in _ADAPTERS:
        raise ValueError(f"adapter already registered: {name}/{capability}")
    _ADAPTERS[name, capability] = Adapter(factory, validate)


def adapter_definition(name: str, capability: str) -> Adapter:
    try:
        return _ADAPTERS[name, capability]
    except KeyError:
        raise ValueError(f"adapter {name!r} does not support {capability}") from None


def _builtins():
    from .adapters.tencent import TencentASRProvider
    from .adapters.fish import FishAudioTTSProvider
    from .adapters.embedding import EmbeddingClient
    from .adapters.deepseek import DeepSeekBalanceProvider
    from .adapters.openai import OpenAIProvider
    from .adapters.anthropic import AnthropicProvider

    def llm_factory(options, ctx, name, cls):
        instance = cls(llm_config(options, name), ctx.dump_dir)
        if name == "deepseek":
            from .adapters.deepseek import DeepSeekAccounting

            instance.accounting = DeepSeekAccounting()
        return instance

    for name, cls in [
        ("openai", OpenAIProvider),
        ("deepseek", OpenAIProvider),
        ("anthropic", AnthropicProvider),
    ]:
        register_adapter(
            name,
            "llm",
            lambda options, ctx, name=name, cls=cls: llm_factory(
                options, ctx, name, cls
            ),
            validate=lambda options, name=name: llm_config(options, name),
        )

    def validate_embedding(options):
        fields(
            options,
            {
                "base_url",
                "timeout_seconds",
                "endpoint",
                "api_key",
                "model",
                "dimensions",
                "calibration_profile",
                "query_timeout_seconds",
                "document_timeout_seconds",
                "document_batch_size",
            },
        )
        embedding_config(options)

    register_adapter(
        "openai",
        "embedding",
        lambda options, ctx: EmbeddingClient(
            embedding_config(options), ctx.semantic_policy
        ),
        validate=validate_embedding,
    )

    def validate_asr(options):
        fields(
            options,
            {
                "secret_id",
                "secret_key",
                "region",
                "engine",
                "timeout_seconds",
                "max_audio_bytes",
            },
        )
        for key in ("secret_id", "secret_key"):
            text(options, key)
        for key, default in [("region", ""), ("engine", "16k_zh")]:
            text(options, key, default, empty=key == "region")
        number(options, "timeout_seconds", 30)
        number(options, "max_audio_bytes", 3 * 1024 * 1024, integer=True)

    register_adapter(
        "tencent",
        "asr",
        lambda options, ctx: TencentASRProvider(
            **{k: v for k, v in options.items() if k != "max_audio_bytes"},
            transport=ctx.transport,
        ),
        validate=validate_asr,
    )

    def validate_tts(options):
        fields(
            options,
            {
                "api_key",
                "reference_id",
                "model",
                "base_url",
                "format",
                "latency",
                "timeout_seconds",
                "max_audio_bytes",
            },
        )
        FishAudioTTSProvider(**options)

    register_adapter(
        "fish",
        "tts",
        lambda options, ctx: FishAudioTTSProvider(**options, transport=ctx.transport),
        validate=validate_tts,
    )

    def validate_balance(options):
        fields(options, {"api_key", "base_url", "timeout_seconds"})
        text(options, "api_key")
        url(options, "base_url", "https://api.deepseek.com")
        number(options, "timeout_seconds", 10)

    register_adapter(
        "deepseek",
        "balance",
        lambda options, ctx: DeepSeekBalanceProvider(
            **options, transport=ctx.transport
        ),
        validate=validate_balance,
    )


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
        self.embedding_config = embedding_space_config(
            catalog.options_for("embedding"), enabled=catalog.enabled("embedding")
        )

    def get(self, capability: str):
        if capability in self._instances:
            return self._instances[capability]
        if not self.catalog.enabled(capability):
            return None
        if self._stack is not None:
            raise RuntimeError("Resolve services before entering the registry")
        binding = self.catalog.bindings[capability]
        adapter = adapter_definition(binding.adapter, capability)
        options = self.catalog.options_for(capability)
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
                "config",
                "accounting",
                "usage_sink",
                "thinking_sink",
                "usage_parser",
            )
            if any(not hasattr(instance, attribute) for attribute in attributes):
                raise TypeError(
                    f"{binding.adapter}/llm must supply the LanguageModel attributes"
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


_builtins()
