import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import atomic_write

ACTIVITIES_FILENAME = "HEARTBEAT_ACTIVITIES.json"
MAX_ACTIVITIES = 50
MAX_TITLE = 60
MAX_DETAIL = 4000
TOTAL_PROBABILITY = 100
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class HeartbeatActivity:
    id: str
    title: str
    detail: str
    probability: int

    def public(self) -> dict[str, object]:
        return {"id": self.id, "title": self.title, "detail": self.detail, "probability": self.probability}


def activities_path(heartbeat_prompt_path: Path) -> Path:
    return heartbeat_prompt_path.parent / ACTIVITIES_FILENAME


@dataclass(frozen=True)
class HeartbeatPlan:
    activities: tuple[HeartbeatActivity, ...] = ()
    rest_probability: int = 0

    @property
    def random_enabled(self) -> bool:
        return self.rest_probability > 0 or any(item.probability for item in self.activities)

    def public(self) -> dict[str, object]:
        return {"activities": [item.public() for item in self.activities], "rest_probability": self.rest_probability}


REST = "rest"


def parse_plan(raw: object) -> HeartbeatPlan:
    """Validate the stored or submitted plan; activity ids stay stable across edits."""
    if not isinstance(raw, dict):
        raise ValueError("heartbeat plan must be an object")
    activities = raw.get("activities", [])
    rest_probability = raw.get("rest_probability", 0)
    if not isinstance(activities, list):
        raise ValueError("activities must be a list")
    if len(activities) > MAX_ACTIVITIES:
        raise ValueError(f"at most {MAX_ACTIVITIES} activities")
    if type(rest_probability) is not int or not 0 <= rest_probability <= TOTAL_PROBABILITY:
        raise ValueError(f"rest probability must be an integer 0-{TOTAL_PROBABILITY}")
    items: list[HeartbeatActivity] = []
    seen: set[str] = set()
    for index, entry in enumerate(activities, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"activity {index} must be an object")
        activity_id = entry.get("id")
        title = " ".join(str(entry.get("title") or "").split())
        detail = str(entry.get("detail") or "").strip()
        probability = entry.get("probability")
        if not isinstance(activity_id, str) or not _ID.fullmatch(activity_id) or activity_id == REST:
            raise ValueError(f"activity {index} has an invalid id")
        if activity_id in seen:
            raise ValueError(f"duplicate activity id: {activity_id}")
        if not title or len(title) > MAX_TITLE:
            raise ValueError(f"activity {index} title must be 1-{MAX_TITLE} characters")
        if not detail or len(detail) > MAX_DETAIL:
            raise ValueError(f"activity {index} detail must be 1-{MAX_DETAIL} characters")
        if type(probability) is not int or not 0 <= probability <= TOTAL_PROBABILITY:
            raise ValueError(f"activity {index} probability must be an integer 0-{TOTAL_PROBABILITY}")
        seen.add(activity_id)
        items.append(HeartbeatActivity(activity_id, title, detail, probability))
    total = rest_probability + sum(item.probability for item in items)
    if total not in {0, TOTAL_PROBABILITY}:
        raise ValueError(f"activity and rest probabilities must add up to {TOTAL_PROBABILITY}% (or all be 0 for manual only); got {total}%")
    return HeartbeatPlan(tuple(items), rest_probability)


def load_plan(path: Path) -> HeartbeatPlan:
    if not path.is_file():
        return HeartbeatPlan()
    return parse_plan(json.loads(path.read_text(encoding="utf-8") or "{}"))


def save_plan(path: Path, plan: HeartbeatPlan) -> None:
    atomic_write(path, json.dumps(plan.public(), ensure_ascii=False, indent=2) + "\n")


def activity_catalog(plan: HeartbeatPlan) -> str:
    lines = [f"- {item.id}：{item.title}" for item in plan.activities]
    if not lines:
        lines.append("（未配置心跳活动；pick 不可用。）")
    if not plan.random_enabled:
        lines.append("（未分配随机概率，random 不可用。）")
    return "\n".join(lines)


def select_activity(
    plan: HeartbeatPlan,
    choice: str,
    activity_id: object = None,
    rng: random.Random | None = None,
) -> tuple[HeartbeatActivity | str | None, str | None]:
    """Return the selected activity (or REST from a random draw), or an error code."""
    if choice == "pick":
        selected = next((item for item in plan.activities if item.id == activity_id), None)
        return (selected, None) if selected else (None, "unknown_heartbeat_activity")
    if choice == "random":
        pool: list[HeartbeatActivity | str] = [item for item in plan.activities if item.probability > 0]
        weights = [item.probability for item in plan.activities if item.probability > 0]
        if plan.rest_probability:
            pool.append(REST)
            weights.append(plan.rest_probability)
        if not pool:
            return None, "random_heartbeat_disabled"
        return (rng or random).choices(pool, weights=weights)[0], None
    return None, None
