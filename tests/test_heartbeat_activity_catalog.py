import asyncio
import json
import random

import pytest

from momoi.config.heartbeat_activities import (
    REST,
    HeartbeatPlan,
    activities_path,
    activity_catalog,
    load_plan,
    parse_plan,
    save_plan,
    select_activity,
)
from momoi.dashboard.settings import DashboardSettings, PromptFile
from momoi.models import ToolCall
from momoi.runtime.agent.runtime_tools import begin_heartbeat
from momoi.runtime.tool_contracts.context import heartbeat_begin_spec


def plan(*entries, rest=0):
    return parse_plan({
        "activities": [
            {"id": activity_id, "title": f"标题 {activity_id}", "detail": f"详情 {activity_id}", "probability": probability}
            for activity_id, probability in entries
        ],
        "rest_probability": rest,
    })


def begin(arguments, heartbeat_plan=HeartbeatPlan(), started=False):
    return asyncio.run(begin_heartbeat(
        ToolCall("begin", "heartbeat_begin", arguments),
        heartbeat_turn=True, harness_started=started, plan=heartbeat_plan,
    ))


@pytest.mark.parametrize("entry, message", [
    ({"id": "Bad Id", "title": "t", "detail": "d", "probability": 100}, "invalid id"),
    ({"id": "rest", "title": "t", "detail": "d", "probability": 100}, "invalid id"),
    ({"id": "a", "title": "", "detail": "d", "probability": 100}, "title"),
    ({"id": "a", "title": "t", "detail": "", "probability": 100}, "detail"),
    ({"id": "a", "title": "t", "detail": "d", "probability": -1}, "probability"),
    ({"id": "a", "title": "t", "detail": "d", "probability": 1.5}, "probability"),
    ({"id": "a", "title": "t", "detail": "d", "probability": True}, "probability"),
])
def test_parse_rejects_invalid_entries(entry, message):
    with pytest.raises(ValueError, match=message):
        parse_plan({"activities": [entry]})


@pytest.mark.parametrize("rest", [-1, 101, 1.5, True, "10"])
def test_parse_rejects_invalid_rest_probability(rest):
    with pytest.raises(ValueError, match="rest probability"):
        parse_plan({"activities": [], "rest_probability": rest})


def test_parse_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate"):
        plan(("a", 50), ("a", 50))


@pytest.mark.parametrize("probabilities, rest", [((60, 30), 0), ((60, 30), 20), ((100,), 1), ((), 50)])
def test_probabilities_including_rest_must_total_exactly_100(probabilities, rest):
    with pytest.raises(ValueError, match="add up to 100"):
        plan(*((f"a{index}", value) for index, value in enumerate(probabilities)), rest=rest)


def test_rest_counts_toward_the_total():
    assert plan(("a", 60), rest=40).rest_probability == 40
    assert plan(rest=100).random_enabled


def test_all_zero_probabilities_mean_manual_only():
    assert not plan(("a", 0), ("b", 0)).random_enabled


def test_draw_follows_allocated_probabilities_including_rest():
    heartbeat_plan = plan(("weibo", 50), ("news", 25), ("manual", 0), rest=25)
    rng = random.Random(7)
    counts = {"weibo": 0, "news": 0, "manual": 0, REST: 0}
    for _ in range(4000):
        selected, error = select_activity(heartbeat_plan, "random", rng=rng)
        assert error is None
        counts[selected if selected == REST else selected.id] += 1
    assert counts["manual"] == 0
    assert 0.47 < counts["weibo"] / 4000 < 0.53
    assert 0.22 < counts[REST] / 4000 < 0.28


def test_zero_probability_activity_can_still_be_picked():
    selected, error = select_activity(plan(("manual", 0)), "pick", "manual")
    assert error is None and selected.id == "manual"


def test_random_without_probabilities_fails_explicitly():
    assert select_activity(HeartbeatPlan(), "random") == (None, "random_heartbeat_disabled")
    assert select_activity(plan(("a", 0)), "random") == (None, "random_heartbeat_disabled")
    result = begin({"choice": "random"}, plan(("a", 0)))
    assert result["ok"] is False and result["error"] == "random_heartbeat_disabled"


def test_begin_returns_detail_for_pick_and_random():
    heartbeat_plan = plan(("weibo", 100))
    picked = begin({"choice": "pick", "activity_id": "weibo"}, heartbeat_plan)
    assert picked["activity"] == {"id": "weibo", "title": "标题 weibo", "detail": "详情 weibo"}
    drawn = begin({"choice": "random"}, heartbeat_plan)
    assert drawn["activity"]["detail"] == "详情 weibo" and drawn["probabilities"] == {"weibo": 100}


def test_random_can_draw_rest():
    drawn = begin({"choice": "random"}, plan(("weibo", 0), rest=100))
    assert drawn == {"ok": True, "state": "started", "choice": "random", "outcome": "rest", "probabilities": {"rest": 100}}


@pytest.mark.parametrize("arguments", [
    {"choice": "pick"},
    {"choice": "pick", "activity_id": "missing"},
    {"choice": "pick", "activity_id": "rest"},
    {"choice": "random", "activity_id": "weibo"},
    {"choice": "custom"},
    {"choice": "custom", "activity": "  "},
    {"choice": "rest", "activity": "nap"},
    {"choice": "other"},
])
def test_begin_rejects_mismatched_arguments(arguments):
    assert begin(arguments, plan(("weibo", 100)))["ok"] is False


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
    text = activity_catalog(plan(("weibo", 60), rest=40))
    assert "weibo：标题 weibo" in text and "详情" not in text and "60" not in text and "40" not in text
    assert "random 不可用" in activity_catalog(plan(("a", 0)))
    assert "pick 不可用" in activity_catalog(plan(rest=100))
    assert "random 不可用" not in activity_catalog(plan(rest=100))


def test_dashboard_round_trip(tmp_path):
    heartbeat = tmp_path / "HEARTBEAT.md"
    settings = DashboardSettings((PromptFile("heartbeat", heartbeat, False),))
    assert settings.heartbeat_plan() == {"activities": [], "rest_probability": 0}
    saved = settings.update_heartbeat_plan({
        "activities": [{"id": "weibo", "title": "刷微博", "detail": "看首页时间线", "probability": 70}],
        "rest_probability": 30,
    })
    assert saved == {
        "activities": [{"id": "weibo", "title": "刷微博", "detail": "看首页时间线", "probability": 70}],
        "rest_probability": 30,
    }
    stored = json.loads(activities_path(heartbeat).read_text(encoding="utf-8"))
    assert stored["rest_probability"] == 30
    with pytest.raises(ValueError):
        settings.update_heartbeat_plan({"activities": [], "rest_probability": 999})
    assert load_plan(activities_path(heartbeat)).rest_probability == 30


def test_save_and_load_preserve_order(tmp_path):
    path = tmp_path / "HEARTBEAT_ACTIVITIES.json"
    heartbeat_plan = plan(("b", 40), ("a", 30), rest=30)
    save_plan(path, heartbeat_plan)
    assert load_plan(path) == heartbeat_plan
