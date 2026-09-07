"""Builtin provider fields; plugins declare their own schemas at registration."""

import copy


def field(kind="string", default=None, *, secret=False):
    value = {"type": kind}
    if default is not None:
        value["default"] = default
    if secret:
        value["secret"] = True
    return value


LLM = {
    "base_url": field(),
    "api_key": field(secret=True),
    "model": field(),
    "max_tokens": field("integer", 16384),
    "temperature": field("number", 0.6),
    "timeout_seconds": field("number", 300),
    "max_retries": field("integer", 3),
    "tool_choice": field("boolean", True),
    "thinking": field("object", {}),
}
SCHEMAS = {
    ("openai", "llm"): LLM,
    ("anthropic", "llm"): LLM,
    ("deepseek", "llm"): {**LLM, "base_url": field(default="https://api.deepseek.com")},
    ("tencent", "asr"): {
        "secret_id": field(secret=True),
        "secret_key": field(secret=True),
        "region": field(default=""),
        "engine": field(default="16k_zh"),
        "timeout_seconds": field("number", 30),
        "max_audio_bytes": field("integer", 3145728),
    },
    ("fish", "tts"): {
        "api_key": field(secret=True),
        "reference_id": field(),
        "model": field(default="s2.1-pro-free"),
        "base_url": field(default="https://api.fish.audio"),
        "format": field(default="mp3"),
        "latency": field(default="normal"),
        "timeout_seconds": field("number", 60),
        "max_audio_bytes": field("integer", 20971520),
    },
    ("openai", "embedding"): {
        "endpoint": field(default="http://embedding:8002/v1/embeddings"),
        "api_key": field(secret=True),
        "model": field(default="BAAI/bge-small-zh-v1.5"),
        "dimensions": field("integer", 512),
        "calibration_profile": field(default="bge-small-zh-v1.5-momoi-v1"),
        "query_timeout_seconds": field("number", 5),
        "document_timeout_seconds": field("number", 30),
    },
    ("deepseek", "balance"): {
        "api_key": field(secret=True),
        "base_url": field(default="https://api.deepseek.com"),
        "timeout_seconds": field("number", 10),
    },
}


def builtin_schema(name, capability):
    fields = copy.deepcopy(SCHEMAS.get((name, capability), {}))
    labels = {
        "base_url": "服务地址",
        "endpoint": "Embedding 接口地址",
        "api_key": "API 密钥",
        "model": "模型名称",
        "max_tokens": "最大输出 Token",
        "temperature": "温度",
        "timeout_seconds": "请求超时（秒）",
        "max_retries": "最大重试次数",
        "tool_choice": "工具选择（Tool Choice）",
        "secret_id": "Secret ID",
        "secret_key": "Secret Key",
        "region": "服务区域",
        "engine": "识别引擎",
        "max_audio_bytes": "音频大小上限（字节）",
        "reference_id": "音色 ID",
        "format": "音频格式",
        "latency": "延迟模式",
        "dimensions": "向量维度",
        "calibration_profile": "评分校准配置",
        "query_timeout_seconds": "查询超时（秒）",
        "document_timeout_seconds": "文档编码超时（秒）",
    }
    for key, spec in fields.items():
        spec["label"] = labels.get(key, key)
    basic = {
        "llm": {"base_url", "api_key", "model"},
        "asr": {"secret_id", "secret_key"},
        "tts": {"api_key", "reference_id", "model"},
        "embedding": {"endpoint", "api_key", "model", "dimensions"},
        "balance": {"api_key", "base_url", "timeout_seconds"},
    }
    for key, spec in fields.items():
        spec["advanced"] = key not in basic[capability]
    required = {
        "llm": {"base_url", "model"},
        "asr": {"secret_id", "secret_key"},
        "tts": {"api_key", "reference_id"},
        "embedding": set(),
        "balance": {"api_key"},
    }
    for key in required[capability]:
        fields[key]["required"] = True
    if (name, capability) == ("openai", "embedding"):
        fields["endpoint"]["description"] = "完整请求地址，例如 https://api.example.com/v1/embeddings；不会自动追加路径。"
        fields["dimensions"]["description"] = "需与所选模型的输出维度一致"
        fields["calibration_profile"]["description"] = "选择与 embedding 模型匹配的已实现校准配置；填写名称不会自动生成评分阈值。"
    if (name, capability) == ("fish", "tts"):
        fields["format"]["enum"] = ["mp3", "wav", "opus"]
        fields["latency"]["enum"] = ["normal", "balanced", "low"]
    return fields
