"""Adapter option validation, run before any network resources are opened."""

import math
from dataclasses import asdict
from urllib.parse import urlsplit

from ..config.models import ConfigError
from .models import EmbeddingConfig, EmbeddingSpaceConfig, LLMConfig, ThinkingConfig


def fields(options, allowed):
    if unknown := set(options) - set(allowed):
        raise ConfigError(f"unknown provider option: {sorted(unknown)[0]}")


def text(options, key, default=None, *, empty=False):
    value = options.get(key, default)
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or any(c in value for c in "\r\n")
    ):
        raise ConfigError(
            f"{key} must be a {'single-line' if empty else 'nonempty single-line'} string"
        )
    return value.strip()


def number(options, key, default, *, minimum=0, integer=False, inclusive=False):
    value = options.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (integer and not isinstance(value, int))
        or (value < minimum if inclusive else value <= minimum)
    ):
        raise ConfigError(
            f"{key} must be {'an integer' if integer else 'finite'} and {'at least' if inclusive else 'greater than'} {minimum}"
        )
    return value


def url(options, key, default=None):
    value = text(options, key, default).rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{key} must be an absolute HTTP URL without credentials, query or fragment"
        )
    return value


def llm_config(options, api_format):
    fields(
        options,
        {
            "base_url",
            "api_key",
            "model",
            "max_tokens",
            "temperature",
            "timeout_seconds",
            "max_retries",
            "tool_choice",
            "thinking",
        },
    )
    thinking = options.get("thinking", {})
    if not isinstance(thinking, dict):
        raise ConfigError("thinking must be a mapping")
    fields(thinking, {"effort", "stages"})
    effort = thinking.get("effort", "")
    stages = thinking.get("stages", {})
    if effort not in {"", "low", "high", "max"}:
        raise ConfigError("thinking.effort must be low, high, or max")
    if not isinstance(stages, dict) or any(
        not isinstance(k, str) or not k.strip() or v not in {"low", "high", "max"}
        for k, v in stages.items()
    ):
        raise ConfigError(
            "thinking.stages must map nonempty stages to low, high, or max"
        )
    choice = options.get("tool_choice", True)
    if type(choice) is not bool:
        raise ConfigError("tool_choice must be boolean")
    return LLMConfig(
        base_url=url(
            options,
            "base_url",
            "https://api.deepseek.com" if api_format == "deepseek" else None,
        ),
        api_key=text(options, "api_key", "", empty=True),
        model=text(options, "model"),
        max_tokens=number(options, "max_tokens", 16384, integer=True),
        temperature=number(options, "temperature", 0.6, inclusive=True),
        timeout_seconds=number(options, "timeout_seconds", 300),
        max_retries=number(options, "max_retries", 3, integer=True, inclusive=True),
        api_format="anthropic" if api_format == "anthropic" else "openai",
        tool_choice=choice,
        thinking=ThinkingConfig(effort, dict(stages)),
    )


def embedding_config(options, *, enabled=True):
    values = dict(options)
    if "base_url" in values:
        base = url(values, "base_url")
        values.setdefault(
            "endpoint",
            base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings"),
        )
        del values["base_url"]
    if "timeout_seconds" in values:
        timeout = number(values, "timeout_seconds", 30)
        values.setdefault("query_timeout_seconds", timeout)
        values.setdefault("document_timeout_seconds", timeout)
        del values["timeout_seconds"]
    return EmbeddingConfig(
        **asdict(embedding_space_config(values, enabled=enabled)),
        endpoint=url(values, "endpoint", EmbeddingConfig().endpoint),
        api_key=text(values, "api_key", "", empty=True),
        query_timeout_seconds=number(values, "query_timeout_seconds", 5),
        document_timeout_seconds=number(values, "document_timeout_seconds", 30),
    )


def embedding_space_config(options, *, enabled):
    """Metadata consumed by recall, independent of the encoder's transport."""
    defaults = EmbeddingSpaceConfig()
    if not enabled:
        return defaults
    return EmbeddingSpaceConfig(
        enabled=True,
        model=text(options, "model", defaults.model),
        dimensions=number(options, "dimensions", defaults.dimensions, integer=True),
        calibration_profile=text(
            options, "calibration_profile", defaults.calibration_profile
        ),
        document_batch_size=number(
            options, "document_batch_size", defaults.document_batch_size, integer=True
        ),
    )
