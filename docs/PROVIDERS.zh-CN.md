# 外部服务 Provider

[EN](./PROVIDERS.md) | 中文

外部 API 统一配置在 `providers.yaml`。主配置 `config.json` 只写
`"providers": "providers.yaml"`，路径相对于主配置所在目录。修改后重启 Momoi；
后台设置页编辑提示词文件，服务配置由 YAML 管理。

从[完整示例](../config.example/providers.yaml)开始，启用所需能力并填写凭据。

## 连接服务

Service 定义适配器、端点、凭据和共享默认参数；Binding 为某项能力选择服务并配置
任务参数。同一个服务可以绑定多个能力，前提是适配器实现了这些能力。

```yaml
version: 1
credentials:
  deepseek:
    api_key: {env: DEEPSEEK_API_KEY}
services:
  deepseek:
    adapter: deepseek
    base_url: https://api.deepseek.com
    credentials: deepseek
bindings:
  llm:
    service: deepseek
    options:
      model: deepseek-v4-flash
      max_tokens: 16384
      thinking:
        effort: high
        stages:
          reply_followup: low
  balance:
    service: deepseek
    options:
      timeout_seconds: 10
```

在 Momoi 进程环境中设置 `DEEPSEEK_API_KEY`。Docker 部署需要通过容器的
`environment` 或 `env_file` 传入。凭据也可以直接写字符串。
只有明确的 `{env: NAME}` 会读取环境变量，不会自动展开字符串中的 `${NAME}`。

| 字段 | 含义 |
| --- | --- |
| `version` | 必填整数 `1` |
| `plugins` | 可选的 Python 模块名列表，启动校验前导入，模块需安装在运行环境 |
| `credentials` | 命名凭据组，例如 `api_key` 或腾讯的 `secret_id` / `secret_key` |
| `services` | 命名服务：`adapter`，以及可选的 `credentials`、`base_url`、`timeout_seconds`、`settings` |
| `bindings` | 能力映射：`service`，可选布尔值 `enabled`（默认 `true`）和 `options` |

参数按服务 `settings` → 服务端点/超时 → binding `options` → 凭据字段合并。
凭据字段不能在 options 中重复定义。启动前会校验未知字段、重复 YAML 键、引用关系
和适配器支持的能力。启用的 binding 必须能读取其引用的环境凭据；禁用的 binding
仍需引用有效服务，但不要求环境密钥，也不会创建客户端。
`llm` 必须存在且启用；省略的可选能力默认禁用。

## 内置适配器

| 适配器 | 能力 | 用途 |
| --- | --- | --- |
| `anthropic` | `llm` | Anthropic Messages 协议 |
| `openai` | `llm` | OpenAI Chat Completions 协议 |
| `deepseek` | `llm` | OpenAI 协议，加 DeepSeek token 解析和本地费用估算 |
| `openai` | `embedding` | OpenAI 兼容的向量接口 |
| `tencent` | `asr` | 腾讯 SentenceRecognition |
| `fish` | `tts` | Fish Audio 语音合成 |
| `deepseek` | `balance` | 查询账户余额，与本地 token 统计独立 |

### LLM

`model`、`base_url` 必填；`deepseek` 的默认地址为 `https://api.deepseek.com`。
无需鉴权的本地接口可以省略 `api_key`。默认参数：`max_tokens: 16384`、
`temperature: 0.6`、`timeout_seconds: 300`、`max_retries: 3`、`tool_choice: true`。
`thinking.effort` 和 `thinking.stages` 的值支持 `low`、`high`、`max`，阶段设置优先。
接口不支持强制工具调用时设置 `tool_choice: false`。协议由服务的 adapter 决定。

### Embedding

可以配置服务 `base_url`，程序追加 `/v1/embeddings`（已以 `/v1` 结尾则追加
`/embeddings`）；也可以直接设置 `endpoint`。

| 参数 | 默认值 |
| --- | --- |
| `model` | `BAAI/bge-small-zh-v1.5` |
| `dimensions` | `512` |
| `calibration_profile` | `bge-small-zh-v1.5-momoi-v1` |
| `query_timeout_seconds` | `5` |
| `document_timeout_seconds` | `30` |
| `document_batch_size` | `8` |

