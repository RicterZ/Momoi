import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from momoi.integrations.adapters.openai import OpenAIProvider
from momoi.integrations.adapters.anthropic import AnthropicProvider
from momoi.integrations.models import LLMConfig
from momoi.llm.request_metrics import RequestMetric, request_shape
from momoi.llm.transport import retry_request
from momoi.observability.context import log_context
from momoi.storage import Store


def metric(*, stage="owner", hit=9000, status="success", reported=True, payload=None):
    return dict(created_at=time.time(), request_id="request", attempt=1, route="route", stage=stage,
                model="model", status=status, duration_ms=20, first_response_ms=10,
                shape=request_shape(payload or {"model": "model", "messages": [{"role": "system", "content": "stable " * 5000}, {"role": "user", "content": "hi"}]}),
                usage={"input": 10000, "output": 100, "cache_read": hit, "uncached": 10000-hit, "cache_reported": reported})


def test_metrics_reuse_unknown_cache_filter_pagination_and_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    store.record_request_metric(metric())
    store.record_request_metric(metric(stage="heartbeat", hit=100))
    store.record_request_metric(metric(stage="goal", hit=0, reported=False))
    data = store.dashboard_request_metrics(limit=1)
    assert data["totals"]["requests"] == 3
    assert data["totals"]["cache_hit_rate"] == .455
    assert data["totals"]["cache_reported_requests"] == 2
    assert data["totals"]["alerts"] == 1
    page = store.dashboard_request_metrics(before=data["next_cursor"], limit=1)
    assert page["items"][0]["stage"] == "heartbeat"
    assert page["items"][0]["cache_alert"]
    assert page["items"][0]["compared_stage"] == "owner"
    assert "shape" not in page["items"][0]
    assert store.dashboard_request_metrics(stage="goal")["totals"]["cache_hit_rate"] is None
    assert store.dashboard_request_metrics(model="absent")["totals"]["requests"] == 0
    store.close()
    store = Store(path)
    assert store.dashboard_request_metrics()["totals"]["requests"] == 3
    store.close()


def test_request_shape_detects_settings_and_prefix_changes(tmp_path):
    from momoi.storage.ops.request_metrics import compare_shapes
    a = request_shape({"model": "x", "messages": [{"role": "user", "content": "hello"}]})
    b = request_shape({"model": "x", "messages": [{"role": "user", "content": "bye"}]})
    assert compare_shapes(a, b)[1] == "user[0]"
    b = request_shape({"model": "x", "tool_choice": "required", "messages": []})
    assert compare_shapes(a, b)[0] > 0
    assert a["settings_hash"] != b["settings_hash"]


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_http_attempt_records_success_failure_missing_usage_and_headers(protocol):
    async def run():
        records = []
        attempts = 0
        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.json_response({"error": {"message": "bad"}}, status=400)
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "ok"}}]} if protocol == "openai" else {"content": [{"type": "text", "text": "ok"}]})
        app = web.Application()
        app.router.add_post("/v1/chat/completions" if protocol == "openai" else "/v1/messages", handler)
        server = TestServer(app)
        await server.start_server()
        provider = (OpenAIProvider if protocol == "openai" else AnthropicProvider)(LLMConfig(str(server.make_url("/")).rstrip("/"), "secret", "test", 100, 0, 1, 0, "openai"))
        provider.request_metrics_sink = records.append
        # Response metrics are tested here; cooldown has its own clock-based tests.
        from unittest.mock import AsyncMock
        provider._circuit.wait = AsyncMock()
        try:
            async with provider:
                with log_context(stage="owner", turn_id="turn", call_id="call", round=2):
                    with pytest.raises(Exception):
                        await provider.complete("private prompt", [{"role": "user", "content": "hello"}])
                    await provider.complete("private prompt", [{"role": "user", "content": "hello"}])
        finally:
            await server.close()
        assert [r["status"] for r in records] == ["error", "success"]
        assert [r["http_status"] for r in records] == [400, 200]
        assert records[1]["usage"] is None
        assert records[1]["call_id"] == "call"
        assert records[1]["round"] == 2
        assert 0 <= records[1]["first_response_ms"] <= records[1]["duration_ms"]
        assert "private prompt" not in str(records)
        assert "secret" not in str(records)
    asyncio.run(run())


def test_cancellation_is_recorded_and_propagates():
    async def run():
        records = []
        async def operation(started):
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await retry_request(protocol="test", max_retries=0, request_fields={}, operation=operation,
                                monitor=RequestMetric(records.append, payload={}, protocol="test", endpoint="local"))
        assert records[0]["status"] == "cancelled"
        assert records[0]["first_response_ms"] is None
    asyncio.run(run())


