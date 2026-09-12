# 配置参考

[EN](./CONFIG.md) | 中文

Momoi 从 workspace 中读取 `config.json`。默认 workspace 是 `~/.momoi`；
在命令前传入 `--workspace` 可选择其他目录。完整的起始配置见
[config.example/config.json](../config.example/config.json)。

相对路径以 `config.json` 所在目录为基准解析。所有路径字段均可使用绝对路径。
`config.json` 不会展开 `${VAR}` 占位符。

外部 API 的端点、凭据和参数统一由 [providers.yaml](./PROVIDERS.zh-CN.md) 管理。
主配置包含 `"providers": "providers.yaml"`，dashboard 模式自动检测服务配置变化并重建业务实例。

升级已有工作区时，先备份 `providers.yaml`，删除旧的 `bindings.asr`（包括禁用的绑定），
并删除它独占的服务和凭据；被其他服务共用的配置应保留。入站语音现在使用渠道自身的转写，
保留旧绑定会因不支持的配置项而阻止启动。

## 运行配置接口

`GET /api/settings/mcp` 原样返回 `tools.mcp_config` 指定的 JSON 文件，未指定时
读取工作区的 `mcp.json`。保留禁用服务和环境变量引用；工作区没有该文件时返回
`{"mcpServers": {}}`，自定义路径不存在时返回 404。
`PATCH /api/settings/mcp` 校验请求中的完整 JSON，替换文件并触发业务运行实例重启。
返回 202 和配置快照，表示保存成功，不代表新实例已启动。两个接口均需 dashboard 认证。

鉴权后请求 `GET /api/settings/configuration`，从 `app` 读取当前配置，
从 `app_fields` 读取控件定义。每组包含 `label` 和 `fields`，字段提供
`type`、`default` 和 `advanced: false`；日志级别通过 `enum` 提供选项，
复盘时间通过 `format: time` 和 `pattern` 指定 `HH:MM`，使用应用配置的时区。

通过 `PATCH /api/settings/configuration/app` 提交获取到的 revision 和需要修改的字段：

```json
{
  "revision": "<读取配置时返回的 revision>",
  "document": {
    "heartbeat": {"enabled": true},
    "logging": {"level": "INFO"},
    "reflection": {"enabled": true, "at": "03:00"},
    "episode_annealing": {"enabled": true}
  }
}
```

可以只提交其中一个字段。这些运行配置会保留未提交的字段，例如心跳间隔、复盘时间和
退火时限。开关必须是 JSON 布尔值；日志级别限定为大写的 `TRACE`、`DEBUG`、
`INFO`、`WARNING`、`ERROR`、`CRITICAL`；时间范围为 `00:00`–`23:59`。
非法参数返回 400，过期 revision 返回 409，均不写入配置。

阶段思考强度位于运行配置 `thinking.stages`，不再位于模型 provider 配置。
前端从 `app_fields.thinking.fields.stages.properties` 递归生成各阶段下拉框，
使用字段的 `label`、`enum`、`default`，放在“运行配置”下。选项为 `""`（跟随模型）、
`low`、`medium`、`high`、`xhigh`、`max`，所有字段标记 `advanced: false`。
五档原样发送，由服务端处理映射，前端无需为 DeepSeek 特殊处理。

```json
{
  "revision": "<读取配置时返回的 revision>",
  "document": {
    "thinking": {
      "stages": {
        "episode_anneal": "low",
        "reply_followup": "low"
      }
    }
  }
}
```

同样提交到 `PATCH /api/settings/configuration/app`。只提交一个阶段时保留其他阶段；
设为 `""` 取消该阶段覆盖，提交空对象不清空已有覆盖。阶段名限定为 metadata 中的键，
未知阶段、非法 effort 或 null 返回 400。未配置阶段使用当前模型的默认 effort。
切换模型不修改阶段配置；运行层只把本次请求的 effort 交给 provider，由 provider 转成协议参数。
旧配置需将 `providers.yaml` 中 `thinking.stages` 移到 `config.json` 顶层 `thinking.stages`，
模型配置只保留 `thinking.effort`，不再接受旧位置的 `stages`。

保存后自动重载业务运行实例，dashboard 和容器保持运行。通过
`GET /api/settings/runtime` 确认 `applied_revision` 等于保存返回的 revision，
且 `state` 为 `running`（未完成基础配置时为 `setup`）。直接修改文件也会在一秒轮询
周期内被检测到。dashboard 模式下无需手动重启容器。

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
Dashboard 模式修改后自动应用，内部可通过 `daemon.bubble_delivery.tts_provider` 访问初始化后的 provider，
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

