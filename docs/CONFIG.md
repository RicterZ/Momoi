# Configuration reference

EN | [中文](./CONFIG.zh-CN.md)

Momoi reads `config.json` from its workspace. The default workspace is
`~/.momoi`; pass `--workspace` before a command to select another directory.
The complete starter file is
[config.example/config.json](../config.example/config.json).

Relative paths are resolved from the directory containing `config.json`.
Absolute paths are accepted for every path field. `config.json` does not expand
`${VAR}` placeholders.

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
        "reply_followup": "low"
      }
    }
  }
}
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `api_format` | No | `anthropic` | Request format: `anthropic` or `openai` |
| `base_url` | Yes | — | Compatible API base URL |
| `api_key` | Yes | — | Non-empty API credential |
| `model` | Yes | — | Provider model identifier |
| `max_tokens` | No | `16384` | Maximum output tokens per model call |
| `temperature` | No | `0.6` | Sampling temperature |
| `timeout_seconds` | No | `300` | Positive request timeout |
| `max_retries` | No | `3` | Retry count for transient errors |
| `tool_choice` | No | `true` | Require tool use in OpenAI-format requests; set `false` for endpoints that reject `tool_choice` |
| `thinking.effort` | No | provider default | Default reasoning effort: `low`, `high`, or `max` |
| `thinking.stages` | No | `{}` | Reasoning-effort overrides keyed by runtime stage |

Known stage names are `context_plan`, `owner`, `heartbeat_plan`, `heartbeat`,
`reply_followup`, `goal`, `webhook`, `reflection`, `episode_anneal`, and
`episode_consolidate`. Unknown stages use `thinking.effort`.

## Inbound speech recognition

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

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Enable ASR for inbound NapCat voice messages |
| `provider` | `tencent` | Built-in `tencent` or the dotted name of an `ASRProvider` subclass |
| `timeout_seconds` | `30` | Positive timeout for one transcription |
| `max_audio_bytes` | `3145728` | Positive maximum input size in bytes |
| `settings` | `{}` | Provider constructor arguments |

Tencent ASR requires non-empty `settings.secret_id` and
`settings.secret_key` when enabled. `settings.region` may be empty, and
`settings.engine` defaults to `16k_zh`.

## Channels

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

`primary` is required and must name an entry in `enabled`. Every entry in
`enabled` is loaded as a Channel plugin.

### NapCat

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `url` | Yes | — | NapCat WebSocket URL |
| `owner_qq` | Yes | — | Digits-only owner QQ ID |
| `quiet_seconds` | No | `1` | Wait after the latest owner message |
| `max_batch_seconds` | No | `60` | Maximum message-batch wait |
| `heartbeat_seconds` | No | `30` | Connection heartbeat interval |
| `reconnect_max_seconds` | No | `30` | Maximum reconnect backoff |
| `send_timeout_seconds` | No | `20` | Outbound request timeout |
| `media_max_bytes` | No | `20971520` | Maximum downloaded inbound image size |
| `media_download_timeout_seconds` | No | `15` | Inbound image download timeout |

The starter file sets `quiet_seconds` to `6`; omitting it uses the runtime
default of one second. Timing and size fields must be positive.

### Weixin

| Field | Default | Description |
| --- | --- | --- |
| `quiet_seconds` | `6` | Wait after the latest owner message |
| `max_batch_seconds` | `60` | Maximum message-batch wait |
| `reconnect_max_seconds` | `30` | Maximum update retry delay |
| `send_timeout_seconds` | `20` | Outbound request timeout |
| `media_max_bytes` | `104857600` | Maximum inbound or outbound media size |

All fields must be positive. Weixin credentials and state are stored under
`channel/weixin/` in the workspace.

## Context

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

| Field | Default | Description |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | Required, non-empty persona file |
| `heartbeat_prompt` | `prompts/HEARTBEAT.md` | Optional heartbeat guidance file |
| `recent_raw_tokens` | `32000` | Recent Turn token budget; minimum `1` |
| `recent_turns` | `6` | Recent Turn count; minimum `1` |
| `planner_recent_base_turns` | `recent_turns` | Planner stable-base Turn count; minimum `1` |
| `planner_recent_append_turns` | `recent_turns` | Planner append Turn count; minimum `1` |
| `planner_active_recent_turns` | `recent_turns` | Planner focus Turn count; minimum `1` |
| `planner_recent_tokens` | automatic | Planner recent-log budget; minimum `1000` |
| `memory_results` | `6` | Per-category top-k for confirmed recall memory and reflection memory; range `0`–`6`, and `0` disables both (combined maximum `12`) |
| `memory_tokens` | `8000` | Durable-memory context budget; minimum `0` |
| `max_input_tokens` | `96000` | Target ceiling for complete model input; minimum `1000` |
| `summary_results` | `8` | Maximum query-recalled Episodes, configurable up to `12`; `0` disables query recall |
| `summary_tokens` | `6000` | Merged Episode-summary token budget; `0` disables this layer |
| `recent_episode_hours` | `6` | Recent-Episode window in hours; `0` disables it |

When omitted, `planner_recent_tokens` is the smaller of 55% of
`max_input_tokens` and `88000`. Set `max_input_tokens` below the provider's
actual context window.

