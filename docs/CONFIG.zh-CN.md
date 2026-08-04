# 配置与能力访问

[EN](./CONFIG.md) | 中文

Momoi 从 workspace 读取配置。默认 workspace 是 `~/.momoi`；把 `--workspace` 放在任意命令前可选择其他目录。

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
├── HEARTBEAT.md
├── prompts/
│   └── SOUL.md
├── workflows/
│   └── *.yaml
├── workflow-executors.yaml
├── emotion/
├── channel/
│   └── weixin/       # 启用微信渠道时创建
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

## Channel

Momoi 可以同时运行多个 Channel 插件。它们共享同一段对话、记忆、目标、情绪与身份。主人在哪个渠道开口就在哪里回复；新发起的主动消息使用 `primary`。

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
        "send_timeout_seconds": 20
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

`primary` 必须对应 `enabled` 中的一个插件。某个渠道断线时，它自己的消息会继续排队，但不会阻塞其他渠道；Momoi 也不会悄悄把消息改发到另一个平台。旧的单一 `channel.plugin/settings` 格式仍可作为只有一个 primary 的配置使用。

`napcat` 表示第三方适配器；未来的 QQ 官方 AI Bot 适配器将使用独立的 `qq` 插件名。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `url` | 是 | — | NapCat WebSocket URL |
| `owner_qq` | 是 | — | 仅包含数字、被认可为主人的 QQ 号 |
| `quiet_seconds` | 否 | `6` | 收到主人最新消息后等待的时间；新消息会重新计时 |
| `max_batch_seconds` | 否 | `60` | 持续增长的消息批次最长可等待的时间 |
| `heartbeat_seconds` | 否 | `30` | NapCat 连接心跳间隔 |
| `reconnect_max_seconds` | 否 | `30` | 重连退避的最大时间 |
| `send_timeout_seconds` | 否 | `20` | 单次 NapCat 出站请求的超时时间 |

所有渠道时间字段都必须为正数。

6 秒可以把一组自然连续的短消息一起处理。只有在开发时更看重反馈速度而不是消息收集时，才建议降到 1 秒。

### 微信（腾讯 iLink）

在同一个 workspace 中扫码登录一次，再运行 daemon：

```bash
momoi --workspace ~/.momoi channel login weixin
momoi --workspace ~/.momoi run
```

登录命令会在终端打印二维码。凭据、更新游标和最新对话 context token 会以原子写入、权限 `0600` 的方式保存在 `channel/weixin/state.json`。解密后的入站附件保存在 `channel/weixin/media/inbound/`；它们都属于需要保护和备份的 workspace 数据。

微信渠道可接收文本、引用、图片、视频、文件和语音。图片会交给支持视觉的模型，其他媒体以本地附件描述进入上下文；优先使用服务端语音转写，无转写时保留原始 SILK。可发送文本、图片、视频和文件；出站音频按文件附件发送。`media_max_bytes` 是入站下载，以及出站本地路径、HTTP(S) 或 `base64://` 来源的正整数大小上限。

