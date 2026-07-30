# Configuration and capability access

EN | [中文](./CONFIG.zh-CN.md)

Momoi reads configuration from a workspace. The default workspace is `~/.momoi`; pass `--workspace` before any command to select another one.

```bash
momoi run
momoi --workspace /path/to/workspace run
```

Create a workspace from the generic template before the first run:

```bash
mkdir -p ~/.momoi
cp -R config.example/. ~/.momoi/
```

The complete template is [config.example/config.json](../config.example/config.json).

## Choose an integration path

| Need | Use |
| --- | --- |
| Let the model discover and call an external capability | MCP server in `mcp.json` |
| Let Home Assistant, Jellyfin, or another service push an event | Webhook Workflow |
| Repeat work that needs fresh reasoning or tool calls | Goal |
| Deliver fixed text at a known time | Reminder |
| Fetch a URL during an owner task | Built-in HTTP tool |

MCP is the normal way to add model-controlled capabilities. Workflows are for event-driven, predefined sequences. See [WORKFLOW.md](./WORKFLOW.md) for webhook setup and YAML reference.

## Paths and workspace files

Relative paths in `config.json` are resolved from the directory containing that file.

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
│   └── weixin/       # created only when the Weixin channel is used
└── data/
```

This makes the workspace relocatable. Absolute paths are also accepted where a path field is supported.

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

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `api_format` | No | `anthropic` | `anthropic` or `openai` |
| `base_url` | Yes | — | Compatible API base URL |
| `api_key` | Yes | — | API credential; must not be empty |
| `model` | Yes | — | Model identifier sent to the provider |
| `max_tokens` | No | `2048` | Maximum output tokens for one model call |
| `temperature` | No | `0.6` | Sampling temperature |
| `timeout_seconds` | No | `120` | Positive request timeout |
| `max_retries` | No | `2` | Retries for transient connection and server errors |

For Anthropic-compatible providers, Momoi calls `/v1/messages`. For OpenAI-compatible providers, a host-only URL receives `/v1/chat/completions`; a URL that already contains a gateway path receives `/chat/completions` below that path.

`config.json` does not expand environment variables. Keep it private and restrict its file permissions if it contains credentials.

## Channel

Momoi runs one Channel plugin at a time. `napcat` is the example default; `weixin` is the native Tencent iLink alternative. `plugin` identifies the adapter and its protocol-specific values stay under `settings`.

```json
{
  "channel": {
    "plugin": "napcat",
    "settings": {
      "url": "ws://127.0.0.1:3001",
      "owner_qq": "100000000",
      "quiet_seconds": 6,
      "max_batch_seconds": 60,
      "heartbeat_seconds": 30,
      "reconnect_max_seconds": 30,
      "send_timeout_seconds": 20
    }
  }
}
```

`napcat` names this third-party adapter. A future official QQ AI Bot adapter will use a distinct `qq` plugin name.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `url` | Yes | — | NapCat WebSocket URL |
| `owner_qq` | Yes | — | Digits-only QQ ID accepted as the owner |
| `quiet_seconds` | No | `6` | Wait after the latest owner message before starting; a new message resets the wait |
| `max_batch_seconds` | No | `60` | Maximum time a continuously growing message batch may wait |
| `heartbeat_seconds` | No | `30` | NapCat connection heartbeat interval |
| `reconnect_max_seconds` | No | `30` | Maximum reconnect backoff |
| `send_timeout_seconds` | No | `20` | Timeout for one outbound NapCat request |

All timing fields in `channel.settings` must be positive.

Six seconds lets a natural sequence of short messages be handled together. Lower it to one second only when faster development feedback matters more than message collection.

### Weixin (Tencent iLink)

Replace the complete `channel` object with the following; do not configure it alongside NapCat:

```json
{
  "channel": {
    "plugin": "weixin",
    "settings": {
      "quiet_seconds": 6,
      "max_batch_seconds": 60,
      "reconnect_max_seconds": 30,
      "send_timeout_seconds": 20,
      "media_max_bytes": 104857600
    }
  }
}
```

Authenticate once in the same workspace, then run the daemon:

```bash
momoi --workspace ~/.momoi channel login
momoi --workspace ~/.momoi run
```

The login command prints a QR code in the terminal. Credentials, the update cursor, and the latest conversation context token are stored atomically with mode `0600` in `channel/weixin/state.json`. Decrypted inbound attachments are stored in `channel/weixin/media/inbound/`; protect and back up these files as part of the workspace.

Weixin receives text, quotes, images, video, files, and voice. Images are supplied to vision-capable models; other media are represented by local attachment descriptions. Server voice transcription is preferred, with raw SILK retained when no transcript is available. It sends text, images, video, and files; outbound audio is sent as a file attachment. `media_max_bytes` is a positive byte limit for inbound downloads and outbound local, HTTP(S), or `base64://` sources.

