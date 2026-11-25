import asyncio
import json
import random

import pytest

from momoi.config.heartbeat_activities import (
    activities_path,
    activity_catalog,
    load_activities,
    parse_activities,
    save_activities,
    select_activity,
)
from momoi.dashboard.settings import DashboardSettings, PromptFile
from momoi.models import ToolCall
from momoi.runtime.agent.runtime_tools import begin_heartbeat
from momoi.runtime.tool_contracts.context import heartbeat_begin_spec


def activities(*entries):
    return parse_activities([
        {"id": activity_id, "title": f"标题 {activity_id}", "detail": f"详情 {activity_id}", "weight": weight}
        for activity_id, weight in entries
    ])


def begin(arguments, catalog=(), started=False):
    return asyncio.run(begin_heartbeat(
        ToolCall("begin", "heartbeat_begin", arguments),
        heartbeat_turn=True, harness_started=started, activities=catalog,
    ))


@pytest.mark.parametrize("entry, message", [
    ({"id": "Bad Id", "title": "t", "detail": "d", "weight": 1}, "invalid id"),
    ({"id": "a", "title": "", "detail": "d", "weight": 1}, "title"),
    ({"id": "a", "title": "t", "detail": "", "weight": 1}, "detail"),
    ({"id": "a", "title": "t", "detail": "d", "weight": -1}, "weight"),
    ({"id": "a", "title": "t", "detail": "d", "weight": 1.5}, "weight"),
    ({"id": "a", "title": "t", "detail": "d", "weight": True}, "weight"),
])
def test_parse_rejects_invalid_entries(entry, message):
    with pytest.raises(ValueError, match=message):
        parse_activities([entry])


def test_parse_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate"):
        activities(("a", 1), ("a", 2))


def test_weighted_draw_follows_configured_weights():
    catalog = activities(("weibo", 3), ("news", 1), ("manual", 0))
    rng = random.Random(7)
    counts = {"weibo": 0, "news": 0, "manual": 0}
    for _ in range(4000):
        selected, error = select_activity(catalog, "random", rng=rng)
        assert error is None
        counts[selected.id] += 1
    assert counts["manual"] == 0
    assert 0.72 < counts["weibo"] / 4000 < 0.78


def test_zero_weight_activity_can_still_be_picked():
    selected, error = select_activity(activities(("manual", 0)), "pick", "manual")
    assert error is None and selected.id == "manual"


def test_random_without_positive_weight_fails_explicitly():
    assert select_activity((), "random") == (None, "no_weighted_heartbeat_activity")
    assert select_activity(activities(("a", 0)), "random") == (None, "no_weighted_heartbeat_activity")
    result = begin({"choice": "random"}, activities(("a", 0)))
    assert result["ok"] is False and result["error"] == "no_weighted_heartbeat_activity"


def test_begin_returns_detail_for_pick_and_random():
    catalog = activities(("weibo", 1))
    picked = begin({"choice": "pick", "activity_id": "weibo"}, catalog)
    assert picked["activity"] == {"id": "weibo", "title": "标题 weibo", "detail": "详情 weibo"}
    drawn = begin({"choice": "random"}, catalog)
    assert drawn["activity"]["detail"] == "详情 weibo" and drawn["draw"] == {"weibo": 1}


@pytest.mark.parametrize("arguments", [
    {"choice": "pick"},
    {"choice": "pick", "activity_id": "missing"},
    {"choice": "random", "activity_id": "weibo"},
    {"choice": "custom"},
    {"choice": "custom", "activity": "  "},
    {"choice": "rest", "activity": "nap"},
    {"choice": "other"},
])
def test_begin_rejects_mismatched_arguments(arguments):
    assert begin(arguments, activities(("weibo", 1)))["ok"] is False


def test_begin_custom_and_rest_need_no_catalog():
    assert begin({"choice": "custom", "activity": "整理 灵感"}) == {
        "ok": True, "state": "started", "choice": "custom", "activity": {"title": "整理 灵感"},
    }
    assert begin({"choice": "rest"})["ok"] is True


def test_begin_cannot_run_twice():
    assert begin({"choice": "rest"}, started=True)["error"] == "invalid_heartbeat_begin"


def test_schema_does_not_embed_activity_ids():
    schema = heartbeat_begin_spec()["input_schema"]
    assert "enum" not in schema["properties"]["activity_id"]


def test_catalog_lists_ids_and_titles_only():
    text = activity_catalog(activities(("weibo", 2)))
    assert "weibo：标题 weibo" in text and "详情" not in text and "2" not in text
    assert "random 不可用" in activity_catalog(activities(("a", 0)))
    assert "custom 或 rest" in activity_catalog(())


def test_dashboard_round_trip(tmp_path):
    heartbeat = tmp_path / "HEARTBEAT.md"
    settings = DashboardSettings((PromptFile("heartbeat", heartbeat, False),))
    assert settings.heartbeat_activities() == []
    saved = settings.update_heartbeat_activities([
        {"id": "weibo", "title": "刷微博", "detail": "看首页时间线", "weight": 3},
    ])
    assert saved == [{"id": "weibo", "title": "刷微博", "detail": "看首页时间线", "weight": 3}]
    stored = json.loads(activities_path(heartbeat).read_text(encoding="utf-8"))
    assert stored["activities"][0]["id"] == "weibo"
    with pytest.raises(ValueError):
        settings.update_heartbeat_activities([{"id": "x", "title": "t", "detail": "d", "weight": 999}])
    assert len(load_activities(activities_path(heartbeat))) == 1


def test_save_and_load_preserve_order(tmp_path):
    path = tmp_path / "HEARTBEAT_ACTIVITIES.json"
    catalog = activities(("b", 1), ("a", 2))
    save_activities(path, catalog)
    assert load_activities(path) == catalog
