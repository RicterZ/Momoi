# 配置参考

[EN](./CONFIG.md) | 中文

Momoi 从 workspace 中读取 `config.json`。默认 workspace 是 `~/.momoi`；
在命令前传入 `--workspace` 可选择其他目录。完整的起始配置见
[config.example/config.json](../config.example/config.json)。

相对路径以 `config.json` 所在目录为基准解析。所有路径字段均可使用绝对路径。
`config.json` 不会展开 `${VAR}` 占位符。

外部 API 的端点、凭据和参数统一由 [providers.yaml](./PROVIDERS.zh-CN.md) 管理。
主配置包含 `"providers": "providers.yaml"`，修改服务配置后重启。

## Fish Audio 语音合成

TTS 默认关闭，关闭时不暴露 `send_voice`。启用后微信和 NapCat 请求使用相同的语音工具 schema，
保留工具前缀缓存。harness 只允许支持语音发送的频道（目前为 NapCat）执行。不会修改提示词，也不会强制
“语音输入用语音回复”的规则。

合并到工作区 `providers.yaml` 的对应字段：

```yaml
credentials:
  fish:
    api_key: {env: FISH_API_KEY}
services:
  speech:
    adapter: fish
    base_url: https://api.fish.audio
    credentials: fish
    timeout_seconds: 60
bindings:
  tts:
    service: speech
    enabled: true
    options:
      model: s2.1-pro-free
      reference_id: 9bb8ad542dc44d148c21c73a0884e9ae
      format: mp3
      latency: normal
      max_audio_bytes: 20971520
```

