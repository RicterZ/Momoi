from dataclasses import dataclass, field


@dataclass(frozen=True)
class ThinkingConfig:
    effort: str = ""
    stages: dict[str, str] = field(default_factory=dict)

    def for_stage(self, stage: str) -> str:
        return str((self.stages or {}).get(stage) or self.effort)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    max_tokens: int
    temperature: float
    timeout_seconds: float
    max_retries: int
    api_format: str = "anthropic"
    tool_choice: bool = True
    thinking: ThinkingConfig = ThinkingConfig()


@dataclass(frozen=True)
class EmbeddingSpaceConfig:
    enabled: bool = False
    model: str = "BAAI/bge-small-zh-v1.5"
    dimensions: int = 512
    calibration_profile: str = "bge-small-zh-v1.5-momoi-v1"
    document_batch_size: int = 8


@dataclass(frozen=True)
class EmbeddingConfig(EmbeddingSpaceConfig):
    endpoint: str = "http://embedding:8002/v1/embeddings"
    api_key: str = field(default="", repr=False)
    query_timeout_seconds: float = 5
    document_timeout_seconds: float = 30
