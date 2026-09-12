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
The main config contains `"providers": "providers.yaml"`. Dashboard mode watches service changes and reloads the business runtime.

When upgrading an existing workspace, remove the obsolete `bindings.asr` entry
from `providers.yaml`, including disabled bindings, and remove its service and
credentials if no other service uses them. Incoming voice now uses the channel's
own transcription. Back up the file before editing; unknown bindings prevent startup.

## Runtime controls API

`GET /api/settings/mcp` returns the original JSON file specified by
`tools.mcp_config`, falling back to workspace `mcp.json`. Disabled entries and
environment references are preserved. Without a workspace MCP file it returns
`{"mcpServers": {}}`; a missing custom path returns 404.
`PATCH /api/settings/mcp` replaces the file with the request JSON, validates it,
and requests a business runtime restart. It returns 202 with the configuration
snapshot; this confirms saving, not successful startup. Both methods require
dashboard authentication.

Authenticated `GET /api/settings/configuration` returns current editable values
in `app` and control metadata in `app_fields`. Each section has `label` and
`fields`; each field specifies its type, default and `advanced: false`.
`logging.level.enum` lists allowed choices. `reflection.at` specifies
`format: time` and a pattern for `HH:MM`, using the application's timezone.

Use `PATCH /api/settings/configuration/app` with the snapshot's revision:

```json
{
  "revision": "<revision from GET /api/settings/configuration>",
  "document": {
    "heartbeat": {"enabled": true},
    "logging": {"level": "INFO"},
    "reflection": {"enabled": true, "at": "03:00"},
    "episode_annealing": {"enabled": true}
  }
}
```

Submit any subset of these fields. Patching these runtime sections preserves omitted
fields, including heartbeat intervals, reflection time and annealing limits.
Booleans must be JSON booleans; log levels must be one of `TRACE`, `DEBUG`, `INFO`,
`WARNING`, `ERROR`, `CRITICAL` (uppercase). Times must be `00:00`–`23:59`.
Invalid requests return 400 without writing; stale revisions return 409.

Stage thinking overrides belong to `config.json` under `thinking.stages`, separate
from model providers. Render dropdowns in Runtime Settings using
`app_fields.thinking.fields.stages.properties`; each stage declares its label,
default, `advanced: false`, and enum `["", "low", "medium", "high", "xhigh", "max"]`.
The empty string means “use model default”. Submit through the same PATCH endpoint:

```json
{
  "revision": "<revision from GET /api/settings/configuration>",
  "document": {
    "thinking": {
      "stages": {"episode_anneal": "low", "reply_followup": "low"}
    }
  }
}
```

Partial stage updates retain omitted stages; send `""` to clear an override.
An empty object does not clear existing overrides. Unknown stages, invalid efforts
and null values return 400. Switching models preserves the runtime stage policy.
The runtime resolves the request's effort; each provider handles its wire format.
Move existing `thinking.stages` from provider options into `config.json` before
applying the configuration: providers now accept only the default `thinking.effort`.

Saving automatically reloads the business runtime while keeping the dashboard and
container running. Poll `GET /api/settings/runtime` until `applied_revision`
matches the saved revision and `state` is `running` (or `setup` when setup is
incomplete). Direct file edits are detected within the one-second watcher interval.
No manual container restart is required in dashboard mode.

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
Dashboard mode applies configuration changes automatically. The initialized provider is available internally through
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
Goal voice messages retain the existing notification scheduling.

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

