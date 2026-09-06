from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..integrations.configuration import ProviderCatalog

from ..policies import RuntimePolicies


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NotificationConfig:
    quiet_start: str | None = None
    quiet_end: str | None = None
    cooldown_seconds: float = 1800
    pending_owner_delay_seconds: float = 30


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    token: str = ""
    workflows: Path | None = None
    executors: Path | None = None


@dataclass(frozen=True)
class DashboardConfig:
    token: str = ""


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    initial_delay_seconds: float = 900
    min_interval_seconds: float = 1800
    max_interval_seconds: float = 5400


@dataclass(frozen=True)
class ReflectionConfig:
    enabled: bool = False
    at: str = "03:00"


@dataclass(frozen=True)
class EpisodeAnnealingConfig:
    enabled: bool = True
    idle_seconds: float = 60
    max_seconds: float = 650


@dataclass(frozen=True)
class AppConfig:
    providers: "ProviderCatalog"
    channel: object
    system_prompt: str
    transcript_turns_min: int
    transcript_turns_max: int
    episode_raw_tail_turns: int
    memory_results: int
    database: Path
    log_level: str
    timezone: str = "UTC"
    max_input_tokens: int = 142222
    context_compaction_ratio: float = 0.9
    summary_results: int = 8
    summary_tokens: int = 6000
    soul_prompt: str = ""
    mcp_config: Path | None = None
    notifications: NotificationConfig = NotificationConfig()
    tool_result_max_chars: int = 12000
    tool_result_retention_days: float = 30
    turn_max_seconds: float = 0
    turn_max_total_tokens: int = 0
    webhooks: WebhookConfig = WebhookConfig()
    dashboard: DashboardConfig = DashboardConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    reflection: ReflectionConfig = ReflectionConfig()
    episode_annealing: EpisodeAnnealingConfig = EpisodeAnnealingConfig()
    workspace: Path | None = None
    heartbeat_prompt: str = ""
    soul_prompt_path: Path | None = None
    heartbeat_prompt_path: Path | None = None
    thinking: Path | None = None
    channels: tuple[object, ...] = ()
    policies: RuntimePolicies = RuntimePolicies()

    @property
    def channel_configs(self) -> tuple[object, ...]:
        return self.channels or (self.channel,)
