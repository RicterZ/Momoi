from dataclasses import dataclass, field


def text_value(value: object) -> str:
    return str(value or "").strip()


VISIBLE_ASSISTANT_STATES = frozenset({"delivered", "uncertain"})

DEFAULT_GAP_SECONDS = 30 * 60

DEFAULT_ACTION_LIMIT = 12

@dataclass(frozen=True)
class TranscriptGroup:
    """One protocol message built from consecutive same-role rows."""

    role: str
    parts: tuple[str, ...]
    part_times: tuple[float, ...]
    message_ids: tuple[int, ...]
    turn_ids: tuple[str, ...]
    started_at: float
    ended_at: float
    uncertain: bool = False
    token_estimate: int = field(default=0, compare=False)

@dataclass(frozen=True)
class Transcript:
    """A protocol-valid transcript plus the speech that could not enter it."""

    messages: list[dict[str, object]]
    groups: list[TranscriptGroup]
    orphaned: list[TranscriptGroup]

    @property
    def token_estimate(self) -> int:
        return sum(group.token_estimate for group in self.groups)
