# Webhook 工作流

[EN](./WORKFLOW.md) | 中文

工作流让外部系统可以在 Momoi 中触发一段预先定义的执行序列。

当 Home Assistant、Jellyfin、监控系统或其他服务已经知道某个事件发生时，使用工作流。如果是 Momoi 需要在对话或目标中自行决定是否调用某项能力、以及如何调用，则使用 MCP。

## 启用 Webhook 服务

在 `config.json` 中配置：

```json
{
  "webhooks": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8787,
    "token": "replace-with-a-random-token",
    "workflows": "workflows",
    "executors": "workflows/workflow-executors.yaml"
  }
}
```

只有其他机器必须访问 Momoi 时，才使用 `0.0.0.0` 或指定的局域网地址。使用 Bearer token 保护 endpoint；经过不可信网络时，应部署 TLS 反向代理。

添加或修改工作流后需要重启 Momoi。

## 创建消息工作流

创建 `workflows/event-message.yaml`：

```yaml
version: 1
id: event-message
description: Generate a natural notification from an event prompt

inputs:
  event_prompt:
    type: string
    required: true
    max_length: 2000

steps:
  - id: notify
    uses: message
    prompt: "${inputs.event_prompt}"
```

使用与已声明输入匹配的 JSON 调用：

```bash
curl -X POST http://127.0.0.1:8787/webhooks/event-message \
  -H "Authorization: Bearer $MOMOI_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"event_prompt":"The washing machine has finished. Remind the owner to collect the laundry."}'
```

Momoi 会获得当前对话上下文、相关记忆、目标、提醒、情绪、活动和情绪素材库。该事件不会被当作主人说的话，但最终消息仍由同一个 Momoi 发出。

`message` 步骤可以使用 HTTP 获取事件 prompt 所需的数据，然后必须先把事件写入对话时间线。是否向主人发可见消息另判，可以静默结束。它不能调用任意 MCP 或文件工具。

## 添加固定命令步骤

命令步骤与工作流分开定义。模型永远不会编写或修改命令行。工作流只能把已声明、已验证的输入传入固定参数位置。

通用示例会先检查一个 HTTP endpoint，然后发送消息。

`workflows/workflow-executors.yaml`：

```yaml
version: 1
executors:
  http-check:
    parameters:
      target_url:
        type: string
        required: true
        format: url
        schemes: [http, https]
    argv:
      - python3
      - -c
      - "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=15).close()"
      - "${args.target_url}"
    env: {}
    timeout_seconds: 30
```

`workflows/url-check-event.yaml`：

```yaml
version: 1
id: url-check-event
description: Check an HTTP endpoint, then notify the owner if it succeeds

inputs:
  event_prompt:
    type: string
    required: true
    max_length: 2000
  target_url:
    type: string
    required: true
    format: url
    schemes: [http, https]

steps:
  - id: check-endpoint
    uses: exec
    executor: http-check
    args:
      target_url: "${inputs.target_url}"

  - id: notify
    uses: message
    prompt: "${inputs.event_prompt}"
```

调用方式：

```bash
curl -X POST http://127.0.0.1:8787/webhooks/url-check-event \
  -H "Authorization: Bearer $MOMOI_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"event_prompt":"The monitored service is healthy again. Notify the owner.","target_url":"https://status.example.com/health"}'
```

步骤按顺序执行。如果命令无法启动、以非零状态退出或超时，后续步骤不会执行。

## 工作流参考

工作流目录中的每个 `*.yaml` 文件都定义一个工作流。

```yaml
version: 1
id: lowercase-workflow-id
description: Optional human-readable description
inputs: {}
steps: []
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `version` | 是 | 必须为 `1` |
| `id` | 是 | `/webhooks/{id}` 使用的路由 ID |
| `description` | 否 | 人类可读的用途说明 |
| `inputs` | 否 | 已声明的 JSON 请求字段 |
| `steps` | 是 | 由 `message` 或 `exec` 步骤组成的非空有序列表 |

工作流、步骤、执行器、输入和参数标识符必须以小写字母开头，后续可以包含小写字母、数字、`_` 或 `-`，最长 64 个字符。

加载工作流时会拒绝未知字段。

## 输入 schema

```yaml
inputs:
  label:
    type: string
    required: true
    max_length: 200
    pattern: "[A-Za-z0-9 _-]+"
  target_url:
    type: string
    required: true
    format: url
    schemes: [https]
  retry_count:
    type: integer
    required: false
  threshold:
    type: number
    required: false
  active:
    type: boolean
    required: false
