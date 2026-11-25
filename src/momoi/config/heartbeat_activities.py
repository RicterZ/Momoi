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


def parse_activities(raw: object) -> tuple[HeartbeatActivity, ...]:
    """Validate the stored or submitted activity list; ids stay stable across edits."""
    if not isinstance(raw, list):
        raise ValueError("activities must be a list")
    if len(raw) > MAX_ACTIVITIES:
        raise ValueError(f"at most {MAX_ACTIVITIES} activities")
    items: list[HeartbeatActivity] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"activity {index} must be an object")
        activity_id = entry.get("id")
        title = " ".join(str(entry.get("title") or "").split())
        detail = str(entry.get("detail") or "").strip()
        probability = entry.get("probability")
        if not isinstance(activity_id, str) or not _ID.fullmatch(activity_id):
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
    total = sum(item.probability for item in items)
    if total not in {0, TOTAL_PROBABILITY}:
        raise ValueError(f"activity probabilities must add up to {TOTAL_PROBABILITY}% (or all be 0 for manual only); got {total}%")
    return tuple(items)


def load_activities(path: Path) -> tuple[HeartbeatActivity, ...]:
    if not path.is_file():
        return ()
    data: Any = json.loads(path.read_text(encoding="utf-8") or "{}")
    return parse_activities(data.get("activities", []) if isinstance(data, dict) else data)


def save_activities(path: Path, activities: tuple[HeartbeatActivity, ...]) -> None:
    payload = {"activities": [item.public() for item in activities]}
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def activity_catalog(activities: tuple[HeartbeatActivity, ...]) -> str:
    if not activities:
        return "（未配置心跳活动；只能选择 custom 或 rest。）"
    lines = [f"- {item.id}：{item.title}" for item in activities]
    if not any(item.probability for item in activities):
        lines.append("（未分配随机概率，random 不可用。）")
    return "\n".join(lines)


def select_activity(
    activities: tuple[HeartbeatActivity, ...],
    choice: str,
    activity_id: object = None,
    rng: random.Random | None = None,
) -> tuple[HeartbeatActivity | None, str | None]:
    """Return the selected activity, or an error code when the choice cannot be satisfied."""
    if choice == "pick":
        selected = next((item for item in activities if item.id == activity_id), None)
        return (selected, None) if selected else (None, "unknown_heartbeat_activity")
    if choice == "random":
        pool = [item for item in activities if item.probability > 0]
        if not pool:
            return None, "random_heartbeat_disabled"
        return (rng or random).choices(pool, weights=[item.probability for item in pool])[0], None
    return None, None
