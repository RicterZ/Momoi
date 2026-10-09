"""Transport-free planning values; evidence references are authenticated by the host."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from typing import NotRequired, TypedDict


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class MemoryRequest(TypedDict):
    id: str
    type: str
    event_id: str
    content: str
    evidence: str
    target_id: NotRequired[int]


@dataclass
class PlanningContext:
    """An injected host adapter may add verified evidence and reviewed snapshots."""
    requests: list[dict[str, object]]
    evidence: dict[str, str]
    snapshots: dict[int, dict[str, object]]


MemoryPlanner = Callable[[PlanningContext], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class MemoryPlan:
    """An immutable review artifact, not an authorization token."""
    payload_json: str

    def payload(self) -> dict[str, object]:
        return json.loads(self.payload_json)

    @property
    def decisions(self) -> list[dict[str, object]]:
        return self.payload()['decisions']

    @property
    def snapshots(self) -> dict[int, dict[str, object]]:
        snapshots = self.payload()['snapshots']
        if not isinstance(snapshots, dict) or any(
            not key.isdecimal() or str(int(key)) != key or int(key) <= 0 for key in snapshots
        ):
            raise ValueError('invalid memory snapshot IDs')
        return {int(key): value for key, value in snapshots.items()}
