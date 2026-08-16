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
├── prompts/
│   ├── SOUL.md
│   └── HEARTBEAT.md
├── workflows/
│   ├── *.yaml
│   └── workflow-executors.yaml
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
    "max_tokens": 16384,
    "temperature": 0.6,
    "timeout_seconds": 300,
    "max_retries": 3,
    "tool_choice": true
  }
}
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `api_format` | No | `anthropic` | `anthropic` or `openai` |
| `base_url` | Yes | — | Compatible API base URL |
| `api_key` | Yes | — | API credential; must not be empty |
| `model` | Yes | — | Model identifier sent to the provider |
| `max_tokens` | No | `16384` | Maximum output tokens for one model call |
| `temperature` | No | `0.6` | Sampling temperature |
| `timeout_seconds` | No | `300` | Positive request timeout |
| `max_retries` | No | `3` | Retries for transient connection and server errors; OpenAI-compatible endpoints also retry unusable successful responses |
| `tool_choice` | No | `true` | Require tool use in OpenAI-compatible requests; set to `false` for models such as Thinking mode that reject `tool_choice` |

For Anthropic-compatible providers, Momoi calls `/v1/messages`. For OpenAI-compatible providers, a host-only URL receives `/v1/chat/completions`; a URL that already contains a gateway path receives `/chat/completions` below that path.

`config.json` does not expand environment variables. Keep it private and restrict its file permissions if it contains credentials.

## Channel

Momoi can run multiple Channel plugins at once. They share one conversation, memory, goals, mood, and identity. Replies stay on the channel where the owner spoke; new proactive messages use `primary`.

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

`primary` must name an entry in `enabled`. A disconnected channel keeps its own messages queued without blocking the others, and Momoi never silently reroutes a message across platforms. The legacy single `channel.plugin/settings` form remains supported as one primary channel.

`napcat` names this third-party adapter. A future official QQ AI Bot adapter will use a distinct `qq` plugin name.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `url` | Yes | — | NapCat WebSocket URL |
| `owner_qq` | Yes | — | Digits-only QQ ID accepted as the owner |
| `quiet_seconds` | No | `1` | Wait after the latest owner message before starting; a new message resets the wait |
| `max_batch_seconds` | No | `60` | Maximum time a continuously growing message batch may wait |
| `heartbeat_seconds` | No | `30` | NapCat connection heartbeat interval |
| `reconnect_max_seconds` | No | `30` | Maximum reconnect backoff |
| `send_timeout_seconds` | No | `20` | Timeout for one outbound NapCat request |
| `media_max_bytes` | No | `20971520` | Maximum size of one remote inbound image materialized for model input |
| `media_download_timeout_seconds` | No | `15` | Timeout for downloading one remote inbound image |

All channel timing fields must be positive.

The starter template uses six seconds so a natural sequence of short messages can be handled together. Omitting the field uses the one-second runtime default.

NapCat owner input-status notices refresh the same quiet window, including while an Owner Turn is already running. At model and tool boundaries, Momoi waits for the owner to remain quiet and folds any newly arrived message into that Turn, still bounded by `max_batch_seconds`. Input status is transient activity: it is never stored as a conversation message or sent to the model.

### Weixin (Tencent iLink)

Authenticate once in the same workspace, then run the daemon:

```bash
momoi --workspace ~/.momoi channel login weixin
momoi --workspace ~/.momoi run
```

The login command prints a QR code in the terminal. Credentials, the update cursor, and the latest conversation context token are stored atomically with mode `0600` in `channel/weixin/state.json`. Decrypted inbound attachments are stored in `channel/weixin/media/inbound/`; protect and back up these files as part of the workspace.

Weixin receives text, quotes, images, video, files, and voice. Images are supplied to vision-capable models; other media are represented by local attachment descriptions. Server voice transcription is preferred, with raw SILK retained when no transcript is available. It sends text, images, video, and files; outbound audio is sent as a file attachment. `media_max_bytes` is a positive byte limit for inbound downloads and outbound local, HTTP(S), or `base64://` sources.

| Field | Default | Description |
| --- | --- | --- |
| `quiet_seconds` | `6` | Wait after the latest owner message before starting |
| `max_batch_seconds` | `60` | Maximum time a continuously growing message batch may wait |
| `reconnect_max_seconds` | `30` | Maximum delay after repeated update failures |
| `send_timeout_seconds` | `20` | Timeout for one outbound request |
| `media_max_bytes` | `104857600` | Maximum size of one inbound or outbound media item |

This implementation follows Tencent's MIT-licensed [`@tencent-weixin/openclaw-weixin` 2.4.6](https://github.com/Tencent/openclaw-weixin) protocol behavior. Use of Weixin and iLink remains subject to the applicable Tencent and Weixin service terms. Momoi supports one linked account and its single scanning owner, not groups or multiple simultaneous accounts.

### Adding a Channel

