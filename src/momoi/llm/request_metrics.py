"""Per-attempt transport metrics. Fingerprints contain no prompt text or credentials."""
import hashlib
import json
import logging
from contextvars import ContextVar
from time import monotonic, time
from uuid import uuid4

from ..observability.context import current_log_context
from ..memory.text import estimate_tokens
from ..storage.contracts import PromptFingerprint, RequestShape

_active = ContextVar("llm_request_metric", default=None)
logger = logging.getLogger(__name__)


def fingerprint(value) -> PromptFingerprint:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"hash": hashlib.sha256(encoded.encode()).hexdigest(), "tokens_est": estimate_tokens(encoded)}


def request_shape(payload) -> RequestShape:
    messages = payload.get("messages", [])
    system = payload.get("system", [])
    if messages and messages[0].get("role") == "system":
        system = messages[0]
        messages = messages[1:]
    settings = {k: v for k, v in payload.items() if k not in {"messages", "system", "tools"}}
    parts = [fingerprint(payload.get("tools", [])), fingerprint(system)]
    parts.extend(fingerprint(message) for message in messages)
    return {"settings_hash": fingerprint(settings)["hash"],
            "settings_fields": {key: fingerprint(value)["hash"] for key, value in settings.items()}, "parts": parts,
            "tool_parts": [fingerprint(tool) for tool in payload.get("tools", [])],
            "message_count": len(messages), "input_tokens_est": sum(p["tokens_est"] for p in parts)}


def response_headers(status):
    record = _active.get()
    if record is not None:
        record["first_response_ms"] = round((monotonic() - record["started"]) * 1000, 2)
        record["http_status"] = status


def response_usage(metrics):
    record = _active.get()
    if record is not None:
        record["usage"] = metrics


class RequestMetric:
    def __init__(self, sink, *, payload, protocol, endpoint):
        self.sink = sink
        self.model = payload.get("model", "")
        self.protocol = protocol
        self.route = fingerprint([protocol, endpoint, self.model])["hash"]
        self.shape = request_shape(payload) if sink else {}
        self.request_id = uuid4().hex

    def begin(self, attempt):
        record = dict(current_log_context())
        record.update(created_at=time(), started=monotonic(), attempt=attempt,
                      request_id=self.request_id, model=self.model, protocol=self.protocol,
                      route=self.route, shape=self.shape, status="success", usage=None,
                      first_response_ms=None, http_status=None, error_type=None)
        return record, _active.set(record)

    def finish(self, record, token):
        _active.reset(token)
        record["duration_ms"] = round((monotonic() - record.pop("started")) * 1000, 2)
        if self.sink:
            try:
                self.sink(record)
            except Exception:
                logger.warning("llm_request_metric_record_failed", exc_info=True)
