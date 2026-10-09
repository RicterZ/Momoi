"""Builtin provider fields; plugins declare their own schemas at registration."""

import copy

from .request_context import THINKING_EFFORTS

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
    "tool_choice": field("boolean", False),
    "thinking": {
        **field("object", {}),
        "properties": {
            "effort": {
                "type": "string",
                "label": "默认思考强度",
                "default": "low",
                "enum": ["", *THINKING_EFFORTS],
            },
        },
    },
}
SCHEMAS = {
    ("openai", "llm"): {
        **LLM,
        "base_url": field(default="https://api.deepseek.com/v1"),
        "model": field(default="deepseek-flash"),
    },
    ("anthropic", "llm"): LLM,
    ("sherpa", "asr"): {
        "model_path": field(default=""),
        "num_threads": field("integer", 2),
        "trailing_silence": field("number", 0.8),
    },
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
        "reference_id": field(default="9bb8ad542dc44d148c21c73a0884e9ae"),
        "model": field(default="s2.1-pro-free"),
        "format": field(default="mp3"),
        "latency": field(default="normal"),
        "timeout_seconds": field("number", 60),
        "max_audio_bytes": field("integer", 20971520),
    },
    ("vocu", "tts"): {
        "api_key": field(secret=True),
        "voice_id": field(),
        "prompt_id": field(default="default"),
        "preset": field(default="balance"),
        "speech_rate": field("number", 1),
        "language": field(default="auto"),
        "flash": field("boolean", False),
        "vivid": field("boolean", False),
        "timeout_seconds": field("number", 60),
        "max_audio_bytes": field("integer", 20971520),
    },
    ("local", "embedding"): {"model_path": field(default="")},
    ("openai", "embedding"): {
        "endpoint": field(),
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
        "accounting": field("boolean", True),
    },
}


def builtin_schema(name, capability):
    fields = copy.deepcopy(SCHEMAS.get((name, capability), {}))
    labels = {
        "secret_id": "SecretId",
        "secret_key": "SecretKey",
        "region": "地域",
        "engine": "识别引擎",
        "base_url": "服务地址",
        "endpoint": "Embedding 接口地址",
        "api_key": "API 密钥",
        "model": "模型名称",
        "max_tokens": "最大输出 Token",
        "temperature": "温度",
        "timeout_seconds": "请求超时（秒）",
        "max_retries": "最大重试次数",
        "tool_choice": "工具选择（Tool Choice）",
        "thinking": "模型思考设置",
        "accounting": "费用估算",
        "max_audio_bytes": "音频大小上限（字节）",
        "reference_id": "音色 ID",
        "voice_id": "音色 ID",
        "prompt_id": "音色风格 ID",
        "preset": "合成预设",
        "speech_rate": "语速",
        "language": "语言",
        "flash": "极速生成",
        "vivid": "增强表现力",
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
        "tts": {"api_key", "reference_id", "voice_id", "model"},
        "embedding": {"endpoint", "api_key", "model", "dimensions"},
        "balance": {"api_key", "base_url", "timeout_seconds", "accounting"},
    }
    for key, spec in fields.items():
        spec["advanced"] = key not in basic[capability]
    required = {
        "llm": {"base_url", "model"},
        "asr": {"secret_id", "secret_key"} if name == "tencent" else set(),
        "tts": {"api_key", "voice_id" if name == "vocu" else "reference_id"},
        "embedding": {"endpoint"} if name == "openai" else set(),
        "balance": set(),
    }
    for key in required[capability]:
        fields[key]["required"] = True
    if (name, capability) == ("sherpa", "asr"):
        fields["model_path"].update(label="本地 ASR 模型目录", advanced=True,
            description="留空使用内置模型；仅自定义本地模型时填写。")
        fields["num_threads"]["label"] = "CPU 推理线程数"
        fields["trailing_silence"]["label"] = "断句静音（秒）"
    if (name, capability) == ("deepseek", "balance"):
        fields["api_key"]["description"] = "可留空：仅估算费用，不查询账户余额。填写后启用余额查询。"
        fields["accounting"]["description"] = "按 DeepSeek 用量和官方价格估算模型费用；模型使用其他服务商时请关闭。关闭后仍可查询余额并记录通用 Token 用量。"
    if (name, capability) == ("local", "embedding"):
        fields["model_path"].update(label="BGE 模型目录",
            description="留空使用安装包内置模型。直接调用本地 BGE，无需服务地址。")
    if (name, capability) == ("openai", "embedding"):
        fields["endpoint"]["description"] = "完整请求地址，例如 https://api.example.com/v1/embeddings；不会自动追加路径。"
        fields["dimensions"]["description"] = "需与所选模型的输出维度一致"
        fields["calibration_profile"]["description"] = "选择与 embedding 模型匹配的已实现校准配置；填写名称不会自动生成评分阈值。"
    if (name, capability) == ("fish", "tts"):
        fields["format"]["enum"] = ["mp3", "wav", "opus"]
        fields["latency"]["enum"] = ["normal", "balanced", "low"]
    if (name, capability) == ("vocu", "tts"):
        fields["prompt_id"]["description"] = "使用该音色的风格 ID，default 为默认风格。"
        fields["timeout_seconds"]["minimum"] = 1
        fields["max_audio_bytes"]["minimum"] = 1
        fields["speech_rate"].update(minimum=0.5, maximum=2)
        fields["speech_rate"]["description"] = "时长倍率，0.5–2；值越大，语速越慢。"
        fields["preset"]["enum"] = ["creative", "balance", "stable"]
    return fields