def test_retries_share_request_id_but_record_each_attempt(monkeypatch):
    from types import SimpleNamespace
    from momoi.llm.errors import ProviderResponseError
    from momoi.models import ProviderResponse
    from momoi.llm import transport
    async def run():
        records = []
        calls = 0
        async def sleep(delay):
            await asyncio.sleep(0)
        monkeypatch.setattr(transport, "asyncio", SimpleNamespace(sleep=sleep, TimeoutError=asyncio.TimeoutError))
        async def operation(started):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderResponseError("unusable")
            return ProviderResponse([], [])
        await retry_request(protocol="test", max_retries=1, request_fields={}, operation=operation,
                            monitor=RequestMetric(records.append, payload={}, protocol="test", endpoint="local"))
        assert [r["attempt"] for r in records] == [1, 2]
        assert [r["status"] for r in records] == ["error", "success"]
        assert records[0]["request_id"] == records[1]["request_id"]
    asyncio.run(run())


def test_effort_change_preserves_prefix_reuse_and_flags_parameter(tmp_path):
    store = Store(tmp_path / "db")
    payload = {"model": "model", "messages": [{"role": "user", "content": "stable " * 5000}], "reasoning_effort": "low"}
    store.record_request_metric(metric(payload=payload))
    store.record_request_metric(metric(payload={**payload, "reasoning_effort": "high"}, hit=100))
    row = store.dashboard_request_metrics()["items"][0]
    assert row["reuse_ratio_est"] == 1
    assert row["settings_changed"] is True
    assert row["changed_settings"] == ["reasoning_effort"]
    assert row["cache_alert"] is True
    store.close()


def test_tool_change_counts_shared_tools_and_reports_unchanged_system(tmp_path):
    from momoi.storage.ops.request_metrics import compare_shapes
    messages = [{"role": "system", "content": "stable system"},
                {"role": "user", "content": "stable memory"}]
    a = {"tools": [{"name": "first"}, {"name": "second"}], "messages": messages}
    b = {**a, "tools": [*a["tools"], {"name": "third"}]}
    before, after = request_shape(a), request_shape(b)
    prefix, boundary = compare_shapes(after, before)
    assert boundary == "tools[2]"
    assert prefix == sum(t["tokens_est"] for t in before["tool_parts"])
    store = Store(tmp_path / "metrics.sqlite3")
    store.record_request_metric(metric(payload=a))
    store.record_request_metric(metric(payload=b))
    row = store.dashboard_request_metrics()["items"][0]
    assert row["system_unchanged"]
    assert row["common_message_prefix_count"] == 1
    assert row["tool_comparison_granularity"] == "item"
    assert row["reuse_ratio_est"] > 0
    del before["tool_parts"]
    assert compare_shapes(after, before) == (0, "tools")
    store.close()


def test_first_tool_latency_includes_cancelled_request_and_records_once(tmp_path, monkeypatch):
    from momoi.storage.ops import request_metrics
    store = Store(tmp_path / 'metrics.db')
    start = time.time()
    store._db.execute("INSERT INTO turns(id,kind,source_ids_json,state,started_at,updated_at) VALUES(?,?,?,?,?,?)",
                      ('turn', 'owner', '[]', 'running', start, start))
    store._db.commit()
    first = metric(status='cancelled')
    first.update(turn_id='turn', call_id='cancelled')
    store.record_request_metric(first)
    second = metric()
    second.update(turn_id='turn', call_id='send')
    store.record_request_metric(second)
    monkeypatch.setattr(request_metrics.time, 'time', lambda: start + 220)
    store.record_first_tool('turn', 'send', 'send_bubbles')
    monkeypatch.setattr(request_metrics.time, 'time', lambda: start + 225)
    store.record_first_tool('turn', 'send', 'end_turn')
    data = store.dashboard_request_metrics()
    assert data['totals']['first_tool_ms'] == pytest.approx(220000)
    assert data['stages'][0]['first_tool_ms'] == pytest.approx(220000)
    assert data['items'][0]['first_tool_name'] == 'send_bubbles'
    assert 'first_tool_ms' not in data['items'][1]
    store.close()


def test_per_request_cost_uses_usage_buckets_and_respects_disabled_accounting(tmp_path):
    from momoi.integrations.adapters.deepseek import DeepSeekAccounting

    store = Store(tmp_path / 'db')
    record = metric(hit=9000)
    record['model'] = 'deepseek-v4-flash'
    record['usage']['cache_write'] = 50
    store.record_request_metric(record)
    missing = metric()
    missing['usage'] = None
    store.record_request_metric(missing)
    assert all(row['estimated_cost'] is None for row in store.dashboard_request_metrics()['items'])
    store.set_usage_accounting(DeepSeekAccounting())
    rows = store.dashboard_request_metrics()['items']
    assert store.dashboard_request_metrics()['cost_available'] is True
    assert rows[0]['estimated_cost'] is None
    expected = DeepSeekAccounting().estimate_cost(
        record['model'], record['created_at'], cache_read=9000,
        uncached=1000, cache_write=50, output=100)
    assert rows[1]['estimated_cost'] == pytest.approx(expected)
    store.set_usage_accounting(None)
    assert store.dashboard_request_metrics()['items'][1]['estimated_cost'] is None
    store.close()