A Channel plugin is one module or package under `momoi.channel`. It exports `load_config(value, workspace)` and `create_channel(config)`. The created Channel supplies its unique `name`, runtime prompt context, batch timing, `run`, `send_message`, `content_blocks`, and Workflow variables. Incoming events identify their source with the plugin name, so NapCat events use `napcat:` and Weixin events use `weixin:`; `qq:` remains available for an official QQ AI Bot plugin.

Protocol-specific parsing, content rendering, and connection logs belong in that plugin module. The daemon, store, and webhook layers use only the common Channel interface.

## Context and memory budgets

```json
{
  "context": {
    "soul_prompt": "prompts/SOUL.md",
    "heartbeat_prompt": "prompts/HEARTBEAT.md",
    "recent_raw_tokens": 32000,
    "recent_turns": 6,
    "memory_results": 6,
    "memory_tokens": 8000,
    "max_input_tokens": 96000,
    "summary_results": 12,
    "summary_tokens": 6000,
    "recent_episode_hours": 6
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | Persona file, relative to the workspace |
| `heartbeat_prompt` | `prompts/HEARTBEAT.md` | Workspace heartbeat guidance; omitted when the file is absent |
| `recent_raw_tokens` | `32000` | Budget for recent conversation in original form |
| `recent_turns` | `6` | Maximum recent completed conversation Turns kept in raw form |
| `memory_results` | `6` | Maximum durable memories recalled automatically |
| `memory_tokens` | `8000` | Token budget for recalled durable memory |
| `max_input_tokens` | `96000` | Target ceiling for the complete model input, including tool schemas |
| `summary_results` | `12` | Maximum Episodes returned by keyword recall before merging with recent Episodes |
| `summary_tokens` | `6000` | Token budget for recalled conversation segments |
| `recent_episode_hours` | `6` | Automatically include Episode summaries active within this many recent hours; set to `0` to disable |

Set `max_input_tokens` below the provider's real context window. These are context-building budgets, not a promise that every provider counts tokens identically.

`recent_raw_tokens` limits recent conversation kept in original form, and `recent_turns` limits how many completed conversation Turns are considered. `recent_episode_hours` adds every Episode active in the configured window, independent of keyword recall. `summary_results` limits keyword-recalled Episodes to 12 by default. The two sets are deduplicated, then ordered with recent keyword matches first, other keyword matches next, and recent-only Episodes last. More matched keyword alternatives rank ahead within the keyword groups. `summary_tokens` is shared by the merged Episode summaries.

Set a recall result count or token budget to `0` to disable that automatic recall layer. Explicit memory and conversation search tools remain available to the agent when their tool is enabled.

## Storage

```json
{
  "storage": {
    "database": "data/momoi.sqlite3",
    "thinking": null
  }
}
```

`database` is required. A relative path is resolved from the workspace, and its parent directory is created automatically.

`thinking` is the directory for monthly model-thinking files (`thinking-YYYY-MM.sqlite3`). Omit it or set `null` to use the same directory as `database`. A relative path is resolved from the workspace.

Back up the complete workspace to preserve conversation history, memory, goals, reminders, emotion assets, pending delivery state, and recorded thinking.

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

Built-in file tools resolve relative paths from the workspace. Absolute paths remain supported.

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
| `cooldown_seconds` | `1800` | Minimum interval between proactive contacts with the same key |
| `pending_owner_delay_seconds` | `30` | Delay durable proactive delivery while an owner message is waiting |

`quiet_start` and `quiet_end` must either both be omitted or both use distinct `HH:MM` values. Overnight windows are supported.

This policy applies to proactive Goal and Heartbeat contacts. Goal notifications are durable and move to the next eligible time. Heartbeat conversation is ephemeral: if it is not eligible for immediate delivery, it stays silent instead of retaining text for later replay. A fixed Reminder follows its requested schedule.

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

When a non-empty `prompts/HEARTBEAT.md` exists, Momoi automatically appends it as workspace heartbeat guidance. No additional prompt is injected when the file is absent. Change the path with `context.heartbeat_prompt`.

A heartbeat may use explicitly allowed read-only tools, search memory, create files under `<workspace>/artifacts`, or create an agent-owned Goal for work that must continue. It records the real result before deciding whether contacting the owner is useful. Owner Goals and reminders remain separate and are never performed or imitated by a heartbeat.

Send `/heartbeat` in the private owner chat to trigger one evaluation immediately, even when automatic heartbeat scheduling is disabled. A command received while another heartbeat is queued or running is deduplicated.

When a delivered reply explicitly expects an owner response, Momoi raises its attention after `reply_initial_interval_seconds`, even if automatic heartbeats are disabled. Reply attention has its own schedule and never replaces the ordinary `next_heartbeat_at` rhythm. It uses three short checks separated by roughly 1, 3, and 6 minutes; the model independently decides whether to follow up and whether to keep waiting. After the third check, any continued waiting uses the ordinary heartbeat rhythm. Any owner message ends the old waiting state, cancels its scheduled check, and cancels an unsent follow-up.

Owner Turns exclusively answer owner input. A heartbeat is deferred while owner events, an Owner Turn, or its outgoing reply are in flight. It records the owner-event revision it read and discards visible heartbeat output if the conversation changes before commit; internal heartbeat activity is still retained.

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

The default is `curl`, `read_file`, `write_file`, and `list_dir`. `curl` is limited to GET, HEAD, and OPTIONS. Autonomous file access is restricted to `<workspace>/artifacts`. MCP tools must be both listed here and classified read-only through `readOnlyHint` or `readOnlyTools`. Agent-owned Goals inherit the same boundary; owner-created Goals retain the tools authorized by the owner's task.

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

Reflection never contacts the owner or receives external tools. The complete summary and candidate learning are stored in SQLite `reflections`; promoted durable learning is stored in `reflection_memories` and enters later context below confirmed owner memory. Owner profile and preference items are accepted only when they quote owner text from that day. The same Turn reviews still-open conversation episodes and may close a thread that the day's record shows is finished or expired.

Send `/reflect` in the private owner chat to review the current local calendar day immediately, even when automatic reflection is disabled. A command received while that day's reflection is already running is ignored; a completed day can be reviewed again and overwrites that day's summary and promoted memories.

## Episode history maintenance

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
| `enabled` | `true` | Maintain older conversation Episodes in the background |
| `idle_seconds` | `60` | Required owner-idle time before maintenance starts |
| `max_seconds` | `650` | Maximum model time for one maintenance batch |

Maintenance is coalesced and processes one Episode batch at a time. A new owner message cancels active maintenance without counting it as a failure; the work becomes eligible again after the owner is idle.

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
| `host` | `127.0.0.1` | Bind address; use a reachable interface only when another machine must connect |
| `port` | `8787` | TCP port from `1` to `65535` |
| `token` | empty | Bearer token; required when webhooks are enabled |
| `workflows` | `workflows` | Directory containing workflow YAML files |
| `executors` | `workflows/workflow-executors.yaml` | File containing predefined command executors; may live inside the workflows directory and is skipped when loading workflows |

Use a long random token and place a TLS reverse proxy in front of Momoi when the endpoint crosses an untrusted network. Continue with [WORKFLOW.md](./WORKFLOW.md).

## Dashboard

The local Web dashboard is a small window into Momoi's records: conversations, daily reflections, memories, image reactions, goals, and recorded model thinking. From there you can also edit memories, manage reactions, and adjust goals that are still in progress. Thinking is read-only.

It starts only with `momoi run --dashboard`. Bind address and port come from the CLI; `config.json` holds the access passphrase.

```json
{
  "dashboard": {
    "token": "replace-with-a-long-random-secret"
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `token` | empty | Passphrase required whenever `--dashboard` is enabled. Enter it on the page to unlock the dashboard; the browser keeps access for about one year |

Open `http://127.0.0.1:8788` by default. Use `--dashboard-host` and `--dashboard-port` to change the listener. Keep the dashboard on localhost or a trusted network — do not expose the port directly to the public Internet.

## Usage

The dashboard records each LLM call locally. A Usage plugin supplies official token rates, and optionally a live account balance, so the page can estimate cost. Leave `provider` empty to keep request and token counts without pricing.

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
| `provider` | empty | Dotted class name of a `UsagePlugin` |
| `api_key` | empty | Passed to the plugin constructor as `api_key` |
| other keys | — | Forwarded as constructor keyword arguments |

The included DeepSeek plugin prices `deepseek-v4-flash` and `deepseek-v4-pro` in CNY per 1M tokens (cache hit, cache miss, output) and reads `/user/balance` when `api_key` is set. `base_url` and `timeout_seconds` are optional.

Restart `momoi run` after changing `usage`. The class must be importable from Momoi's Python environment.

### Writing a plugin

Subclass `momoi.extensions.UsagePlugin`. Implement `token_rates`. Override `balance` if the dashboard should show account funds. The default `estimate_cost` multiplies those rates by token counts; override it only when pricing is not linear.

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
        # CNY per 1M tokens: cache hit, cache miss, output
        return (self.input_cny, self.input_cny, self.output_cny)

    async def balance(self) -> dict[str, object]:
        return {
            "source": "unavailable",
            "currency": "CNY",
            "is_available": False,
            "total_balance": "0",
        }
```

Point `provider` at the dotted class name. Extra fields become constructor arguments:

```json
{
  "usage": {
    "provider": "my_package.usage.FlatRatePlugin",
    "input_cny": 2.0,
    "output_cny": 8.0
  }
}
```

`balance()` must include `source`, `currency`, `is_available`, and `total_balance`. Use `source` `"live"` when the value is current, or `"unavailable"` when it is not.

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
| `level` | `DEBUG` | Python logging level; supports `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL` |

Use `INFO` for normal operation and `DEBUG` for development. `TRACE` also writes complete LLM requests and raw responses under `llm-dumps/` in the workspace. DEBUG and TRACE logs may contain owner messages, model output, and tool status; treat them as private data.

## Apply changes

`SOUL.md` and `HEARTBEAT.md` are reloaded for each new Turn, so edits need no restart. Restart `momoi run` after changing `config.json`, `mcp.json`, workflow files, or executor definitions. Startup validates required configuration and reports a concise configuration error before connecting services.