NapCat 按消息 ID 调用 `fetch_ptt_text` 转写收到的 QQ 语音，需要支持该接口的版本
（已验证 4.18.19；[上游实现](https://github.com/NapNeko/NapCatQQ/pull/1837)）。
QQ 可能异步生成文字，接口拒绝或返回空文字时会分别等待 2、4 秒重试两次，
整个转写过程受 `send_timeout_seconds` 限制。失败时保留 `[QQ 语音消息暂时无法转写]` 提示。
微信直接使用渠道提供的转写文字。两者均无需单独配置识别服务或凭据，
语音合成开关只控制发送语音。

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
| `enabled` | 是 | — | 渠道名称与配置对象；dashboard 设置阶段允许为空，否则必须包含 `primary` |

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

`max_input_tokens` 应低于 Provider 的实际上下文窗口。默认 32–80 Turn transcript
水位与完整请求 token 水位是两道相互独立的保护。Episode 原始证据额度也从同一
压缩水位派生，不再单独配置 token 预算。
同一窗口覆盖对话、Webhook 事件、Goal 执行和 Heartbeat 记录，也包含有记录但没有发消息的
已完成 Turn。对应 ID 索引从保留的 transcript 生成，没有独立的历史条数限制。
Goal 和 Webhook Turn 不运行自动预检索。
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
    "exec_enabled": false,
    "result_max_chars": 12000,
    "result_retention_days": 30
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `mcp_config` | `mcp.json` | MCP 服务器配置路径；`null` 或 `""` 关闭 MCP 加载 |
| `exec_enabled` | `false` | 向模型暴露 `exec` 命令执行工具；设置页的 MCP 工具中可切换 |
| `result_max_chars` | `12000` | 模型可见的单个工具结果分段最大长度；最小值为 `1000` 个字符 |
| `result_retention_days` | `30` | 大结果私有快照的保留天数；`0` 关闭按时间清理 |

`exec` 使用 Bash 执行 `command`，可指定 `cwd` 和 `timeout_seconds`（默认 30 秒，最大 120 秒）。
默认工作目录为 workspace，但不提供沙箱或路径隔离；命令拥有 Momoi 进程的文件、凭据和网络访问权限。
输出保留 stdout/stderr 各自最后 16 KiB，超时或取消时终止进程组。关闭开关同时移除模型工具并拒绝直接调用。
通过 `PATCH /api/settings/configuration/app` 提交 `tools.exec_enabled` 和当前 revision，保存后应用新运行实例。
Webhook 的预配置 `uses: exec` 仍以 argv 执行，不受此模型工具开关影响，也不会引入 Bash 字符串解释。

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
    "max_total_tokens": 0,
    "max_protocol_retries": 3
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_seconds` | `0` | 单个 Turn 的运行时间上限；`0` 表示不限制 |
| `max_total_tokens` | `0` | 累计原始输入/输出 token 上限；`0` 表示不限制 |
| `max_protocol_retries` | `3` | 单个 Turn 内协议/工具错误的统一重试次数；达到上限后熔断并通过 workflow/channel 汇报 |

`max_seconds` 和 `max_total_tokens` 必须为非负数；`max_protocol_retries` 必须为正整数。

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
    "enabled": true,
    "initial_delay_seconds": 900,
    "min_interval_seconds": 1800,
    "max_interval_seconds": 5400
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 启用自动心跳检查 |
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

## 当前状态维护

```json
{
  "current_state": {
    "max_seconds": 180
  }
}
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `max_seconds` | `180` | 当前状态维护单批次的模型运行时间上限，必须为正数 |

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
Dashboard 模式自动检测 `providers.yaml`、`config.json` 和当前 MCP 配置文件的变化。
MCP 修改会重启整个业务运行实例，重建所有 MCP 连接和工具 schema，dashboard 进程继续运行。
格式错误保留当前实例；新实例启动失败时，尝试使用上一份有效配置（包括 MCP 快照）恢复。
修好外部依赖后可点击“重新应用”重试原配置；工作流或执行器修改后也需“重新应用”。存储路径、时区、dashboard
认证及提示词文件路径需要重启进程。纯后台模式的配置修改需要重启。
每个新 Turn 开始前都会重新加载提示词内容。
