import copy
import json

import pytest

from momoi.llm.dumps import dump_request, dump_response, redact_dump_media
from momoi.observability.events import TRACE
from momoi.observability.values import compact_log_value


@pytest.mark.parametrize("properties", [
    {"optional": {"type": ["string", "null"]}},
    {"type": {"type": "string"}},
])
def test_trace_dump_accepts_schema_types_and_still_redacts_media(tmp_path, caplog, properties):
    caplog.set_level(TRACE, logger="momoi.llm.dumps")
    payload = {
        "tools": [{"input_schema": {
            "type": "object",
            "properties": properties,
        }}],
        "content": [
            {"type": "image", "data": "private-image"},
            {"type": "base64", "data": "private-image"},
        ],
    }
    original = copy.deepcopy(payload)
    for redact in (redact_dump_media, compact_log_value):
        safe = redact(payload)
        assert safe["tools"] == payload["tools"]
        assert "private-image" not in str(safe)
        assert all(block["data"].startswith("[omitted ") for block in safe["content"])
    path = dump_request(tmp_path, "anthropic", payload, False)
    assert path is not None
    dumped = json.loads(path.read_text())
    assert dumped["payload"]["tools"] == payload["tools"]
    assert "private-image" not in path.read_text()
    dump_response(path, payload)
    dumped = json.loads(path.read_text())
    assert dumped["response"]["tools"] == payload["tools"]
    assert "private-image" not in path.read_text()
    assert "llm_dump_failed" not in caplog.text
    assert payload == original