```

| 字段 | 适用范围 | 说明 |
| --- | --- | --- |
| `type` | 所有输入 | `string`、`integer`、`number` 或 `boolean` |
| `required` | 所有输入 | 默认为 `false` |
| `max_length` | 字符串 | 正数字符数限制 |
| `pattern` | 字符串 | 完整正则表达式匹配 |
| `format: url` | 字符串 | 要求带有 hostname 的绝对 URL |
| `schemes` | URL 字符串 | 非空允许列表；默认为 `http` 和 `https` |

请求 body 只能包含已声明的输入。缺少必填输入、存在未知输入、JSON 类型错误、非有限数字或无效 URL 时返回 HTTP `400`。

`exec` 步骤引用的输入必须存在，因为命令参数不能省略。

## Message 步骤

```yaml
- id: notify
  uses: message
  prompt: "Service ${inputs.service_name} changed state: ${inputs.state}"
```

`prompt` 必须是非空字符串。`${inputs.<name>}` 可以出现在任何位置，其引用的每个名称都必须已由工作流声明。

prompt 用来描述发生了什么，以及 Momoi 应该传达什么。它不应包含凭据，也不应伪装成主人说的话。

## Exec 步骤

```yaml
- id: run-action
  uses: exec
  executor: action-name
  args:
    target: "${inputs.target}"
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 工作流内唯一的步骤 ID |
| `uses` | 是 | `exec` |
| `executor` | 是 | `workflows/workflow-executors.yaml` 中的名称 |
| `args` | 是 | 已声明执行器每个参数各一项 |

每个参数值必须完整等于一个 `${inputs.<name>}` 模板。会拒绝 `prefix-${inputs.target}` 这样的部分插值。前缀和 flag 应使用独立的固定 `argv` token。

## 执行器参考

```yaml
version: 1
executors:
  action-name:
    parameters: {}
    argv: [command, fixed-argument]
    env: {}
    timeout_seconds: 180
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `parameters` | 否 | 工作流步骤可传入的已验证参数 |
| `argv` | 是 | 不经过 shell，直接传给进程的非空数组 |
| `env` | 否 | 附加的进程环境 |
| `timeout_seconds` | 否 | 必须为正数的超时时间，默认为 `180` |

执行器参数使用与工作流输入相同的 schema。

`argv` 和 `env` 中的值可以是静态字符串，也可以是一个完整模板：

- `${args.<parameter>}`——已验证的执行器参数
- `${config.owner_id}`——primary Channel 提供的主人标识
- `${config.channel_url}`——primary Channel 提供的连接 URL（如果有）

已配置插件也会提供各自的命名变量，例如 NapCat 的 `${config.owner_qq}`、`${config.napcat_url}`，以及微信的 `${config.weixin_user_id}`。

模板不能嵌入较大的 token 中。执行器会继承 Momoi 现有的进程环境，因此命令可以从环境变量读取 secret，无需把它放入工作流输入。

不要定义通用 shell 执行器，也不要从请求 body 接收命令字符串。应为每个允许的操作定义狭窄的专用执行器，并验证所有动态参数。

## HTTP API

### 启动工作流

```text
POST /webhooks/{workflow_id}
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <optional-key>
```

成功请求返回 HTTP `202`：

```json
{
  "run_id": "7ed3e848d70e4f2f98ce0c8a3b2dc2fa",
  "workflow": "event-message",
  "state": "pending"
}
```

`Idempotency-Key` 是可选的。对同一工作流重复使用相同 key 会返回已有 run，而不会创建重复 run。Key 不能为空，不能包含控制字符，最长 200 个字符。

请求最大为 64 KiB。

### 读取工作流状态

```text
GET /webhook-runs/{run_id}
Authorization: Bearer <token>
```

响应包含当前工作流和步骤状态。未知工作流 ID 返回 `404`，无效输入返回 `400`，token 缺失或错误返回 `401`。

## Home Assistant 示例

```yaml
rest_command:
  momoi_event:
    url: "http://momoi-host:8787/webhooks/event-message"
    method: post
    content_type: "application/json"
    headers:
      Authorization: "Bearer replace-with-a-random-token"
    payload: >
      {
        "event_prompt": {{ event_prompt | tojson }}
      }
```

在自动化中使用 `event_prompt` 值调用 `rest_command.momoi_event`。使用 Home Assistant 可达的网络地址；真实部署中应把 token 保存在 Home Assistant secrets 中。

## 执行与恢复

- 步骤按顺序执行。
- `message` 步骤会等待其 Channel 消息投递完成，然后再执行下一步。
- `exec` 步骤只在退出码为 `0` 时成功。
- 非零退出或启动失败会将步骤标记为失败。
- 超时的结果不确定，因为进程可能在停止前已经造成外部影响。
- 正常重启后不会重复执行已完成的步骤。

尽可能使用范围狭且幂等的命令。对结果不确定的命令事件进行人工重试前，先检查工作流状态。
