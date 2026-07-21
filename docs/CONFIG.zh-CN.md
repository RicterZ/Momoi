# 配置与能力访问

[EN](./CONFIG.md) | 中文

Momoi 从 workspace 读取配置。默认 workspace 是 `~/.momoi`；使用 `--workspace` 可选择其他目录。

```bash
momoi run
momoi --workspace /path/to/workspace run
```

首次运行前，先从通用模板创建 workspace：

```bash
mkdir -p ~/.momoi
cp -R config.example/. ~/.momoi/
```

完整模板见 [config.example/config.json](../config.example/config.json)。

## 选择集成方式

| 需求 | 使用 |
| --- | --- |
| 让模型发现并调用外部能力 | `mcp.json` 中的 MCP 服务器 |
| 让 Home Assistant、Jellyfin 或其他服务推送事件 | Webhook 工作流 |
| 重复执行需要新信息、推理或工具调用的工作 | 目标 |
| 在已知时间传达固定文本 | 提醒 |
| 在主人任务中访问 URL | 内置 HTTP 工具 |

MCP 是添加由模型控制能力的常规方式。工作流用于事件驱动、预先定义的执行序列。Webhook 配置和 YAML 参考见 [WORKFLOW.zh-CN.md](./WORKFLOW.zh-CN.md)。

## 路径与 workspace 文件

`config.json` 中的相对路径以该文件所在目录为基准解析。

```text
~/.momoi/
├── config.json
├── mcp.json
├── prompts/
│   └── SOUL.md
├── workflows/
│   └── *.yaml
├── workflow-executors.yaml
├── emotion/
└── data/
```

因此 workspace 可以整体移动。支持路径的字段也可以使用绝对路径。

## LLM

```json
{
  "llm": {
    "api_format": "anthropic",
    "base_url": "https://llm.example.com",
    "api_key": "replace-me",
    "model": "model-name",
    "max_tokens": 4096,
    "temperature": 0.6,
    "timeout_seconds": 120,
    "max_retries": 2
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_format` | 否 | `anthropic` | `anthropic` 或 `openai` |
| `base_url` | 是 | — | 兼容 API 的基础 URL |
| `api_key` | 是 | — | API 凭据，不能为空 |
| `model` | 是 | — | 发送给 provider 的模型标识 |
| `max_tokens` | 否 | `2048` | 单次模型调用的最大输出 token 数 |
| `temperature` | 否 | `0.6` | 采样温度 |
| `timeout_seconds` | 否 | `120` | 必须为正数的请求超时时间 |
| `max_retries` | 否 | `2` | 短暂连接错误和服务端错误的重试次数 |

对于 Anthropic 兼容 provider，Momoi 请求 `/v1/messages`。对于 OpenAI 兼容 provider，只含 host 的 URL 会请求 `/v1/chat/completions`；已经包含网关路径的 URL 则在该路径下请求 `/chat/completions`。

`config.json` 不会展开环境变量。如果其中包含凭据，请妥善保管并限制文件权限。

## NapCat