This implementation follows Tencent's MIT-licensed [`@tencent-weixin/openclaw-weixin` 2.4.6](https://github.com/Tencent/openclaw-weixin) protocol behavior. Use of Weixin and iLink remains subject to the applicable Tencent and Weixin service terms. Momoi supports one linked account and its single scanning owner, not groups or multiple simultaneous accounts.

### Adding a Channel

A Channel plugin is one module or package under `momoi.channel`. It exports `load_config(value, workspace)` and `create_channel(config)`. The created Channel supplies its `name`, runtime prompt context, batch timing, `run`, `send_message`, `content_blocks`, and Workflow variables. Incoming events identify their source with the plugin name, so NapCat events use `napcat:` and Weixin events use `weixin:`; `qq:` remains available for an official QQ AI Bot plugin.

Protocol-specific parsing, content rendering, and connection logs belong in that plugin module. The daemon, store, and webhook layers use only the common Channel interface.

## Context and memory budgets

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

| Field | Default | Description |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | Persona file, relative to the workspace |
| `recent_raw_tokens` | `32000` | Budget for recent conversation in original form |
| `recent_turns` | `6` | Minimum recent owner interactions retained even when trimming history |
| `memory_results` | `6` | Maximum durable memories recalled automatically |
| `memory_tokens` | `8000` | Token budget for recalled durable memory |
| `max_input_tokens` | `96000` | Target ceiling for the complete model input, including tool schemas |
| `summary_results` | `3` | Maximum older conversation segments recalled automatically |
| `summary_tokens` | `6000` | Token budget for recalled conversation segments |

Set `max_input_tokens` below the provider's real context window. These are context-building budgets, not a promise that every provider counts tokens identically.

Set a recall result count or token budget to `0` to disable that automatic recall layer. Explicit memory and conversation search tools remain available to the agent when their tool is enabled.

## Storage

```json
{
  "storage": {
    "database": "data/momoi.sqlite3"
  }
}
```

`database` is required. A relative path is resolved from the workspace, and its parent directory is created automatically.

Back up the complete workspace to preserve conversation history, memory, goals, reminders, emotion assets, and pending delivery state.

## MCP and tool results

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
| `mcp_config` | `mcp.json` | Standard MCP server configuration; use `null` or an empty string to disable MCP loading |
| `result_max_chars` | `30000` | Maximum normalized tool-result size returned to the model; minimum `1000` |

### Configure a stdio MCP server

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

#### Connect gog Gmail and Calendar

Use the built-in read-only `gog` MCP server directly:

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

Run `gog --account you@gmail.com mcp --allow-tool gmail,calendar --list-tools` to inspect the exposed tools first. This configuration permits Gmail and Calendar reads only; widen it explicitly if writes are later required.

### Configure a remote MCP server

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

MCP environment values, remote URLs, and headers support `${VARIABLE}` expansion from Momoi's process environment. The variable must exist when Momoi starts. Add `"disabled": true` to keep a server definition without connecting it.

Each connected server is isolated by name. Its tools appear to the model with a `mcp__<server>__<tool>` prefix. Connection failures are logged without preventing other configured servers from starting.

Some MCP servers omit the standard `readOnlyHint`. Add `readOnlyTools` with the server's original tool names when a tool is known to be read-only. This declaration does not expose the tool by itself; self-directed work must also allow its prefixed name through `autonomy.allowed_tools`.

## Turn budgets

