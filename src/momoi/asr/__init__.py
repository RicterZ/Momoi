import importlib

from .base import ASRError, ASRProvider, AudioInput


_BUILTIN_PROVIDERS = {
    "tencent": "momoi.asr.tencent.TencentASRProvider",
}


def load_asr_provider(provider: str, **kwargs: object) -> ASRProvider:
    qualified_name = _BUILTIN_PROVIDERS.get(provider, provider)
    module_name, separator, class_name = qualified_name.rpartition(".")
    if not separator or not module_name or not class_name:
        raise ValueError("asr.provider must be 'tencent' or a dotted class name")
    module = importlib.import_module(module_name)
    provider_cls = getattr(module, class_name, None)
    if not isinstance(provider_cls, type) or not issubclass(provider_cls, ASRProvider):
        raise TypeError(f"{qualified_name} is not an ASRProvider")
    return provider_cls(**kwargs)


__all__ = [
    "ASRError",
    "ASRProvider",
    "AudioInput",
    "load_asr_provider",
]
