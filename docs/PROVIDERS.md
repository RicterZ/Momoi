# External service providers

EN | [中文](./PROVIDERS.zh-CN.md)

Momoi configures external APIs in `providers.yaml`. The main `config.json` contains
`"providers": "providers.yaml"`; this path is resolved relative to `config.json`.
Restart Momoi after editing the catalog. The dashboard settings page edits prompt
files; provider configuration is managed in YAML.

Start with [the complete example](../config.example/providers.yaml). Enable only
the capabilities you need and supply their credentials.

## Connect services

A service identifies an adapter, endpoint, credentials, and shared defaults.
A binding selects a service for one capability and supplies its task options.
One service can serve multiple capabilities if its adapter supports them.

```yaml
version: 1
credentials:
  deepseek:
    api_key: {env: DEEPSEEK_API_KEY}
services:
  deepseek:
    adapter: deepseek
    base_url: https://api.deepseek.com
    credentials: deepseek
bindings:
  llm:
    service: deepseek
    options:
      model: deepseek-v4-flash
      max_tokens: 16384
      thinking:
        effort: high
        stages:
          reply_followup: low
  balance:
    service: deepseek
    options:
      timeout_seconds: 10
```

Export `DEEPSEEK_API_KEY` in the Momoi process environment. In Docker, pass it
through the container's `environment` or `env_file`. A credential value may also
be a literal string. Environment expansion only occurs in explicit `{env: NAME}`
credential references; strings containing `${NAME}` are not interpolated.

| Catalog field | Meaning |
| --- | --- |
| `version` | Required integer `1` |
| `plugins` | Optional list of installed Python modules imported before validation |
| `credentials` | Named maps of credential fields, such as `api_key` or Tencent's `secret_id` / `secret_key` |
| `services` | Named service definitions: `adapter`, optional `credentials`, `base_url`, `timeout_seconds`, `settings` |
| `bindings` | Capability map: `service`, optional boolean `enabled` (default `true`), `options` |

Options merge in this order: service `settings`, service `base_url` and
`timeout_seconds`, binding `options`, credential fields. Credential fields cannot
also appear in options. Unknown fields, duplicate YAML keys, unresolved references,
and unsupported adapter/capability pairs fail validation before startup. Enabled
bindings require their referenced environment credentials. Disabled bindings still
validate references but do not require environment secrets or create clients.
The LLM binding is required and enabled; omitted optional bindings are disabled.

## Built-in adapters

| Adapter | Capability | Options |
| --- | --- | --- |
| `anthropic` | `llm` | Anthropic Messages protocol |
| `openai` | `llm` | OpenAI Chat Completions protocol |
| `deepseek` | `llm` | OpenAI protocol with DeepSeek token parsing and local pricing |
| `openai` | `embedding` | OpenAI-compatible embedding endpoint |
| `tencent` | `asr` | Tencent SentenceRecognition |
| `fish` | `tts` | Fish Audio synthesis |
| `deepseek` | `balance` | Live account balance, independent of local token statistics |

### LLM

`model` and `base_url` are required (`deepseek` defaults to
`https://api.deepseek.com`). `api_key` can be omitted for an unauthenticated local
endpoint. Defaults: `max_tokens: 16384`, `temperature: 0.6`,
`timeout_seconds: 300`, `max_retries: 3`, `tool_choice: true`.
`thinking.effort` and each value in `thinking.stages` accept `low`, `high`, or `max`.
A stage override takes precedence over the default effort. Set `tool_choice: false`
for endpoints that do not implement tool forcing. The service's adapter selects the
wire protocol.

### Embedding

Use service `base_url` (Momoi appends `/v1/embeddings`, or `/embeddings` if the URL
already ends in `/v1`) or an explicit `endpoint` option. Defaults:

| Option | Default |
| --- | --- |
| `model` | `BAAI/bge-small-zh-v1.5` |
| `dimensions` | `512` |
| `calibration_profile` | `bge-small-zh-v1.5-momoi-v1` |
| `query_timeout_seconds` | `5` |
| `document_timeout_seconds` | `30` |
| `document_batch_size` | `8` |

