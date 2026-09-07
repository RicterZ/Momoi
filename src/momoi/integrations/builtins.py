"""Builtin adapter factories, separate from the extensible registry."""

from .validation import embedding_config, fields, llm_config, number, text, url
from .schema import builtin_schema


def register_builtins():
    from .registry import Adapter, register_adapter
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
            Adapter(
                name,
                "llm",
                lambda options, ctx, name=name, cls=cls: llm_factory(
                    options, ctx, name, cls
                ),
                validate=lambda options, name=name: llm_config(options, name),
                schema=builtin_schema(name, "llm"),
            )
        )

    def validate_embedding(options):
        fields(
            options,
            {
                "endpoint",
                "api_key",
                "model",
                "dimensions",
                "query_timeout_seconds",
                "document_timeout_seconds",
            },
        )
        embedding_config(options)

    register_adapter(
        Adapter(
            "openai",
            "embedding",
            lambda options, ctx: EmbeddingClient(
                embedding_config(options), ctx.semantic_policy
            ),
            validate=validate_embedding,
            schema=builtin_schema("openai", "embedding"),
        )
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
        Adapter(
            "tencent",
            "asr",
            lambda options, ctx: TencentASRProvider(
                **options,
                transport=ctx.transport,
            ),
            validate=validate_asr,
            schema=builtin_schema("tencent", "asr"),
        )
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
        Adapter(
            "fish",
            "tts",
            lambda options, ctx: FishAudioTTSProvider(
                **options, transport=ctx.transport
            ),
            validate=validate_tts,
            schema=builtin_schema("fish", "tts"),
        )
    )

    def validate_balance(options):
        fields(options, {"api_key", "base_url", "timeout_seconds"})
        text(options, "api_key")
        url(options, "base_url", "https://api.deepseek.com")
        number(options, "timeout_seconds", 10)

    register_adapter(
        Adapter(
            "deepseek",
            "balance",
            lambda options, ctx: DeepSeekBalanceProvider(
                **options, transport=ctx.transport
            ),
            validate=validate_balance,
            schema=builtin_schema("deepseek", "balance"),
        )
    )
