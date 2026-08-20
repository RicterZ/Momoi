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
├── prompts/
│   ├── SOUL.md
│   └── HEARTBEAT.md
├── workflows/
│   ├── *.yaml
│   └── workflow-executors.yaml
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
    "max_tokens": 16384,
    "temperature": 0.6,
    "timeout_seconds": 300,
    "max_retries": 3,
    "tool_choice": true
  }
}
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_format` | 否 | `anthropic` | `anthropic` 或 `openai` |
| `base_url` | 是 | — | 兼容 API 的基础 URL |
| `api_key` | 是 | — | API 凭据，不能为空 |
| `model` | 是 | — | 发送给 provider 的模型标识 |
| `max_tokens` | 否 | `16384` | 单次模型调用的最大输出 token 数 |
| `temperature` | 否 | `0.6` | 采样温度 |
| `timeout_seconds` | 否 | `300` | 必须为正数的请求超时时间 |
| `max_retries` | 否 | `3` | 短暂连接和服务端错误的重试次数；OpenAI 兼容端点还会重试成功但不可用的响应 |
| `tool_choice` | 否 | `true` | OpenAI 兼容请求要求模型调用工具；Thinking mode 等不支持 `tool_choice` 的模型需设为 `false` |

对于 Anthropic 兼容 provider，Momoi 请求 `/v1/messages`。对于 OpenAI 兼容 provider，只含 host 的 URL 会请求 `/v1/chat/completions`；已经包含网关路径的 URL 则在该路径下请求 `/chat/completions`。

`config.json` 不会展开 `${VAR}` 占位符。如果其中包含凭据，请妥善保管并限制文件权限。Docker 和一键启动可以用下面的 `MOMOI_*` 环境变量覆盖对应字段；进程里环境变量优先于文件。

| 变量 | 覆盖字段 |
| --- | --- |
| `MOMOI_LLM_API_FORMAT` | `llm.api_format` |
| `MOMOI_LLM_BASE_URL` | `llm.base_url` |
| `MOMOI_LLM_API_KEY` | `llm.api_key` |
| `MOMOI_LLM_MODEL` | `llm.model` |
| `MOMOI_NAPCAT_URL` | NapCat `url` |
| `MOMOI_OWNER_QQ` | NapCat `owner_qq` |
| `MOMOI_PRIMARY` | `channels.primary` |
| `MOMOI_TIMEZONE` | `notifications.timezone` |
| `MOMOI_DASHBOARD_TOKEN` | `dashboard.token` |
| `MOMOI_WEBHOOKS_ENABLED` | `webhooks.enabled` |
| `MOMOI_WEBHOOKS_HOST` | `webhooks.host` |
| `MOMOI_WEBHOOKS_TOKEN` | `webhooks.token` |
| `MOMOI_USAGE_API_KEY` | `usage.api_key` |
| `MOMOI_ASR_SECRET_ID` | `asr.settings.secret_id` |
| `MOMOI_ASR_SECRET_KEY` | `asr.settings.secret_key` |

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
| `enabled` | `false` | 为 NapCat 入站语音启用 ASR |
| `provider` | `tencent` | 内置 `tencent`，或 `ASRProvider` 子类的 Python 点分类名 |
| `timeout_seconds` | `30` | 单次 provider 请求的正数超时 |
| `max_audio_bytes` | `3145728` | 送入 provider 前允许的最大音频字节数 |
| `settings` | `{}` | 传给 provider 构造函数的参数 |

内置 Tencent provider 使用一句话识别接口。启用时必须填写 `secret_id` 和 `secret_key`；`region` 可留空，`engine` 默认 `16k_zh`。NapCat 会先通过 `get_record` 把独占语音气泡转换为 MP3，再交给 provider。成功后，Channel 对下层只产生一条普通文本消息；ASR 关闭或失败时产生 `[QQ 语音消息暂时无法转写]` 文本占位。数据库、批处理和 LLM 不接收音频路径、`record` segment 或 ASR 状态。

当前外部 ASR provider 只注入 NapCat。微信继续使用平台返回的 `voice_item.text`，不会调用这里配置的 provider。

自定义 provider 需继承 `momoi.asr.ASRProvider` 并实现异步 `transcribe(AudioInput) -> str`。`timeout_seconds` 会和 `settings` 一起作为构造参数传入。

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
| `quiet_seconds` | 否 | `1` | 收到主人最新消息后等待的时间；新消息会重新计时 |
| `max_batch_seconds` | 否 | `60` | 持续增长的消息批次最长可等待的时间 |
| `heartbeat_seconds` | 否 | `30` | NapCat 连接心跳间隔 |
| `reconnect_max_seconds` | 否 | `30` | 重连退避的最大时间 |
| `send_timeout_seconds` | 否 | `20` | 单次 NapCat 出站请求的超时时间 |
| `media_max_bytes` | 否 | `20971520` | 为模型输入物化单张远程入站图片时允许的最大大小 |
| `media_download_timeout_seconds` | 否 | `15` | 下载单张远程入站图片的超时时间 |

所有渠道时间字段都必须为正数。

初始模板显式使用 6 秒，以便把一组自然连续的短消息一起处理；省略该字段时，运行时默认值为 1 秒。

NapCat 的主人输入状态通知会刷新同一个安静窗口，即使 Owner Turn 已经开始运行也一样。在模型和工具调用边界，Momoi 会等待主人停止输入，并把这期间真正到达的消息合并进当前 Turn；等待仍受 `max_batch_seconds` 限制。输入状态只是瞬时活动信号，不会作为对话消息持久化，也不会发送给模型。

### 微信（腾讯 iLink）

在同一个 workspace 中扫码登录一次，再运行 daemon：

```bash
momoi --workspace ~/.momoi channel login weixin
momoi --workspace ~/.momoi run
```

登录命令会在终端打印二维码。凭据、更新游标和最新对话 context token 会以原子写入、权限 `0600` 的方式保存在 `channel/weixin/state.json`。解密后的入站附件保存在 `channel/weixin/media/inbound/`；它们都属于需要保护和备份的 workspace 数据。

微信渠道可接收文本、引用、图片、视频、文件和语音。图片会交给支持视觉的模型，其他媒体以本地附件描述进入上下文；优先使用服务端语音转写，无转写时保留原始 SILK。可发送文本、图片、视频和文件；出站音频按文件附件发送。`media_max_bytes` 是入站下载，以及出站本地路径、HTTP(S) 或 `base64://` 来源的正整数大小上限。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `quiet_seconds` | `6` | 收到主人最新消息后开始处理前的等待时间 |
| `max_batch_seconds` | `60` | 持续增长的消息批次最长可等待的时间 |
| `reconnect_max_seconds` | `30` | 连续更新失败后的最大等待时间 |
| `send_timeout_seconds` | `20` | 单次出站请求的超时时间 |
| `media_max_bytes` | `104857600` | 单个入站或出站媒体允许的最大大小 |