```json
{
  "turn": {
    "max_seconds": 1800,
    "max_total_tokens": 0
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `max_seconds` | `0` | Maximum wall time for one agent task; `0` disables the time limit |
| `max_total_tokens` | `0` | Maximum raw input and output tokens accumulated across model calls, including repeated or cached input; `0` disables the token limit |

These are safety budgets, not limits on the number of tool calls.

## Proactive notification policy

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
| `timezone` | `UTC` | Valid IANA timezone used by schedules and quiet hours |
| `quiet_start` | unset | Local `HH:MM` start of the quiet window |
| `quiet_end` | unset | Local `HH:MM` end of the quiet window |
| `cooldown_seconds` | `1800` | Minimum delay between queued notifications with the same key |
| `pending_owner_delay_seconds` | `30` | Delay proactive delivery while an owner message is waiting |

`quiet_start` and `quiet_end` must either both be omitted or both use distinct `HH:MM` values. Overnight windows are supported.

This policy applies to proactive Goal and Heartbeat notifications. A fixed Reminder follows its requested schedule.

## Autonomous heartbeat

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

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Enable autonomous heartbeat evaluations |
| `initial_delay_seconds` | `900` | Delay before the first heartbeat in a new workspace |
| `min_interval_seconds` | `1800` | Smallest next interval Momoi may select |
| `max_interval_seconds` | `5400` | Largest next interval Momoi may select (90 minutes by default) |
| `reply_initial_interval_seconds` | `60` | First heartbeat delay while actively waiting for an owner reply |

Intervals must be positive, and the maximum must not be smaller than the ordinary minimum.

When a non-empty `HEARTBEAT.md` exists beside the configuration file, Momoi automatically appends it as workspace heartbeat guidance. No additional prompt is injected when the file is absent.

A heartbeat may use explicitly allowed read-only tools, search memory, create files under `<workspace>/artifacts`, or create an agent-owned Goal for work that must continue. It records the real result before deciding whether contacting the owner is useful. Owner Goals and reminders remain separate and are never performed or imitated by a heartbeat.

Send `/heartbeat` in the private owner chat to trigger one evaluation immediately, even when automatic heartbeat scheduling is disabled. A command received while another heartbeat is queued or running is deduplicated.

When a delivered reply explicitly expects an owner response, Momoi raises its attention after `reply_initial_interval_seconds`, even if automatic heartbeats are disabled. It performs three short checks at roughly 1, 3, and 7 minutes; the model independently decides whether to follow up and whether to keep waiting. After the third check, any continued waiting uses the ordinary heartbeat rhythm. Any owner message ends the old waiting state and cancels an unsent follow-up.

## Self-directed tool allowlist

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

The default is `curl`, `read_file`, and `write_file`. `curl` is limited to GET, HEAD, and OPTIONS. Autonomous file access is restricted to `<workspace>/artifacts`. MCP tools must be both listed here and classified read-only through `readOnlyHint` or `readOnlyTools`. Agent-owned Goals inherit the same boundary; owner-created Goals retain the tools authorized by the owner's task.

## Daily reflection

```json
{
  "reflection": {
    "enabled": true,
    "at": "03:00"
  }
}
```

Reflection uses `notifications.timezone` and reviews the local calendar day that just ended at `03:00`. `at` accepts `HH:MM` and defaults to `03:00`; the feature is disabled when omitted, while the example workspace enables it.

Reflection never contacts the owner or receives external tools. The complete summary and candidate learning are stored in SQLite `reflections`; promoted durable learning is stored in `reflection_memories` and enters later context below confirmed owner memory. Owner profile and preference items are accepted only when they quote owner text from that day.

## Webhooks

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

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Start the webhook API and workflow worker |
| `host` | `127.0.0.1` | Bind address; use a reachable interface only when another machine must connect |
| `port` | `8787` | TCP port from `1` to `65535` |
| `token` | empty | Bearer token; required when webhooks are enabled |
| `workflows` | `workflows` | Directory containing workflow YAML files |
| `executors` | `workflow-executors.yaml` | File containing predefined command executors |

Use a long random token and place a TLS reverse proxy in front of Momoi when the endpoint crosses an untrusted network. Continue with [WORKFLOW.md](./WORKFLOW.md).

## Logging

```json
{
  "logging": {
    "level": "INFO"
  }
}
```

Use `INFO` for normal operation and `DEBUG` for development. DEBUG logs may contain owner messages, model output, and tool status; treat them as private data.

## Apply changes

Restart `momoi run` after changing `config.json`, `mcp.json`, `SOUL.md`, workflow files, or executor definitions. Startup validates required configuration and reports a concise configuration error before connecting services.