## Storage

```json
{
  "storage": {
    "database": "data/momoi.sqlite3",
    "thinking": null
  }
}
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `database` | Yes | — | SQLite database path; its parent directory is created automatically |
| `thinking` | No | database directory | Directory for monthly `thinking-YYYY-MM.sqlite3` files |

Set `thinking` to `null` or an empty string to use the database directory.

## Tools and MCP

```json
{
  "tools": {
    "mcp_config": "mcp.json",
    "result_max_chars": 30000
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `mcp_config` | `mcp.json` | MCP server configuration path; `null` or `""` disables MCP loading |
| `result_max_chars` | `30000` | Maximum normalized tool-result size; minimum `1000` characters |

An MCP server entry in `mcp.json` uses either `command` plus optional `args`,
`cwd`, and `env`, or a remote `url` plus optional `headers`. The optional fields
below control discovery and routing.

| MCP field | Default | Description |
| --- | --- | --- |
| `description` | generated from server id | Optional capability summary, 1–500 characters when set |
| `enabled_tools` | `["*"]` | Raw or fully qualified tool names to register; `[]` registers none |
| `readOnlyTools` | `[]` | Raw names of tools that should be treated as read-only |
| `disabled` | `false` | Keep the definition without connecting |

Unlike `config.json`, MCP environment values, URLs, and headers expand
`${VARIABLE}` from the Momoi process environment.

## Turn budgets

```json
{
  "turn": {
    "max_seconds": 0,
    "max_total_tokens": 0
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `max_seconds` | `0` | Per-Turn wall-time limit; `0` disables it |
| `max_total_tokens` | `0` | Accumulated raw input/output token limit; `0` disables it |

Both values must be non-negative. The starter file omits this section so the
limits remain disabled.

## Notifications

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

| Field | Default | Description |
| --- | --- | --- |
| `timezone` | `UTC` | IANA timezone for schedules and quiet hours |
| `quiet_start` | unset | Quiet-window start in local `HH:MM` |
| `quiet_end` | unset | Quiet-window end in local `HH:MM` |
| `cooldown_seconds` | `1800` | Non-negative interval between proactive contacts with the same key |
| `pending_owner_delay_seconds` | `30` | Non-negative delivery delay while an owner message is pending |

`quiet_start` and `quiet_end` must be distinct and either both set or both
omitted. Overnight windows are supported.

## Heartbeat

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

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Enable automatic heartbeat evaluations |
| `initial_delay_seconds` | `900` | Positive delay before the first heartbeat |
| `min_interval_seconds` | `1800` | Positive minimum interval |
| `max_interval_seconds` | `5400` | Positive maximum interval |

`max_interval_seconds` must be at least `min_interval_seconds`.

## Autonomous tools

```json
{
  "autonomy": {
    "allowed_tools": ["curl", "read_file", "write_file", "list_dir"]
  }
}
```

`allowed_tools` is an array of non-empty strings. The default is `curl`,
`read_file`, `write_file`, and `list_dir`. MCP tools must use their full
`mcp__<server>__<tool>` names.

## Reflection

```json
{
  "reflection": {
    "enabled": true,
    "at": "03:00"
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Enable daily reflection |
| `at` | `03:00` | Local run time in `HH:MM`, using `notifications.timezone` |

## Episode maintenance

```json
{
  "episode_annealing": {
    "enabled": true,
    "idle_seconds": 60,
    "max_seconds": 650
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Enable background Episode maintenance |
| `idle_seconds` | `60` | Non-negative required owner-idle time |
| `max_seconds` | `650` | Positive model-time limit for one batch |

## Webhooks

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

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Start the webhook API and workflow worker |
| `host` | `127.0.0.1` | Bind address |
| `port` | `8787` | TCP port from `1` to `65535` |
| `token` | empty | Bearer token; required when enabled |
| `workflows` | `workflows` | Workflow YAML directory |
| `executors` | `workflows/workflow-executors.yaml` | Command-executor definition file |

See [WORKFLOW.md](./WORKFLOW.md) for the workflow YAML reference.

## Dashboard

```json
{
  "dashboard": {
    "token": "replace-with-a-long-random-secret"
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `token` | empty | Access passphrase required when the dashboard is enabled from the CLI |

Dashboard bind address and port are CLI options, not `config.json` fields.

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

| Field | Default | Description |
| --- | --- | --- |
| `provider` | empty | Dotted name of a `UsagePlugin` class |
| `api_key` | empty | Plugin constructor's `api_key` argument |
| other fields | — | Additional plugin constructor keyword arguments |

The bundled DeepSeek plugin accepts optional `base_url` and
`timeout_seconds` fields. Leave `provider` empty to record token counts without
provider pricing or balance lookup.

## Logging

```json
{
  "logging": {
    "level": "INFO"
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `level` | `DEBUG` | `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

`TRACE` writes complete LLM requests and raw responses under `llm-dumps/`.
Debug and trace output may contain private conversation and tool data.

## Environment overrides

Environment values override `config.json` for the current process.

| Variable | Configuration field |
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

Keep files containing credentials private. Restart `momoi run` after changing
`config.json`, `mcp.json`, workflows, or executor definitions. Prompt files are
reloaded before each new Turn.
