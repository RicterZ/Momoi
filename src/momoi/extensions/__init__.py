import importlib

from .base import UsagePlugin


def load_usage_plugin(provider: str, **kwargs: object) -> UsagePlugin:
    module_name, separator, class_name = provider.rpartition(".")
    if not separator or not module_name or not class_name:
        raise ValueError("usage.provider must be a dotted class name")
    module = importlib.import_module(module_name)
    plugin_cls = getattr(module, class_name, None)
    if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, UsagePlugin):
        raise TypeError(f"{provider} is not a UsagePlugin")
    return plugin_cls(**kwargs)


__all__ = ["UsagePlugin", "load_usage_plugin"]