A service `timeout_seconds` supplies both timeout defaults; explicit query/document
timeouts take precedence. Semantic space model, dimensions, and calibration must
match the encoder. Enable this binding before using `momoi embedding` commands.
Query failures still fall back to keyword recall and use the query circuit breaker.

### ASR

Tencent credentials require `secret_id` and `secret_key`. Options:
`region: ""`, `engine: 16k_zh`, `timeout_seconds: 30`,
`max_audio_bytes: 3145728`. The audio limit belongs to the inbound channel gate;
it is not sent to Tencent. Weixin's channel-provided transcription is independent
of this optional NapCat ASR integration.

### TTS

Fish requires `api_key` and `reference_id`. Defaults: `model: s2.1-pro-free`,
`base_url: https://api.fish.audio`, `format: mp3`, `latency: normal`,
`timeout_seconds: 60`, `max_audio_bytes: 20971520`.
Supported models: `s1`, `s2-pro`, `s2.1-pro`, `s2.1-pro-free`.
Formats: `mp3`, `wav`, `opus`. Latency: `normal`, `balanced`, `low`.

Synthesis makes the initial request plus three retries, delayed by 1, 2, and 4
seconds. Each failure logs bounded, redacted details. The `send_voice` tool waits
for synthesis, returns an error on failure, and recommends `send_bubbles` for text.
Success uses the same result structure as `send_bubbles`. See
[voice delivery behavior](./CONFIG.md#fish-audio-speech-synthesis).

### Account balance and token accounting

DeepSeek balance requires `api_key`; `base_url` defaults to
`https://api.deepseek.com` and `timeout_seconds` to `10`.
The dashboard queries the balance provider. API failure marks balance unavailable
while the rest of the overview remains usable.

Token counts are recorded from LLM responses independently. Selecting the
`deepseek` LLM adapter enables its local usage parser and pricing rules; selecting
only a DeepSeek balance binding does not apply DeepSeek prices to another LLM.
Disabling balance has no effect on token recording or LLM cost estimation.

## Extend the architecture

`integrations/contracts` defines LLM, ASR, TTS, embedding and balance capabilities.
`integrations/adapters` implements provider protocols. `ServiceRegistry` is the
composition boundary: application code receives services through these contracts,
and never imports concrete API clients. `HTTPTransport`, typed integration errors,
and retry infrastructure live beside the registry. Protocol-specific LLM replay,
tool encoding, telemetry and retries remain in `llm`.

Factories receive an option dictionary and `AdapterContext` (HTTP transport, dump
directory, semantic policy), not the main application config. They must not open
network resources in their constructors. Resolve services before entering the
registry's async scope. It creates only requested capabilities, enters their async
contexts or registers `close()`, and closes owned resources on exit. Injected test
services are owned by the caller. LLM clients and the embedding HTTPX pool retain
their own protocol sessions under the same registry lifecycle.

Install a Python module in the runtime environment and list it in `plugins`.
For example, `my_balance.py` implements a fixed balance source for local testing:

```python
from momoi.integrations.registry import register_adapter

class FixedBalance:
    def __init__(self, options, context):
        self.amount = options["amount"]

    async def balance(self):
        return {"source": "live", "currency": "CNY",
                "is_available": True, "total_balance": self.amount}

def validate(options):
    if set(options) != {"amount"} or not isinstance(options["amount"], str):
        raise ValueError("amount must be a string")

register_adapter("fixed", "balance", FixedBalance, validate=validate)
```

Add `plugins: [my_balance]`, a service with `adapter: fixed` and
`settings: {amount: "12.34"}`, and point the `balance` binding at it. Registration
includes an offline validator for that capability. Register another capability
for the same adapter when a vendor exposes more APIs. New adapters do not require
changes to Momoi config fields, runtime consumers, or the dashboard.

LLM implementations supply `config`, `accounting`, `usage_sink`, `thinking_sink`,
`usage_parser`, and `complete()` as defined by the contract. Embedders return
normalized vectors from `encode()` and expose `health()` / `close()`; binding
options also describe the semantic space's model, dimensions and calibration.
TTS failures raise `TTSError`; balance adapters raise `IntegrationError` with
sanitized details and an error category. Cancellation must propagate.