服务 `timeout_seconds` 同时提供两种超时的默认值，单独指定的 query/document 超时优先。
模型、维度和校准配置必须与编码器匹配。使用 `momoi embedding` 命令前需要启用 binding。
查询失败仍会退回关键词召回，并保留查询熔断机制。

### ASR

腾讯凭据要求 `secret_id` 和 `secret_key`。默认参数：`region: ""`、`engine: 16k_zh`、
`timeout_seconds: 30`、`max_audio_bytes: 3145728`。音频大小限制由入站渠道执行，
不会作为请求参数发给腾讯。微信渠道自带的转写独立于这项可选的 NapCat ASR 能力。

### TTS

Fish 需要 `api_key` 和 `reference_id`。默认参数：`model: s2.1-pro-free`、
`base_url: https://api.fish.audio`、`format: mp3`、`latency: normal`、
`timeout_seconds: 60`、`max_audio_bytes: 20971520`。
模型支持 `s1`、`s2-pro`、`s2.1-pro`、`s2.1-pro-free`；格式支持 `mp3`、`wav`、`opus`；
latency 支持 `normal`、`balanced`、`low`。

首次请求失败后重试三次，间隔 1、2、4 秒，每次失败记录有长度限制且脱敏的错误详情。
`send_voice` 等待合成完成，失败返回 tool error 并建议 `send_bubbles` 降级文字；
成功结果与 `send_bubbles` 一致。投递细节见[语音合成](./CONFIG.zh-CN.md#fish-audio-语音合成)。

### 账户余额与 token 统计

DeepSeek 余额需要 `api_key`，默认 `base_url: https://api.deepseek.com`、
`timeout_seconds: 10`。后台通过 balance 能力查询余额；API 失败时显示不可用，
不影响概览中的其他数据。

Token 数量独立记录。选择 `deepseek` LLM 适配器时启用该厂商的 token 解析和本地价格表；
仅配置 DeepSeek 的余额服务不会给其他 LLM 套用 DeepSeek 价格。
禁用 balance 不影响 token 记录或 LLM 费用估算。

## 扩展代码架构

`integrations/contracts` 定义 LLM、ASR、TTS、embedding、balance 能力接口，
`integrations/adapters` 实现厂商协议。`ServiceRegistry` 负责组装服务，业务代码只依赖
能力接口，不导入具体 API 客户端。HTTP 连接池、错误分类和重试基础设施与注册表同层；
LLM 协议特有的消息回放、工具编码、遥测和重试保留在 `llm`。

工厂收到参数字典和 `AdapterContext`（HTTP transport、dump 目录、语义策略），不接收
Momoi 主配置。构造时不应打开网络资源。在进入注册表的 async scope 前获取所需能力；
注册表按需创建实例，进入其异步上下文或注册 `close()`，退出时关闭所拥有的资源。
外部注入的测试服务由调用方管理。LLM 协议 session 和 embedding 的 HTTPX pool 也由
注册表统一管理生命周期。

安装 Python 模块并将模块名加入 `plugins`。以下 `my_balance.py` 是可用于本地测试的
固定余额适配器：

```python
from momoi.integrations.registry import register_adapter

class FixedBalance:
    def __init__(self, options, context):
        self.amount = options["amount"]

    async def balance(self):
        return {"source": "live", "currency": "CNY",
                "is_available": True, "total_balance": self.amount}

def validate(options):
    if set(options) != {"amount"} or not isinstance(options["amount"], str):
        raise ValueError("amount must be a string")

register_adapter("fixed", "balance", FixedBalance, validate=validate)
```

添加 `plugins: [my_balance]`，定义 `adapter: fixed`、`settings: {amount: "12.34"}`
的 service，并将 balance binding 指向它。每项能力注册工厂和离线校验函数。
同一厂商新增其他 API 时，继续注册对应能力，无需修改主配置字段、业务消费者或后台。

LLM 实现需提供契约中的 `config`、`accounting`、`usage_sink`、`thinking_sink`、
`usage_parser` 和 `complete()`。Embedding 的 `encode()` 返回归一化向量，并实现
`health()`、`close()`；binding options 同时描述语义空间的模型、维度和校准配置。
TTS 失败抛出 `TTSError`，余额适配器抛出带脱敏详情及分类的 `IntegrationError`；
取消操作必须向上传播。
