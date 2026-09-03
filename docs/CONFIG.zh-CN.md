# 配置参考

[EN](./CONFIG.md) | 中文

Momoi 从 workspace 中读取 `config.json`。默认 workspace 是 `~/.momoi`；
在命令前传入 `--workspace` 可选择其他目录。完整的起始配置见
[config.example/config.json](../config.example/config.json)。

相对路径以 `config.json` 所在目录为基准解析。所有路径字段均可使用绝对路径。
`config.json` 不会展开 `${VAR}` 占位符。

## 时区

```json
{"timezone": "Asia/Shanghai"}
```

`timezone` 是 Momoi 唯一使用的 IANA 时区，统一控制时间显示、本地日期边界、
日程、免打扰时间、日志和模型上下文。默认值为 `UTC`。各子系统和 Goal 不能
单独覆盖。

## LLM

```json
{
  "llm": {
    "api_format": "anthropic",
    "base_url": "https://llm.example.com",
    "api_key": "replace-me",
    "model": "model-name",
    "max_tokens": 16384,
    "temperature": 0.6,
    "timeout_seconds": 300,
    "max_retries": 3,
    "tool_choice": true,
    "thinking": {
      "effort": "high",
      "stages": {
        "episode_anneal": "low",
        "memory_maintenance": "low",
        "reply_followup": "low"
      }
    }
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_format` | 否 | `anthropic` | 请求格式：`anthropic` 或 `openai` |
| `base_url` | 是 | — | 兼容 API 的基础 URL |
| `api_key` | 是 | — | 非空 API 凭证 |
| `model` | 是 | — | Provider 模型标识符 |
| `max_tokens` | 否 | `16384` | 单次模型调用的最大输出 token 数 |
| `temperature` | 否 | `0.6` | 采样温度 |
| `timeout_seconds` | 否 | `300` | 正数请求超时时间 |
| `max_retries` | 否 | `3` | 瞬时错误的重试次数 |
| `tool_choice` | 否 | `true` | OpenAI 格式请求要求使用工具；拒绝 `tool_choice` 的端点应设为 `false` |
| `thinking.effort` | 否 | Provider 默认值 | 默认推理强度：`low`、`high` 或 `max` |
| `thinking.stages` | 否 | `{}` | 按运行阶段覆盖推理强度；每个值只能是 `low`、`high` 或 `max` |

已知阶段名称为 `context_plan`、`owner`、`heartbeat_plan`、`heartbeat`、
`reply_followup`、`goal`、`webhook`、`reflection`、`memory_maintenance`、`episode_anneal` 和
`episode_consolidate`。未写入 `thinking.stages` 的阶段使用
`thinking.effort`。

## 入站语音识别

