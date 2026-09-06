# Configuration reference

EN | [中文](./CONFIG.zh-CN.md)

Momoi reads `config.json` from its workspace. The default workspace is
`~/.momoi`; pass `--workspace` before a command to select another directory.
The complete starter file is
[config.example/config.json](../config.example/config.json).

Relative paths are resolved from the directory containing `config.json`.
Absolute paths are accepted for every path field. `config.json` does not expand
`${VAR}` placeholders.

External API endpoints, credentials and options live in [providers.yaml](./PROVIDERS.md).
The main config contains `"providers": "providers.yaml"`. Restart after service changes.

## Fish Audio speech synthesis

TTS is disabled by default and `send_voice` is hidden while disabled. When TTS
is enabled, both NapCat and Weixin requests include the same `send_voice` schema
to preserve the shared tool prefix for caching. The harness permits execution
only on channels supporting voice output (currently NapCat).
Prompts and automatic voice-versus-text reply rules are unchanged.

Merge these entries into your workspace `providers.yaml`:

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

Create a key on the [Fish API key page](https://fish.audio/app/api-keys) and
put it in `credentials.fish.api_key`, or use the example’s `FISH_API_KEY` environment reference.
Restart Momoi after updating
configuration. The initialized provider is available internally through
`daemon.bubble_delivery.tts_provider`; its `synthesize(text)` method returns the
in-memory `AudioOutput(data: bytes, format: str)`. No CLI entry point is added.

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Initialize the internal TTS provider |
| `timeout_seconds` | `60` | Finite positive timeout for the complete HTTP response |
| `max_audio_bytes` | `20971520` | Maximum downloaded audio size, including chunked responses |
| `credentials.fish.api_key` | — | Fish API credential; required when enabled |
| `services.speech.base_url` | `https://api.fish.audio` | API base URL; Momoi appends `/v1/tts` |
| `options.model` | `s2.1-pro-free` | One of `s2.1-pro-free`, `s2.1-pro`, `s2-pro`, `s1` |
| `options.reference_id` | — | Required Fish voice ID, taken from the voice page URL |
| `options.format` | `mp3` | `mp3`, `wav`, or `opus`; raw PCM is not supported |
| `options.latency` | `normal` | `normal` for quality, `balanced`, or `low` |

The example uses [this Fish voice](https://fish.audio/m/9bb8ad542dc44d148c21c73a0884e9ae/).
Voice availability and free-tier access depend on Fish. Momoi validates model
names because Fish documents a paid-model fallback for unknown names; it never
automatically changes the configured model. Failed synthesis requests receive
three retries after the initial attempt, with delays of 1, 2, and 4 seconds.
See [Fish TTS API](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)
and [pricing](https://docs.fish.audio/developer-guide/models-pricing/pricing-and-rate-limits).

Errors include the HTTP status and bounded response details, or connection exception,
host, port, and OS error when available. API keys, voice IDs, and the submitted text
are redacted from these details. Each failed attempt is logged.
Empty, non-audio, and oversized responses raise `TTSError`. No audio files are written.

The voice tool accepts only the complete `text` string and selects the
current channel internally. Conversation history and recall retain the original
text. The tool waits for synthesis before queueing or staging a notification;
on failure it returns a tool error recommending `send_bubbles` for a text reply.
Successful calls use the same result structure as `send_bubbles`.
The outbox persists only the text and a voice delivery marker. A bounded memory
cache passes the synthesized audio to the worker, which sends it to NapCat as base64. Momoi does
not persist audio files or audio bytes in SQLite; NapCat controls its own internal
temporary-file behavior. Pending messages are synthesized again after a restart
or cache eviction; failures during this recovery mark delivery failed.
On Weixin the harness rejects `send_voice` without changing the tool schema;
direct internal calls return `voice_not_supported`.
Owner, Heartbeat, Webhook, Goal and reply-followup workflows support voice output.
Goal voice messages retain the existing notification scheduling and cooldown.

Incoming voice transcriptions on NapCat and Weixin are prefixed with
`[语音消息] ` before storage and transcript assembly. Plain text is unchanged;
untranscribed voice placeholders are also marked.

## Timezone

```json
{"timezone": "Asia/Shanghai"}
```

`timezone` is the single IANA timezone used by all Momoi timestamps, local-day
boundaries, schedules, quiet hours, logs, and model context. It defaults to
`UTC`. Individual subsystems and Goals cannot override it.

## LLM

See [Provider configuration](./PROVIDERS.md#llm) for options and setup.

## Inbound speech recognition

See [Provider configuration](./PROVIDERS.md#asr) for options and setup.

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

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `primary` | Yes | — | Channel name selected for outbound delivery |
| `enabled` | Yes | — | Non-empty object of Channel names and their settings; must contain `primary` |

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

All fields must be positive.

## Context

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

| Field | Default | Description |
| --- | --- | --- |
| `soul_prompt` | `prompts/SOUL.md` | Required, non-empty persona file |
| `heartbeat_prompt` | `prompts/HEARTBEAT.md` | Optional heartbeat guidance file |
| `transcript_turns_min` | `32` | Recent completed Turns retained after the transcript window slides; minimum `1` |
| `transcript_turns_max` | `80` | High watermark at which the transcript window slides back to `transcript_turns_min`; cannot be lower than the minimum |
| `episode_raw_tail_turns` | `6` | Raw tail Turns retained outside the summary for an open Episode; its normal annealing threshold is twice this value; minimum `1` |
| `memory_results` | `6` | Per-category top-k for confirmed recall memory and reflection memory; range `0`–`6`, and `0` disables both (combined maximum `12`) |
| `max_input_tokens` | `142222` | Upper budget for the complete model input; minimum `1000` |
| `context_compaction_ratio` | `0.9` | Fraction of `max_input_tokens` at which old transcript and current-Turn tool results begin compacting; range `(0, 1]`. The defaults compact at 128,000 tokens. |
| `summary_results` | `8` | Maximum query-recalled Episodes, configurable up to `12`; `0` disables query recall |
| `summary_tokens` | `6000` | Merged Episode-summary token budget; `0` disables this layer |

`max_input_tokens` should remain below the provider's actual context window. The
48–96 Turn transcript watermark and the complete-request token watermark are
independent safeguards. Episode raw evidence uses a budget derived from the same
compaction watermark; it has no separate token setting.
Always-on and active recent memories are injected in full. Query recall is bounded
by `memory_results`, not by a separate memory token budget.

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

## Embedding recall

See [Provider configuration](./PROVIDERS.md#embedding) for options and setup. Configure `bindings.embedding`; changing model, dimensions or calibration requires a new semantic space.

## Tools and MCP

```json
{
  "tools": {
    "mcp_config": "mcp.json",
    "result_max_chars": 12000,
    "result_retention_days": 30
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `mcp_config` | `mcp.json` | MCP server configuration path; `null` or `""` disables MCP loading |
| `result_max_chars` | `12000` | Maximum model-visible tool-result chunk size; minimum `1000` characters |
| `result_retention_days` | `30` | Days to retain private large-result snapshots; `0` disables age-based cleanup |

| MCP field | Default | Description |
| --- | --- | --- |
| `command` | — | Executable for a stdio server; required when `url` is omitted |
| `args` | `[]` | Arguments passed to `command` |
| `cwd` | process directory | Working directory for `command` |
| `env` | `{}` | Environment values added for `command` |
| `url` | — | Streamable HTTP endpoint; required when `command` is omitted |
| `headers` | `{}` | Headers sent to `url` |
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

Both values must be non-negative.

## Notifications

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

| Field | Default | Description |
| --- | --- | --- |
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
| `at` | `03:00` | Local run time in `HH:MM`, using the top-level `timezone` |

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
| `idle_seconds` | `60` | Non-negative owner-idle time before a partial batch of fewer than 6 eligible Turns may run; a full 6-Turn batch does not wait for this timeout |
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

## Account balance and token accounting

See [Provider configuration](./PROVIDERS.md#account-balance-and-token-accounting) for options and setup. Balance queries are independent of local token recording; the DeepSeek LLM adapter supplies its usage parser and cost estimation.

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
| `MOMOI_NAPCAT_URL` | `channels.enabled.napcat.url` |
| `MOMOI_OWNER_QQ` | `channels.enabled.napcat.owner_qq` |
| `MOMOI_PRIMARY` | `channels.primary` |
| `MOMOI_TIMEZONE` | `timezone` |
| `MOMOI_DASHBOARD_TOKEN` | `dashboard.token` |
| `MOMOI_WEBHOOKS_ENABLED` | `webhooks.enabled` |
| `MOMOI_WEBHOOKS_HOST` | `webhooks.host` |
| `MOMOI_WEBHOOKS_TOKEN` | `webhooks.token` |

Keep files containing credentials private. Provider credentials use only explicit YAML
environment references. Restart `momoi run` after changing `providers.yaml`,
`config.json`, `mcp.json`, workflows, or executor definitions. Prompt
files are reloaded before each new Turn.