NapCat converts incoming QQ voice messages through `fetch_ptt_text` using the
message ID. This requires a NapCat version providing that action (verified with
4.18.19; upstream [implementation](https://github.com/NapNeko/NapCatQQ/pull/1837)).
QQ may generate the text asynchronously, so rejected requests or empty results
are retried twice after 2 and 4 seconds, within `send_timeout_seconds` overall.
Failures leave `[QQ 语音消息暂时无法转写]` in place of the recording.
Weixin uses the transcription included by its channel. Neither requires a
separate recognition provider or credentials. The voice synthesis switch only
controls outgoing voice messages.

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
| `enabled` | Yes | — | Channel names and settings; may be empty during dashboard setup; otherwise must contain `primary` |

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
default 32–80 Turn transcript watermark and the complete-request token watermark are
independent safeguards. Episode raw evidence uses a budget derived from the same
compaction watermark; it has no separate token setting.
The same window covers conversation, Webhook events, Goal reviews and Heartbeat
records, including completed Turns with records but no outgoing messages. Their
ID indexes are derived from the retained transcript and have no separate history
limit. Goal and Webhook Turns do not run automatic pre-retrieval.
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
    "exec_enabled": false,
    "result_max_chars": 12000,
    "result_retention_days": 30
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `mcp_config` | `mcp.json` | MCP server configuration path; `null` or `""` disables MCP loading |
| `exec_enabled` | `false` | Expose the `exec` command tool to the model; editable in Settings → MCP tools |
| `result_max_chars` | `12000` | Maximum model-visible tool-result chunk size; minimum `1000` characters |
| `result_retention_days` | `30` | Days to retain private large-result snapshots; `0` disables age-based cleanup |

`exec` runs a Bash `command`, with optional `cwd` and `timeout_seconds` (30 seconds by default, at most 120).
The workspace is the default working directory, not a sandbox: commands have the Momoi process's file,
credential and network permissions. Each output stream retains its last 16 KiB; timeout or cancellation
terminates the process group. Disabling the switch removes the model tool and rejects direct invocation.
Save `tools.exec_enabled` through `PATCH /api/settings/configuration/app` with the current revision;
the supervisor applies the new runtime. Preconfigured Webhook `uses: exec` steps remain argv-based,
independent of this model-tool switch, with no additional Bash interpretation.

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
    "max_total_tokens": 0,
    "max_protocol_retries": 3
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `max_seconds` | `0` | Per-Turn wall-time limit; `0` disables it |
| `max_total_tokens` | `0` | Accumulated raw input/output token limit; `0` disables it |
| `max_protocol_retries` | `3` | Unified retries for protocol/tool errors in one Turn; after the limit the circuit opens and the workflow reports the failure through the channel |

`max_seconds` and `max_total_tokens` must be non-negative; `max_protocol_retries` must be a positive integer.

## Notifications

```json
{
  "notifications": {
    "quiet_start": null,
    "quiet_end": null
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `quiet_start` | unset | Quiet-window start in local `HH:MM` |
| `quiet_end` | unset | Quiet-window end in local `HH:MM` |

`quiet_start` and `quiet_end` must be distinct and either both set or both
omitted. Overnight windows are supported.

Heartbeat contact and queued system notifications observe quiet hours; urgent
system notifications bypass them. Goal `send_bubbles` / `send_voice` calls use
the normal delivery path immediately. Heartbeat owner-busy checks and interruption
by new messages remain in place.

## Heartbeat

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

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Enable automatic heartbeat evaluations |
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

## Current-state maintenance

```json
{
  "current_state": {
    "max_seconds": 180
  }
}
```

| Field | Default | Description |
| --- | --- | --- |
| `max_seconds` | `180` | Positive model-time limit for one current-state maintenance batch |

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

See [Provider configuration](./PROVIDERS.md#account-balance-and-token-accounting) for options and setup. The balance provider supplies optional usage parsing and cost estimation. Disabling it preserves generic token recording.

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
environment references. Dashboard mode watches `providers.yaml`, `config.json` and
the configured MCP file. MCP changes replace the complete business runtime,
including all MCP connections and tool schemas; the dashboard process stays alive.
Invalid files leave the current runtime intact. Failed startup attempts restore
the previous validated configuration, including its MCP snapshot, when available.
Use Settings → Reapply to retry an unchanged configuration after fixing an external
dependency, or after editing workflows or executors.
Storage paths, timezone, dashboard authentication and prompt file paths require a
process restart. Headless mode requires a restart for configuration changes. Prompt
content is reloaded before each new Turn.