```json
{
  "napcat": {
    "url": "ws://127.0.0.1:3001",
    "owner_qq": "100000000",
    "quiet_seconds": 6,
    "max_batch_seconds": 60,
    "heartbeat_seconds": 30,
    "reconnect_max_seconds": 30,
    "send_timeout_seconds": 20
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `url` | 是 | — | NapCat WebSocket URL |
| `owner_qq` | 是 | — | 仅包含数字、被认可为主人的 QQ 号 |
| `quiet_seconds` | 否 | 生产模板中为 `6` | 收到主人最新消息后等待的时间；新消息会重新计时 |
| `max_batch_seconds` | 否 | `60` | 持续增长的消息批次最长可等待的时间 |
| `heartbeat_seconds` | 否 | `30` | NapCat 连接心跳间隔 |
| `reconnect_max_seconds` | 否 | `30` | 重连退避的最大时间 |
| `send_timeout_seconds` | 否 | `20` | 单次 NapCat 出站请求的超时时间 |

本节所有时间字段都必须为正数。

生产模板使用 6 秒，以便把一组自然连续的短消息一起处理。只有在开发时更看重反馈速度而不是消息收集时，才建议降到 1 秒。

## 上下文与记忆预算

```json
{
  "context": {
    "soul_prompt": "prompts/SOUL.md",
    "recent_raw_tokens": 32000,
    "recent_turns": 6,
    "memory_results": 6,
    "memory_tokens": 8000,
    "max_input_tokens": 96000,
    "summary_results": 3,
    "summary_tokens": 6000
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | 相对于 workspace 的人格文件 |
| `recent_raw_tokens` | `32000` | 以原始形式保留近期对话的预算 |
| `recent_turns` | `6` | 即使裁剪历史也会保留的最少近期主人交互数 |
| `memory_results` | `6` | 自动召回的持久记忆最大数量 |
| `memory_tokens` | `8000` | 召回持久记忆的 token 预算 |
| `max_input_tokens` | `96000` | 包括工具 schema 在内的完整模型输入目标上限 |
| `summary_results` | `3` | 自动召回的较早对话片段最大数量 |
| `summary_tokens` | `6000` | 召回对话片段的 token 预算 |

`max_input_tokens` 应低于 provider 真实的上下文窗口。这些数值是构建上下文的预算，不代表每个 provider 都会以相同方式计算 token。

将某个召回层的结果数量或 token 预算设为 `0` 可关闭该层自动召回。对应工具已启用时，显式记忆和对话搜索工具仍然可用。

## 存储

```json
{
  "storage": {
    "database": "data/momoi.sqlite3"
  }
}
```

`database` 为必填项。相对路径以 workspace 为基准解析，父目录会自动创建。

请备份完整 workspace，以保留对话历史、记忆、目标、提醒、情绪素材和待投递状态。

## MCP 与工具结果

```json
{
  "tools": {
    "mcp_config": "mcp.json",
    "result_max_chars": 30000
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `mcp_config` | `mcp.json` | 标准 MCP 服务器配置；使用 `null` 或空字符串关闭 MCP 加载 |
| `result_max_chars` | `30000` | 返回给模型的归一化工具结果最大长度，最小值为 `1000` |

### 配置 stdio MCP 服务器

```json
{
  "mcpServers": {
    "local-tools": {
      "command": "your-mcp-server",
      "args": ["--option", "value"],
      "cwd": "/optional/working/directory",
      "env": {
        "SERVICE_TOKEN": "${SERVICE_TOKEN}"
      }
    }
  }
}
```

### 配置远程 MCP 服务器

```json
{
  "mcpServers": {
    "remote-tools": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${MCP_TOKEN}"
      }
    }
  }
}
```

MCP 环境值、远程 URL 和 header 支持从 Momoi 进程环境展开 `${VARIABLE}`。Momoi 启动时对应变量必须存在。添加 `"disabled": true` 可保留服务器定义而不建立连接。

每个已连接服务器都会按名称隔离。它的工具以 `mcp__<server>__<tool>` 前缀呈现给模型。单个服务器连接失败会记录到日志，不会阻止其他已配置服务器启动。

## 单轮预算

```json
{
  "turn": {
    "max_seconds": 1800,
    "max_total_tokens": 300000
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_seconds` | `0` | 单个 Agent 任务的最大实际时间；`0` 表示不限制 |
| `max_total_tokens` | `0` | 单个任务累计输入和输出 token 的最大数量；`0` 表示不限制 |

这些是安全预算，不是工具调用次数限制。

## 主动通知策略

```json
{
  "notifications": {
    "timezone": "UTC",
    "quiet_start": null,
    "quiet_end": null,
    "cooldown_seconds": 1800,
    "daily_budget": 12,
    "urgent_daily_budget": 3,
    "pending_owner_delay_seconds": 30
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `timezone` | `UTC` | 每日计划和通知预算使用的有效 IANA 时区 |
| `quiet_start` | 未设置 | 本地静默时段的 `HH:MM` 开始时间 |
| `quiet_end` | 未设置 | 本地静默时段的 `HH:MM` 结束时间 |
| `cooldown_seconds` | `1800` | 具有相同 key 的排队通知之间的最小间隔 |
| `daily_budget` | `12` | 每个本地日的普通主动通知上限 |
| `urgent_daily_budget` | `3` | 每个本地日的紧急主动通知上限 |
| `pending_owner_delay_seconds` | `30` | 存在待处理主人消息时，主动通知延后投递的时间 |

`quiet_start` 和 `quiet_end` 必须同时省略，或同时设为不同的 `HH:MM` 值。支持跨夜时段。

该策略适用于主动的目标和心跳通知。固定提醒按请求的计划执行。

## 认知心跳

```json
{
  "heartbeat": {
    "enabled": false,
    "initial_delay_seconds": 900,
    "min_interval_seconds": 1800,
    "max_interval_seconds": 21600,
    "max_daily_turns": 12
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用自主心跳评估 |
| `initial_delay_seconds` | `900` | 新 workspace 第一次心跳前的延迟 |
| `min_interval_seconds` | `1800` | Momoi 可选择的最短下次间隔 |
| `max_interval_seconds` | `21600` | Momoi 可选择的最长下次间隔 |
| `max_daily_turns` | `12` | 每个本地日的心跳评估上限 |

间隔必须为正数，最大值不能小于最小值。即使某次心跳保持沉默，也会计入评估次数。

## Webhook

```json
{
  "webhooks": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 8787,
    "token": "replace-with-a-random-token",
    "workflows": "workflows",
    "executors": "workflow-executors.yaml"
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启动 Webhook API 和工作流 worker |
| `host` | `127.0.0.1` | 绑定地址；只有其他机器必须连接时才使用可达网卡 |
| `port` | `8787` | `1` 到 `65535` 的 TCP 端口 |
| `token` | 空 | Bearer token；启用 Webhook 时必填 |
| `workflows` | `workflows` | 包含工作流 YAML 文件的目录 |
| `executors` | `workflow-executors.yaml` | 包含预定义命令执行器的文件 |

使用长随机 token。如果 endpoint 经过不可信网络，请在 Momoi 前部署 TLS 反向代理。继续阅读 [WORKFLOW.zh-CN.md](./WORKFLOW.zh-CN.md)。

## 日志

```json
{
  "logging": {
    "level": "INFO"
  }
}
```

正常运行使用 `INFO`，开发时使用 `DEBUG`。DEBUG 日志可能包含主人消息、模型输出和工具状态，应当作私密数据保护。

## 应用更改

修改 `config.json`、`mcp.json`、`SOUL.md`、工作流文件或执行器定义后，重启 `momoi run`。启动时会验证必需配置，并在连接服务前报告简洁的配置错误。
