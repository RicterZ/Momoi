# 配置参考

[EN](./CONFIG.md) | 中文

Momoi 从 workspace 中读取 `config.json`。默认 workspace 是 `~/.momoi`；
在命令前传入 `--workspace` 可选择其他目录。完整的起始配置见
[config.example/config.json](../config.example/config.json)。

相对路径以 `config.json` 所在目录为基准解析。所有路径字段均可使用绝对路径。
`config.json` 不会展开 `${VAR}` 占位符。

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
| `thinking.stages` | 否 | `{}` | 按运行阶段覆盖推理强度 |

已知阶段名称为 `context_plan`、`owner`、`heartbeat_plan`、`heartbeat`、
`reply_followup`、`goal`、`webhook`、`reflection`、`memory_maintenance`、`episode_anneal` 和
`episode_consolidate`。未单独配置的阶段使用 `thinking.effort`。

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

启用腾讯 ASR 时，`settings.secret_id` 和 `settings.secret_key` 必须非空。
`settings.region` 可以为空，`settings.engine` 默认为 `16k_zh`。

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

`primary` 为必填项，且必须指向 `enabled` 中的一个条目。`enabled` 中的每个
条目都会作为 Channel 插件加载。

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
    "recent_raw_tokens": 32000,
    "recent_turns": 6,
    "planner_recent_base_turns": 6,
    "planner_recent_append_turns": 6,
    "planner_active_recent_turns": 6,
    "planner_recent_tokens": 52800,
    "memory_results": 6,
    "memory_tokens": 8000,
    "max_input_tokens": 96000,
    "summary_results": 8,
    "summary_tokens": 6000,
    "recent_episode_hours": 6
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | 必需且非空的人格文件 |
| `heartbeat_prompt` | `prompts/HEARTBEAT.md` | 可选的心跳指导文件 |
| `recent_raw_tokens` | `32000` | 近期 Turn 的 token 预算；最小值为 `1` |
| `recent_turns` | `6` | 近期 Turn 数量；最小值为 `1` |
| `planner_recent_base_turns` | `recent_turns` | Planner 稳定基础区的 Turn 数量；最小值为 `1` |
| `planner_recent_append_turns` | `recent_turns` | Planner 追加区的 Turn 数量；最小值为 `1` |
| `planner_active_recent_turns` | `recent_turns` | Planner 关注区的 Turn 数量；最小值为 `1` |
| `planner_recent_tokens` | 自动 | Planner 近期日志预算；最小值为 `1000` |
| `memory_results` | `6` | 已确认召回记忆与复盘记忆各自的 top-k；范围为 `0`–`6`，设为 `0` 时关闭两者（合计最多 `12` 条） |
| `memory_tokens` | `8000` | 持久记忆上下文预算；最小值为 `0` |
| `max_input_tokens` | `96000` | 完整模型输入的目标上限；最小值为 `1000` |
| `summary_results` | `8` | 查询召回的 Episode 上限，最多可配置为 `12`；`0` 关闭查询召回 |
| `summary_tokens` | `6000` | 合并后 Episode 摘要的 token 预算；`0` 关闭该层 |
| `recent_episode_hours` | `6` | 近期 Episode 时间窗口，单位为小时；`0` 关闭该层 |

省略 `planner_recent_tokens` 时，其值取 `max_input_tokens` 的 55% 与 `88000`
中的较小值。`max_input_tokens` 应低于 Provider 的实际上下文窗口。

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

`mcp.json` 中的 MCP 服务器条目可以使用 `command`，以及可选的 `args`、
`cwd` 和 `env`；也可以使用远程 `url`，以及可选的 `headers`。下列可选字段
控制发现和路由。

| MCP 字段 | 默认值 | 说明 |
| --- | --- | --- |
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

两个值都必须为非负数。起始配置省略此节，因此默认不限制。

## 通知

```json
{
  "notifications": {
    "timezone": "UTC",
    "quiet_start": null,
    "quiet_end": null,
    "cooldown_seconds": 1800,
    "pending_owner_delay_seconds": 30
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `timezone` | `UTC` | 日程和免打扰时间使用的 IANA 时区 |
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

`allowed_tools` 是非空字符串数组。默认值为 `curl`、`read_file`、
`write_file` 和 `list_dir`。MCP 工具必须使用完整的
`mcp__<server>__<tool>` 名称。

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
| `at` | `03:00` | 使用 `notifications.timezone` 的本地运行时间，格式为 `HH:MM` |

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
| `idle_seconds` | `60` | 所需的主人空闲时间，必须为非负数 |
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
    "provider": "momoi.extensions.deepseek.DeepSeekPlugin",
    "api_key": "replace-me",
    "base_url": "https://api.deepseek.com",
    "timeout_seconds": 10
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `provider` | 空 | `UsagePlugin` 类的点分名称 |
| `api_key` | 空 | 传给插件构造函数的 `api_key` 参数 |
| 其他字段 | — | 传给插件构造函数的其他关键字参数 |

内置 DeepSeek 插件接受可选的 `base_url` 和 `timeout_seconds` 字段。将
`provider` 留空时仍会记录 token 数量，但不查询 Provider 价格或余额。

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
| `MOMOI_LLM_API_FORMAT` | `llm.api_format` |
| `MOMOI_LLM_BASE_URL` | `llm.base_url` |
| `MOMOI_LLM_API_KEY` | `llm.api_key` |
| `MOMOI_LLM_MODEL` | `llm.model` |
| `MOMOI_NAPCAT_URL` | `channels.enabled.napcat.url` |
| `MOMOI_OWNER_QQ` | `channels.enabled.napcat.owner_qq` |
| `MOMOI_PRIMARY` | `channels.primary` |
| `MOMOI_TIMEZONE` | `notifications.timezone` |
| `MOMOI_DASHBOARD_TOKEN` | `dashboard.token` |
| `MOMOI_WEBHOOKS_ENABLED` | `webhooks.enabled` |
| `MOMOI_WEBHOOKS_HOST` | `webhooks.host` |
| `MOMOI_WEBHOOKS_TOKEN` | `webhooks.token` |
| `MOMOI_USAGE_API_KEY` | `usage.api_key` |
| `MOMOI_ASR_SECRET_ID` | `asr.settings.secret_id` |
| `MOMOI_ASR_SECRET_KEY` | `asr.settings.secret_key` |

请妥善保护包含凭证的文件。修改 `config.json`、`mcp.json`、工作流或执行器
定义后，需要重启 `momoi run`。每个新 Turn 开始前都会重新加载提示词文件。