```json
{
  "asr": {
    "enabled": false,
    "provider": "tencent",
    "timeout_seconds": 30,
    "max_audio_bytes": 3145728,
    "settings": {
      "secret_id": "replace-me",
      "secret_key": "replace-me",
      "region": "",
      "engine": "16k_zh"
    }
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 为收到的 NapCat 语音消息启用 ASR |
| `provider` | `tencent` | 内置的 `tencent`，或 `ASRProvider` 子类的点分名称 |
| `timeout_seconds` | `30` | 单次转写的正数超时时间 |
| `max_audio_bytes` | `3145728` | 输入大小上限，单位为字节且必须为正数 |
| `settings` | `{}` | Provider 构造参数 |
| `settings.secret_id` | — | 腾讯 API Secret ID；启用腾讯 ASR 时必填 |
| `settings.secret_key` | — | 腾讯 API Secret Key；启用腾讯 ASR 时必填 |
| `settings.region` | 空 | 可选的腾讯地域 |
| `settings.engine` | `16k_zh` | 腾讯识别引擎 |

其他 ASR Provider 可以定义不同的 `settings` 字段。

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
    "transcript_turns_min": 48,
    "transcript_turns_max": 96,
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
| `transcript_turns_min` | `48` | transcript 滑动后保留的最近已完成 Turn 数；最小值为 `1` |
| `transcript_turns_max` | `96` | transcript 增长到此水位时滑回 `transcript_turns_min`；不得小于最小水位 |
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

```json
{
  "embedding": {
    "enabled": true,
    "endpoint": "http://embedding:8002/v1/embeddings",
    "api_key": "",
    "model": "BAAI/bge-small-zh-v1.5",
    "dimensions": 512,
    "calibration_profile": "bge-small-zh-v1.5-momoi-v1",
    "query_timeout_seconds": 5,
    "document_timeout_seconds": 30,
    "document_batch_size": 8
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 为记忆和 Episode 召回启用本地语义候选 |
| `endpoint` | `http://embedding:8002/v1/embeddings` | OpenAI 兼容的 embedding 接口地址 |
| `api_key` | 空 | 接口可选的 Bearer 凭证 |
| `model` | `BAAI/bge-small-zh-v1.5` | Embedding 模型标识；内置 profile 支持此模型 |
| `dimensions` | `512` | 正数向量维度；必须与接口输出一致 |
| `calibration_profile` | `bge-small-zh-v1.5-momoi-v1` | 与模型和文档模板匹配的阈值配置 |
| `query_timeout_seconds` | `5` | 单个 Turn 查询批次的正数超时时间 |
| `document_timeout_seconds` | `30` | 单个后台文档批次的正数超时时间 |
| `document_batch_size` | `8` | 每次后台请求编码的正数文档数量 |

使用随项目提供的 Docker Compose 服务时保持默认 `endpoint`。使用其他 OpenAI
兼容 embedding 服务时，`endpoint`、`model`、`dimensions` 和
`calibration_profile` 必须使用受支持且相互匹配的一组值。接口不可用时自动退回
关键词召回。

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
    "quiet_end": null,
    "cooldown_seconds": 1800,
    "pending_owner_delay_seconds": 30
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `quiet_start` | 未设置 | 本地免打扰时间段的开始时间，格式为 `HH:MM` |
| `quiet_end` | 未设置 | 本地免打扰时间段的结束时间，格式为 `HH:MM` |
| `cooldown_seconds` | `1800` | 相同标识主动联系之间的非负间隔 |
| `pending_owner_delay_seconds` | `30` | 有主人消息待处理时的非负投递延迟 |

`quiet_start` 和 `quiet_end` 必须不同，并且必须同时设置或同时省略。支持跨夜
时间段。

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

## 自主工具

```json
{
  "autonomy": {
    "allowed_tools": ["curl", "read_file", "write_file", "list_dir"]
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `allowed_tools` | `curl`、`read_file`、`write_file`、`list_dir` | 自主 Turn 可用的非空工具名；MCP 工具使用完整的 `mcp__<server>__<tool>` 名称 |

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

## Usage

```json
{
  "usage": {
    "enabled": true,
    "provider": "momoi.extensions.deepseek.DeepSeekPlugin",
    "api_key": "replace-me",
    "base_url": "https://api.deepseek.com",
    "timeout_seconds": 10
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否估算金额并查询账户余额；关闭后仍会统计 token 消耗 |
| `provider` | 空 | `UsagePlugin` 类的点分名称 |
| `api_key` | 空 | 传给插件构造函数的 `api_key` 参数 |
| `base_url` | `https://api.deepseek.com` | 内置 DeepSeek 插件使用的 API 根地址 |
| `timeout_seconds` | `10` | 内置 DeepSeek 插件的请求超时，限制在 `1`–`20` 秒 |
| 其他字段 | — | 传给插件构造函数的其他关键字参数 |

将 `enabled` 设为 `false` 后仍会记录 token 数量，但不会估算金额或查询账户余额。
将 `provider` 留空也有相同效果，并使用协议通用的 token 解析器。

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
| `MOMOI_USAGE_API_KEY` | `usage.api_key` |
| `MOMOI_ASR_SECRET_ID` | `asr.settings.secret_id` |
| `MOMOI_ASR_SECRET_KEY` | `asr.settings.secret_key` |

请妥善保护包含凭证的文件。Dashboard 中修改的模型连接字段会立即生效；修改
其他 `config.json` 字段、`mcp.json`、工作流或执行器定义后，需要重启
`momoi run`。每个新 Turn 开始前都会重新加载提示词文件。