该实现按腾讯 MIT 许可的 [`@tencent-weixin/openclaw-weixin` 2.4.6](https://github.com/Tencent/openclaw-weixin) 协议行为实现。使用微信与 iLink 仍须遵守适用的腾讯及微信服务条款。Momoi 仅支持一个已绑定账号及扫码的唯一主人，不支持群聊或多账号同时在线。

### 接入新的 Channel

一个 Channel 插件对应 `momoi.channel` 下的一个模块或 package，并导出 `load_config(value, workspace)` 与 `create_channel(config)`。创建出的 Channel 提供唯一的 `name`、运行时 prompt 上下文、消息批处理时间、`run`、`send_message`、`content_blocks` 和 Workflow 变量。入站事件用插件名标识来源，因此 NapCat 事件使用 `napcat:`，微信事件使用 `weixin:`；`qq:` 留给未来的 QQ 官方 AI Bot 插件。

协议专属的解析、内容渲染和连接日志都留在插件模块内。daemon、store 与 webhook 层只使用通用 Channel 接口。

## 上下文与记忆预算

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
    "summary_results": 12,
    "summary_tokens": 6000,
    "recent_episode_hours": 6
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | 相对于 workspace 的人格文件 |
| `heartbeat_prompt` | `prompts/HEARTBEAT.md` | workspace 心跳指引；文件不存在时不注入 |
| `recent_raw_tokens` | `32000` | 近期 Turn 标准记录的预算 |
| `recent_turns` | `6` | 纳入考虑的近期已完成 Turn 上限 |
| `planner_recent_base_turns` | `recent_turns` | Planner 缓存稳定块的 Turn 数 |
| `planner_recent_append_turns` | `recent_turns` | Planner 在切换稳定块前最多追加的 Turn 数 |
| `planner_active_recent_turns` | `recent_turns` | Planner 默认作为当前对话焦点的最新 Turn 数 |
| `planner_recent_tokens` | 自动 | Planner 专用 Recent 日志预算；默认不超过 `88000`，并随输入预算缩小 |
| `memory_results` | `6` | 自动召回的持久记忆最大数量 |
| `memory_tokens` | `8000` | 召回持久记忆的 token 预算 |
| `max_input_tokens` | `96000` | 包括工具 schema 在内的完整模型输入目标上限 |
| `summary_results` | `12` | 关键词召回最多返回的 Episode 数量，之后再与近期 Episode 合并 |
| `summary_tokens` | `6000` | 召回对话片段的 token 预算 |
| `recent_episode_hours` | `6` | 默认注入最近多少小时内活跃的 Episode 摘要；设为 `0` 可关闭 |

`max_input_tokens` 应低于 provider 真实的上下文窗口。这些数值是构建上下文的预算，不代表每个 provider 都会以相同方式计算 token。

`recent_raw_tokens` 和 `recent_turns` 继续控制主模型的近期 Turn。Context Planner 使用独立的分块日志：保留一个稳定 Base，在其后追加新 Turn；追加块满后，新块成为下一轮 Base。`planner_active_recent_turns` 标记真正的当前焦点，较旧但仍在缓存块中的 Turn 只用于明确引用、未完成工作、工具结果和纠错。Planner 投影保留工具名称和完整参数；成功、内部可见性、Provenance等默认包装不重复，状态变更工具只返回关键最终状态。Active Turn保留完整工具结果，非Active背景Turn的大型文件、目录、搜索或MCP结果使用带原始大小/数量和头尾预览的结构化截断。历史消息省略可推导的 trust、已送达状态和逐项时间戳，Turn 级 `at` 提供时间锚点，空 Final 和重复的历史 intent 原文不发送。非默认投递状态、失败、副作用、等待、情绪变化和 mutation 仍会显式保留。`planner_recent_tokens` 未配置时取 `max_input_tokens × 55%` 与 `88000` 中较小者；这是为动态候选、Provider 计数偏差和约 30% 输入空白预留空间。

`recent_episode_hours` 会加入配置窗口内全部活跃的 Episode，与关键词召回相互独立；`summary_results` 默认将关键词召回限制为最多 12 个 Episode。两组结果按 Episode 去重后排序：近期且命中关键词的优先，其次是其他关键词命中，最后是仅近期活跃的 Episode；在关键词组内，命中的关键词 alternative 越多越靠前。`summary_tokens` 由合并后的 Episode 摘要共同使用。

Owner主模型使用同一套紧凑Recent投影。稳定的Owner Preferences、Core Reflection和近期记忆位于当前Owner消息之前，以便复用前缀；动态当前消息保持在末尾。内部Memory、Conversation、Thinking、Agenda和Builtin工具全部常驻。Context Planner只选择当前需要的外部MCP Server，并必须给出路由理由；主模型始终通过`tool_enable`获得紧凑的Server/Tool目录，可在Turn中随时加载漏选Server。Heartbeat Planner现在采用相同的Handoff模式：选择一个活动，指出最多两个需要Heartbeat Turn执行的Memory/Conversation查询，路由外部MCP Server，并提交有界执行大纲。Heartbeat查询不再由框架自动执行；真正的`rest`计划不带查询、MCP Server或执行步骤。自主工具继续受Autonomy Pattern限制。Planner降级时不预载MCP，但保留`tool_enable`，无需恢复全部外部Schema也不会失能。

Owner Context Planner不再提交关键词让框架自动搜索Memory/Episode。它输出结构化`owner_handoff`：判断现有上下文是否充分、列出最多两个需要Owner主模型执行的Memory/Conversation/Thinking查询、选择MCP Server，并给出执行模式和大纲。框架仍提供Recent Episodes、近期/核心记忆和当前Goals/Reminders作为确定性基线；精确历史补查由始终常驻的内部工具在Owner Turn中完成。主模型可修正Planner方向，并仅在实际推翻Handoff时通过`plan_adjustment`把修正写回后续Recent Turns。

将某个基线上下文层的结果数量或 token 预算设为 `0` 可关闭该层注入。显式记忆和对话搜索工具仍然常驻可用。

## 存储

```json
{
  "storage": {
    "database": "data/momoi.sqlite3",
    "thinking": null
  }
}
```

`database` 为必填项。相对路径以 workspace 为基准解析，父目录会自动创建。

`thinking` 是按月存放模型思考记录的目录（`thinking-YYYY-MM.sqlite3`）。省略或设为 `null` 时，使用与 `database` 相同的目录。相对路径以 workspace 为基准解析。

请备份完整 workspace，以保留对话历史、记忆、目标、提醒、情绪素材、待投递状态和思考记录。

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

内置文件工具会从 workspace 解析相对路径，同时仍支持绝对路径。

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
    "max_interval_seconds": 5400
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 启用自主心跳评估 |
| `initial_delay_seconds` | `900` | 新 workspace 第一次心跳前的延迟 |
| `min_interval_seconds` | `1800` | Momoi 可选择的最短下次间隔 |
| `max_interval_seconds` | `5400` | Momoi 可选择的最长下次间隔（默认 90 分钟） |

所有间隔必须为正数，最大间隔不能小于普通最短间隔。

存在非空的 `prompts/HEARTBEAT.md` 时，其内容会作为 workspace 的 heartbeat 指引自动追加；文件不存在时不注入额外提示词。可用 `context.heartbeat_prompt` 改路径。

心跳可以使用明确允许的只读工具、搜索记忆、在 `<workspace>/artifacts` 下生成文件，或者为需要跨轮继续的工作创建 agent-owned Goal。它会先记录真实结果，再决定是否有必要联系主人。主人 Goal 和提醒由各自的调度器负责，心跳不会代替或模仿它们。

在主人私聊中发送 `/heartbeat` 可以立即触发一次心跳，即使自动心跳没有开启。已有心跳正在排队或执行时，重复命令会自动去重。

当 `respond` 创建回复等待时，Momoi 会一次性选择 1–10 分钟的截止时间、期望主人回复的信息，以及超时后必须跟进的私有理由。计时只在最后一条可见消息成功送达后开始。来源 Turn 仍按事务正常完成，但期望存续期间会暂停它的 Episode Consolidation 和 Anneal。主人在截止前发来消息会原子取消定时器，并把期望和理由作为上下文交给这次 Owner Turn；若截止时间先到，Runtime 只触发一次必发跟进，不再询问模型是否保持沉默或继续等待。跟进消息并入来源 Turn 的对话时间线；独立执行记录仅用于重试、用量和审计，不推进 Recent Turn 计数。回复关注使用独立时钟，不会覆盖普通的 `next_heartbeat_at` 节奏。

新机制不解析旧版纯文本 `outbox.reply_expectation`。从旧的两轮检查机制升级前，应先确保 Outbox 已清空。

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

默认允许 `curl`、`read_file`、`write_file` 和 `list_dir`。`curl` 仅允许 GET、HEAD 和 OPTIONS；自主文件访问仅限 `<workspace>/artifacts`。MCP 工具必须同时出现在这里，并通过 `readOnlyHint` 或 `readOnlyTools` 判定为只读。agent-owned Goal 继承同一边界；主人创建的 Goal 继续使用主人任务授权的工具。

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

在主人私聊中发送 `/reflect` 可以立即复盘当前本地自然日，即使自动复盘没有开启。当天复盘正在执行时，重复命令会被忽略；已完成的当天可以再次复盘。

## Episode 历史维护

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
| `enabled` | `true` | 在后台维护较早的对话 Episode |
| `idle_seconds` | `60` | 开始维护前要求主人保持空闲的时间 |
| `max_seconds` | `650` | 单次维护允许使用模型的最长时间 |

维护请求会自动合并，并且一次只处理一个 Episode 批次。新的主人消息会取消正在进行的维护且不计为失败；主人再次空闲后，该工作可以重新领取。

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
| `enabled` | `false` | 启动 Webhook API 和工作流 worker |
| `host` | `127.0.0.1` | 绑定地址；只有其他机器必须连接时才使用可达网卡 |
| `port` | `8787` | `1` 到 `65535` 的 TCP 端口 |
| `token` | 空 | Bearer token；启用 Webhook 时必填 |
| `workflows` | `workflows` | 包含工作流 YAML 文件的目录 |
| `executors` | `workflows/workflow-executors.yaml` | 包含预定义命令执行器的文件；可放在工作流目录内，加载时会自动跳过 |

使用长随机 token。如果 endpoint 经过不可信网络，请在 Momoi 前部署 TLS 反向代理。继续阅读 [WORKFLOW.zh-CN.md](./WORKFLOW.zh-CN.md)。

## Web 看板

本机 Web 看板是 Momoi 生活记录的一面小窗：聊天、每日复盘、记忆、表情包、任务和模型思考记录都可以在这里翻看；需要时也能改记忆、管表情、调整进行中的 Goals。思考记录只读。

只有带上 `momoi run --dashboard` 才会启动。监听地址和端口由命令行参数决定；`config.json` 里放的是进入页面用的通行证。

```json
{
  "dashboard": {
    "token": "replace-with-a-long-random-secret"
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `token` | 空 | 启用 `--dashboard` 时必填的通行证。在页面上输入后即可进入；浏览器大约会记住一年 |

默认打开 `http://127.0.0.1:8788`。可用 `--dashboard-host` 和 `--dashboard-port` 修改监听地址。请只在本机或可信网络使用，不要直接暴露到公网。

## Usage

看板会在本地记录每一次 LLM 调用。Usage 插件提供官方单价，也可以再提供实时余额，这样页面才能估算费用。`provider` 留空时仍会统计请求和 token，只是没有估价。

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
| `provider` | 空 | `UsagePlugin` 的点分类名 |
| `api_key` | 空 | 作为 `api_key` 传给插件构造函数 |
| 其他字段 | — | 作为构造函数关键字参数转发 |

自带的 DeepSeek 插件按人民币 / 百万 token（缓存命中、未命中、输出）给 `deepseek-v4-flash` 和 `deepseek-v4-pro` 计价；设置了 `api_key` 时会读取 `/user/balance`。`base_url` 和 `timeout_seconds` 可选。

修改 `usage` 后需要重启 `momoi run`。该类必须能从 Momoi 的 Python 环境里 import。

### 编写插件

继承 `momoi.extensions.UsagePlugin`，实现 `token_rates`。如果看板要显示账户余额，再实现 `balance`。默认的 `parse_usage` 只认 OpenAI / Anthropic 的 usage 字段；如果提供商用另一套计费字段（例如 DeepSeek 的 `prompt_cache_hit_tokens`），覆盖 `parse_usage`。默认的 `estimate_cost` 用这些单价乘 token 数；只有计价不是线性时才需要覆盖它。

```python
from momoi.extensions import UsagePlugin


class FlatRatePlugin(UsagePlugin):
    def __init__(
        self,
        *,
        api_key: str = "",
        input_cny: float = 2.0,
        output_cny: float = 8.0,
    ) -> None:
        self.input_cny = float(input_cny)
        self.output_cny = float(output_cny)

    def token_rates(self, model: str, timestamp: float) -> tuple[float, float, float]:
        # 人民币 / 百万 token：缓存命中、未命中、输出
        return (self.input_cny, self.input_cny, self.output_cny)

    async def balance(self) -> dict[str, object]:
        return {
            "source": "unavailable",
            "currency": "CNY",
            "is_available": False,
            "total_balance": "0",
        }
```

把 `provider` 指到这个点分类名。其余字段会变成构造参数：

```json
{
  "usage": {
    "provider": "my_package.usage.FlatRatePlugin",
    "input_cny": 2.0,
    "output_cny": 8.0
  }
}
```

`balance()` 必须包含 `source`、`currency`、`is_available` 和 `total_balance`。当前有效的余额用 `source` `"live"`，拿不到时用 `"unavailable"`。

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
| `level` | `DEBUG` | Python 日志级别；支持 `TRACE`、`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` |

正常运行使用 `INFO`，开发时使用 `DEBUG`。`TRACE` 还会把发给 LLM 的完整请求和原始响应写到 workspace 的 `llm-dumps/`。DEBUG 和 TRACE 日志可能包含主人消息、模型输出和工具状态，应当作私密数据保护。

## 应用更改

`SOUL.md` 和 `HEARTBEAT.md` 会在每个新 Turn 开始时重新读取，修改后无需重启。修改 `config.json`、`mcp.json`、工作流文件或执行器定义后，仍需重启 `momoi run`。启动时会验证必需配置，并在连接服务前报告简洁的配置错误。