从 [Fish API key 页面](https://fish.audio/app/api-keys) 创建密钥，填写到
`credentials.fish.api_key`，或通过示例中的 `FISH_API_KEY` 环境引用传入。
修改后重启 Momoi，内部可通过 `daemon.bubble_delivery.tts_provider` 访问初始化后的 provider，
调用 `synthesize(text)` 返回 `AudioOutput(data: bytes, format: str)`，音频只在内存中传递。不增加 CLI 入口。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否初始化内部 TTS provider |
| `timeout_seconds` | `60` | 完整 HTTP 响应超时，必须是有限正数 |
| `max_audio_bytes` | `20971520` | 最大音频下载字节数，也限制分块响应 |
| `credentials.fish.api_key` | — | 开启时必填 |
| `services.speech.base_url` | `https://api.fish.audio` | API 基址，程序追加 `/v1/tts` |
| `options.model` | `s2.1-pro-free` | 可选 `s2.1-pro-free`、`s2.1-pro`、`s2-pro`、`s1` |
| `options.reference_id` | — | 必填，Fish 音色页面 URL 中的 ID |
| `options.format` | `mp3` | 支持 `mp3`、`wav`、`opus`，不支持裸 PCM |
| `options.latency` | `normal` | `normal` 优先音质；也支持 `balanced`、`low` |

示例 ID 对应[该 Fish 音色](https://fish.audio/m/9bb8ad542dc44d148c21c73a0884e9ae/)。
音色和免费模型的可用性以 Fish 账号实际情况为准。Fish 文档说明未知模型名会回退到付费模型，
因此 Momoi 会提前拒绝模型名拼写错误，不自动切换模型。合成请求失败后额外重试三次，
间隔为 1、2、4 秒（含首次请求共四次）。
参考：[TTS API](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)、
[价格和限制](https://docs.fish.audio/developer-guide/models-pricing/pricing-and-rate-limits)。

每次失败都记录详情：HTTP 状态及限长响应内容，或连接异常类型、主机、端口和底层 OS 错误。
详情中的 API 密钥、音色 ID 和提交的原文会脱敏。空音频、非音频响应和超出大小上限时
会抛出 `TTSError`，不会写入音频文件。

语音工具只接收完整 `text` 字符串，频道由当前对话决定。数据库会话内容和 transcript 保留原文；
工具等待合成完成后才入队或暂存通知；失败时返回工具错误，建议模型用 `send_bubbles` 降级文字。
成功调用的返回结构与 `send_bubbles` 一致。
outbox 只持久化原文和语音投递标记；合成音频通过有容量上限的内存缓存交给投递 worker，
再以 base64 交给 NapCat。
Momoi 不保存音频文件或音频数据库字段；NapCat 自身的临时文件行为由其服务实现决定。
重启或缓存淘汰后，未发送的消息根据原文重新合成；恢复期间合成失败会标记投递失败。
Weixin 的工具 schema 保持一致，harness 拒绝执行 `send_voice`；直接内部调用返回 `voice_not_supported`。
Owner、Heartbeat、Webhook、Goal、后续回复工作流均支持语音；Goal 沿用原有通知调度规则。

NapCat 和 Weixin 的语音转写前统一添加 `[语音消息] `，随后进入数据库和 transcript。
普通文字不加标记，无法转写的语音占位内容也带此标记。

## 时区

```json
{"timezone": "Asia/Shanghai"}
```

`timezone` 是 Momoi 唯一使用的 IANA 时区，统一控制时间显示、本地日期边界、
日程、免打扰时间、日志和模型上下文。默认值为 `UTC`。各子系统和 Goal 不能
单独覆盖。

## LLM

参数与接入方式见 [Provider 配置](./PROVIDERS.zh-CN.md#llm)。

## 入站语音识别

参数与接入方式见 [Provider 配置](./PROVIDERS.zh-CN.md#asr)。

## 渠道

```json
{
  "channels": {
    "primary": "napcat",
    "enabled": {
      "napcat": {
        "url": "ws://127.0.0.1:3001",
        "owner_qq": "100000000",
        "quiet_seconds": 6,
        "max_batch_seconds": 60,
        "heartbeat_seconds": 30,
        "reconnect_max_seconds": 30,
        "send_timeout_seconds": 20,
        "media_max_bytes": 20971520,
        "media_download_timeout_seconds": 15
      },
      "weixin": {
        "quiet_seconds": 6,
        "max_batch_seconds": 60,
        "reconnect_max_seconds": 30,
        "send_timeout_seconds": 20,
        "media_max_bytes": 104857600
      }
    }
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `primary` | 是 | — | 用于出站投递的渠道名称 |
| `enabled` | 是 | — | 非空的渠道名称与配置对象；必须包含 `primary` |

### NapCat

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `url` | 是 | — | NapCat WebSocket URL |
| `owner_qq` | 是 | — | 仅含数字的主人 QQ 号 |
| `quiet_seconds` | 否 | `1` | 收到主人最新消息后的等待时间 |
| `max_batch_seconds` | 否 | `60` | 消息批次的最长等待时间 |
| `heartbeat_seconds` | 否 | `30` | 连接心跳间隔 |
| `reconnect_max_seconds` | 否 | `30` | 最大重连退避时间 |
| `send_timeout_seconds` | 否 | `20` | 出站请求超时时间 |
| `media_max_bytes` | 否 | `20971520` | 下载入站图片的最大大小 |
| `media_download_timeout_seconds` | 否 | `15` | 入站图片下载超时时间 |

起始配置将 `quiet_seconds` 设为 `6`；省略该字段时，运行时默认值为 1 秒。
时间和大小字段必须为正数。

### 微信

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `quiet_seconds` | `6` | 收到主人最新消息后的等待时间 |
| `max_batch_seconds` | `60` | 消息批次的最长等待时间 |
| `reconnect_max_seconds` | `30` | 最大更新重试延迟 |
| `send_timeout_seconds` | `20` | 出站请求超时时间 |
| `media_max_bytes` | `104857600` | 入站或出站媒体的最大大小 |

所有字段都必须为正数。

## 上下文

```json
{
  "context": {
    "soul_prompt": "prompts/SOUL.md",
    "heartbeat_prompt": "prompts/HEARTBEAT.md",
    "transcript_turns_min": 32,
    "transcript_turns_max": 80,
    "episode_raw_tail_turns": 6,
    "memory_results": 6,
    "max_input_tokens": 142222,
    "context_compaction_ratio": 0.9,
    "summary_results": 8,
    "summary_tokens": 6000
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | 必需且非空的人格文件 |
| `heartbeat_prompt` | `prompts/HEARTBEAT.md` | 可选的心跳指导文件 |
| `transcript_turns_min` | `32` | transcript 滑动后保留的最近已完成 Turn 数；最小值为 `1` |
| `transcript_turns_max` | `80` | transcript 增长到此水位时滑回 `transcript_turns_min`；不得小于最小水位 |
| `episode_raw_tail_turns` | `6` | 开放 Episode 在摘要之外保留的原始尾部 Turn 数；其常规退火阈值为该值的两倍；最小值为 `1` |
| `memory_results` | `6` | 已确认召回记忆与复盘记忆各自的 top-k；范围为 `0`–`6`，设为 `0` 时关闭两者（合计最多 `12` 条） |
| `max_input_tokens` | `142222` | 完整模型输入的上限预算；最小值为 `1000` |
| `context_compaction_ratio` | `0.9` | 达到 `max_input_tokens` 此比例时开始压缩旧 transcript 和当前 Turn 工具结果；范围为 `(0, 1]`。默认在 128,000 tokens 开始压缩。 |
| `summary_results` | `8` | 查询召回的 Episode 上限，最多可配置为 `12`；`0` 关闭查询召回 |
| `summary_tokens` | `6000` | 合并后 Episode 摘要的 token 预算；`0` 关闭该层 |

`max_input_tokens` 应低于 Provider 的实际上下文窗口。48–96 Turn transcript
水位与完整请求 token 水位是两道相互独立的保护。Episode 原始证据额度也从同一
压缩水位派生，不再单独配置 token 预算。
常驻记忆和仍有效的近期记忆会完整注入；查询召回仅由 `memory_results` 限制条数，
不再设置独立的记忆 token 预算。

## 存储

```json
{
  "storage": {
    "database": "data/momoi.sqlite3",
    "thinking": null
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `database` | 是 | — | SQLite 数据库路径；自动创建其父目录 |
| `thinking` | 否 | 数据库目录 | 每月 `thinking-YYYY-MM.sqlite3` 文件的存放目录 |

将 `thinking` 设为 `null` 或空字符串时使用数据库目录。

## Embedding 召回

参数与接入方式见 [Provider 配置](./PROVIDERS.zh-CN.md#embedding)。 编码服务配置在 `bindings.embedding`；切换模型、维度或校准配置需要建立新的语义空间。

## 工具与 MCP

```json
{
  "tools": {
    "mcp_config": "mcp.json",
    "result_max_chars": 12000,
    "result_retention_days": 30
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `mcp_config` | `mcp.json` | MCP 服务器配置路径；`null` 或 `""` 关闭 MCP 加载 |
| `result_max_chars` | `12000` | 模型可见的单个工具结果分段最大长度；最小值为 `1000` 个字符 |
| `result_retention_days` | `30` | 大结果私有快照的保留天数；`0` 关闭按时间清理 |

| MCP 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `command` | — | stdio 服务器的可执行文件；未设置 `url` 时必填 |
| `args` | `[]` | 传给 `command` 的参数 |
| `cwd` | 进程目录 | `command` 的工作目录 |
| `env` | `{}` | 为 `command` 增加的环境变量 |
| `url` | — | Streamable HTTP 接口；未设置 `command` 时必填 |
| `headers` | `{}` | 发送给 `url` 的 Header |
| `description` | 根据服务器 ID 生成 | 可选的能力摘要；设置时长度为 1–500 个字符 |
| `enabled_tools` | `["*"]` | 要注册的原始或完整限定工具名；`[]` 表示不注册工具 |
| `readOnlyTools` | `[]` | 应视为只读工具的原始名称 |
| `disabled` | `false` | 保留定义但不连接 |

与 `config.json` 不同，MCP 的环境变量值、URL 和 Header 会从 Momoi 进程
环境中展开 `${VARIABLE}`。

## 单轮预算

```json
{
  "turn": {
    "max_seconds": 0,
    "max_total_tokens": 0
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_seconds` | `0` | 单个 Turn 的运行时间上限；`0` 表示不限制 |
| `max_total_tokens` | `0` | 累计原始输入/输出 token 上限；`0` 表示不限制 |

两个值都必须为非负数。

## 通知

```json
{
  "notifications": {
    "quiet_start": null,
    "quiet_end": null
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `quiet_start` | 未设置 | 本地免打扰时间段的开始时间，格式为 `HH:MM` |
| `quiet_end` | 未设置 | 本地免打扰时间段的结束时间，格式为 `HH:MM` |

`quiet_start` 和 `quiet_end` 必须不同，并且必须同时设置或同时省略。支持跨夜
时间段。

静默时段用于心跳联系和系统通知队列，紧急系统通知可跳过。
Goal 的 `send_bubbles` / `send_voice` 调用后立即进入通用发送流程。
心跳本身的主人忙碌状态检查和新消息打断仍保留。

## 心跳

```json
{
  "heartbeat": {
    "enabled": false,
    "initial_delay_seconds": 900,
    "min_interval_seconds": 1800,
    "max_interval_seconds": 5400
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用自动心跳检查 |
| `initial_delay_seconds` | `900` | 首次心跳前的正数延迟 |
| `min_interval_seconds` | `1800` | 正数最小间隔 |
| `max_interval_seconds` | `5400` | 正数最大间隔 |

`max_interval_seconds` 必须大于或等于 `min_interval_seconds`。

## 复盘

```json
{
  "reflection": {
    "enabled": true,
    "at": "03:00"
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用每日复盘 |
| `at` | `03:00` | 使用顶层 `timezone` 的本地运行时间，格式为 `HH:MM` |

## Episode 维护

```json
{
  "episode_annealing": {
    "enabled": true,
    "idle_seconds": 60,
    "max_seconds": 650
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 启用后台 Episode 维护 |
| `idle_seconds` | `60` | 合格 Turn 少于 6 个时允许小批次归类所需的主人空闲时间，必须为非负数；满 6 个不等待该超时 |
| `max_seconds` | `650` | 单个批次的正数模型运行时间上限 |

## Webhook

```json
{
  "webhooks": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 8787,
    "token": "replace-with-a-random-token",
    "workflows": "workflows",
    "executors": "workflows/workflow-executors.yaml"
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启动 Webhook API 和工作流 Worker |
| `host` | `127.0.0.1` | 监听地址 |
| `port` | `8787` | `1` 到 `65535` 的 TCP 端口 |
| `token` | 空 | Bearer Token；启用时必填 |
| `workflows` | `workflows` | 工作流 YAML 目录 |
| `executors` | `workflows/workflow-executors.yaml` | 命令执行器定义文件 |

工作流 YAML 参考见 [WORKFLOW.zh-CN.md](./WORKFLOW.zh-CN.md)。

## 看板

```json
{
  "dashboard": {
    "token": "replace-with-a-long-random-secret"
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `token` | 空 | 从 CLI 启用看板时所需的访问口令 |

看板监听地址和端口是 CLI 选项，不属于 `config.json` 字段。

## 账户余额与 token 统计

参数与接入方式见 [Provider 配置](./PROVIDERS.zh-CN.md#账户余额与-token-统计)。 余额查询独立于本地 token 记录；DeepSeek LLM 适配器提供对应的用量解析和费用估算。

## 日志

```json
{
  "logging": {
    "level": "INFO"
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `level` | `DEBUG` | `TRACE`、`DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |

`TRACE` 会将完整 LLM 请求和原始响应写入 `llm-dumps/`。Debug 和 Trace 输出
可能包含私聊内容和工具数据。

## 环境变量覆盖

环境变量会覆盖当前进程的 `config.json` 配置。

| 环境变量 | 配置字段 |
| --- | --- |
| `MOMOI_NAPCAT_URL` | `channels.enabled.napcat.url` |
| `MOMOI_OWNER_QQ` | `channels.enabled.napcat.owner_qq` |
| `MOMOI_PRIMARY` | `channels.primary` |
| `MOMOI_TIMEZONE` | `timezone` |
| `MOMOI_DASHBOARD_TOKEN` | `dashboard.token` |
| `MOMOI_WEBHOOKS_ENABLED` | `webhooks.enabled` |
| `MOMOI_WEBHOOKS_HOST` | `webhooks.host` |
| `MOMOI_WEBHOOKS_TOKEN` | `webhooks.token` |

请妥善保护包含凭证的文件。Provider 凭据仅通过 YAML 中声明的环境引用读取。
修改 `providers.yaml`、`config.json`、`mcp.json`、工作流或执行器定义后，需要重启
`momoi run`。每个新 Turn 开始前都会重新加载提示词文件。
