import json
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")
_SENSITIVE_KEY = re.compile(
    r"^(?:(?:x[_-]?)?api[_-]?key|authorization|cookie|credentials?|password|"
    r"secret|token|access[_-]?token|refresh[_-]?token|private[_-]?key)$",
    re.IGNORECASE,
)
_URL_KEY = re.compile(r"(?:url|uri|endpoint)$", re.IGNORECASE)


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return "[redacted]"
    if key and _URL_KEY.search(key) and isinstance(value, str):
        try:
            parsed = urlsplit(value)
            query = urlencode(
                [
                    (
                        item_key,
                        "[redacted]" if _SENSITIVE_KEY.search(item_key) else item_value,
                    )
                    for item_key, item_value in parse_qsl(
                        parsed.query, keep_blank_values=True
                    )
                ]
            )
            host = parsed.hostname or ""
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            netloc = f"[redacted]@{host}" if parsed.username else parsed.netloc
            return urlunsplit(
                (parsed.scheme, netloc, parsed.path, query, parsed.fragment)
            )
        except (TypeError, ValueError):
            return "[redacted-url]"
    if isinstance(value, Mapping):
        if value.get("type") == "base64" and isinstance(value.get("data"), str):
            return {
                str(item_key): (
                    f"[omitted {len(str(item_value))} base64 chars]"
                    if item_key == "data"
                    else _redact(item_value, str(item_key))
                )
                for item_key, item_value in value.items()
            }
        return {
            str(item_key): _redact(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value:
        prefix, encoded = value.split(",", 1)
        return f"{prefix},[omitted {len(encoded)} base64 chars]"
    return value


def safe_preview(value: Any, limit: int = 1000) -> str:
    redacted = _redact(value)
    if isinstance(redacted, str):
        rendered = redacted.replace("\r", "\\r").replace("\n", "\\n")
    else:
        try:
            rendered = json.dumps(
                redacted,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            rendered = repr(redacted)
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 3)].rstrip() + "..."


def compact_log_value(
    value: Any,
    *,
    string_limit: int = 500,
    item_limit: int = 20,
) -> Any:
    value = _redact(value)
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value.replace("\r", "\\r").replace("\n", "\\n")
        return (
            value[: max(0, string_limit - 3)]
            .rstrip()
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            + "..."
        )
    if isinstance(value, Mapping):
        items = list(value.items())
        compact = {
            str(key): compact_log_value(
                item,
                string_limit=string_limit,
                item_limit=item_limit,
            )
            for key, item in items[:item_limit]
        }
        if len(items) > item_limit:
            compact["_omitted"] = len(items) - item_limit
        return compact
    if isinstance(value, (list, tuple)):
        compact = [
            compact_log_value(
                item,
                string_limit=string_limit,
                item_limit=item_limit,
            )
            for item in value[:item_limit]
        ]
        if len(value) > item_limit:
            compact.append(f"... {len(value) - item_limit} more")
        return compact
    return value


def format_log_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            compact_log_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    rendered = safe_preview(value)
    if _SAFE_TOKEN.fullmatch(rendered):
        return rendered
    return json.dumps(rendered, ensure_ascii=False)