该实现按腾讯 MIT 许可的 [`@tencent-weixin/openclaw-weixin` 2.4.6](https://github.com/Tencent/openclaw-weixin) 协议行为实现。使用微信与 iLink 仍须遵守适用的腾讯及微信服务条款。Momoi 仅支持一个已绑定账号及扫码的唯一主人，不支持群聊或多账号同时在线。

### 接入新的 Channel

一个 Channel 插件对应 `momoi.channel` 下的一个模块或 package，并导出 `load_config(value, workspace)` 与 `create_channel(config)`。创建出的 Channel 提供唯一的 `name`、运行时 prompt 上下文、消息批处理时间、`run`、`send_message`、`content_blocks` 和 Workflow 变量。入站事件用插件名标识来源，因此 NapCat 事件使用 `napcat:`，微信事件使用 `weixin:`；`qq:` 留给未来的 QQ 官方 AI Bot 插件。

协议专属的解析、内容渲染和连接日志都留在插件模块内。daemon、store 与 webhook 层只使用通用 Channel 接口。

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

`recent_raw_tokens` 默认保持 32k。它不是把数据库历史硬裁到固定消息数，也不是把长期记忆降成 8k：Owner Turn 会先用这份预算保留最近 `recent_turns` 个完整交互，再把余额给当前规划命中的 Episode 原文。Planner 在主模型聊天前会先读取同一预算内的近期原文，把新消息拆成独立 intent、解析“刚才那个”一类指代，并生成多组 recall query。扩展后的 query 会搜索完整 Episode 索引，因此目标 Episode 即使不在最近 64 条目录候选中仍可召回；64 条只是给 Planner 直接复用 Episode id 的有界语义目录，不是长期历史边界。

所有原始消息永久留在 SQLite。较旧 Episode 只把主模型的工作集退火成带 `message_id`、Turn ordinal 和精确原文引用的证据摘要；引用在提交前会回查原始消息。旧版本的自由摘要会标为 `UNVERIFIED`，不能作为事实使用。已确认送达、送达不确定、内部记录、排队和失败消息也分别保存，只有确认送达的助理消息能直接证明“主人看到了桃衣说的话”。

Goal 和 Reminder 目录只帮助 Planner 识别当前指代：相关项优先且各最多 8 条；主模型上下文只注入当前 intent 实际召回的项。`done`/`cancelled` Goal 保留作审计，但默认不会注入。项目没有每日 token budget；`turn.max_seconds` 和 `turn.max_total_tokens` 只限制单个 Turn。

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

#### 连接 gog Gmail 与 Calendar

可以直接使用 `gog` 自带的只读 MCP 服务器：

```json
{
  "mcpServers": {
    "gog": {
      "command": "/opt/homebrew/bin/gog",
      "args": [
        "--account", "you@gmail.com",
        "--readonly",
        "--no-input",
        "mcp",
        "--allow-tool", "gmail,calendar"
      ]
    }
  }
}
```

用 `gog --account you@gmail.com mcp --allow-tool gmail,calendar --list-tools` 预先检查实际开放的工具。上述配置只允许读取 Gmail 和 Calendar；需要写操作时再显式扩大权限。

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

部分 MCP 服务器没有提供标准的 `readOnlyHint`。如果某个工具确定只读，可以在对应服务器配置中用 `readOnlyTools` 填写服务器原始工具名。这个声明本身不会把工具开放给自主工作；还必须把带前缀的工具名加入 `autonomy.allowed_tools`。

## 单轮预算

```json
{
  "turn": {
    "max_seconds": 1800,
    "max_total_tokens": 0
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_seconds` | `0` | 单个 Agent 任务的最大实际时间；`0` 表示不限制 |
| `max_total_tokens` | `0` | 单个任务跨模型调用累计的原始输入和输出 token，包含重复或缓存输入；`0` 表示不限制 |

这些是安全预算，不是工具调用次数限制。

## 主动通知策略

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
| `timezone` | `UTC` | 计划与静默时段使用的有效 IANA 时区 |
| `quiet_start` | 未设置 | 本地静默时段的 `HH:MM` 开始时间 |
| `quiet_end` | 未设置 | 本地静默时段的 `HH:MM` 结束时间 |
| `cooldown_seconds` | `1800` | 具有相同 key 的主动联系之间的最小间隔 |
| `pending_owner_delay_seconds` | `30` | 存在待处理主人消息时，耐久主动通知延后投递的时间 |

`quiet_start` 和 `quiet_end` 必须同时省略，或同时设为不同的 `HH:MM` 值。支持跨夜时段。

该策略适用于主动的目标和心跳联系。Goal 通知是耐久的，会移动到下一个可投递时间；Heartbeat 对话是瞬时的，若当前不能立即投递就保持静默，不会保留文字稍后重放。固定提醒按请求的计划执行。

## 自主心跳

```json
{
  "heartbeat": {
    "enabled": false,
    "initial_delay_seconds": 900,
    "min_interval_seconds": 1800,
    "max_interval_seconds": 5400,
    "reply_initial_interval_seconds": 60
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用自主心跳评估 |
| `initial_delay_seconds` | `900` | 新 workspace 第一次心跳前的延迟 |
| `min_interval_seconds` | `1800` | Momoi 可选择的最短下次间隔 |
| `max_interval_seconds` | `5400` | Momoi 可选择的最长下次间隔（默认 90 分钟） |
| `reply_initial_interval_seconds` | `60` | 主动等待主人回复时，第一次心跳前的延迟 |

所有间隔必须为正数，最大间隔不能小于普通最短间隔。

配置文件同目录下存在非空的 `HEARTBEAT.md` 时，其内容会作为 workspace 的 heartbeat 指引自动追加；文件不存在时不注入额外提示词。

心跳可以使用明确允许的只读工具、搜索记忆、在 `<workspace>/artifacts` 下生成文件，或者为需要跨轮继续的工作创建 agent-owned Goal。它会先记录真实结果，再决定是否有必要联系主人。主人 Goal 和提醒由各自的调度器负责，心跳不会代替或模仿它们。

在主人私聊中发送 `/heartbeat` 可以立即触发一次心跳，即使自动心跳没有开启。已有心跳正在排队或执行时，重复命令会自动去重。

当一条已送达回复明确期待主人回应时，Momoi 会在 `reply_initial_interval_seconds` 后提高注意力，即使自动心跳未开启。回复关注使用独立时钟，不会覆盖普通的 `next_heartbeat_at` 节奏。程序会在约第 1、3、7 分钟做三次短时检查；模型每次独立判断是否跟进以及是否继续等待。第三次之后即使仍在等待，也会恢复普通心跳节奏。主人发来任何新消息都会结束旧的等待状态、取消已安排的检查，并取消尚未送达的跟进。

Owner Turn 独占对主人输入的回复权。存在未处理的主人消息、正在执行的 Owner Turn，或尚未送达的 Owner 回复时，心跳会被推迟。心跳会记录自己读取的主人事件版本；若提交前对话已经变化，可见输出会被丢弃，内部心跳活动仍会保留。

## 自主工具白名单

```json
{
  "autonomy": {
    "allowed_tools": [
      "curl",
      "read_file",
      "write_file",
      "mcp__brave-search__brave_web_search"
    ]
  }
}
```

默认允许 `curl`、`read_file` 和 `write_file`。`curl` 仅允许 GET、HEAD 和 OPTIONS；自主文件访问仅限 `<workspace>/artifacts`。MCP 工具必须同时出现在这里，并通过 `readOnlyHint` 或 `readOnlyTools` 判定为只读。agent-owned Goal 继承同一边界；主人创建的 Goal 继续使用主人任务授权的工具。

## 每日复盘

```json
{
  "reflection": {
    "enabled": true,
    "at": "03:00"
  }
}
```

复盘使用 `notifications.timezone`，在每天本地时间 `03:00` 回顾刚结束的自然日。`at` 接受 `HH:MM`，默认值为 `03:00`；未配置时默认关闭，示例 workspace 默认开启。

复盘不会联系主人或获得外部工具。完整摘要和候选学习保存在 SQLite 的 `reflections` 表；筛选后的长期学习保存在 `reflection_memories`，并以低于主人原话记忆的权限进入后续上下文。主人画像和偏好只有在复盘项包含当天主人原文证据时才会写入。

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

`SOUL.md` 和 `HEARTBEAT.md` 会在每个新 Turn 开始时重新读取，修改后无需重启。修改 `config.json`、`mcp.json`、工作流文件或执行器定义后，仍需重启 `momoi run`。启动时会验证必需配置，并在连接服务前报告简洁的配置错误。
