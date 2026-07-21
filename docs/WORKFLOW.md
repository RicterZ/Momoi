# Webhook workflows

EN | [中文](./WORKFLOW.zh-CN.md)

Workflows let an external system trigger a predefined sequence in Momoi.

Use a Workflow when Home Assistant, Jellyfin, a monitoring system, or another service already knows that an event happened. Use MCP instead when Momoi should decide whether and how to call a capability during a conversation or Goal.

## Enable the webhook service

In `config.json`:

```json
{
  "webhooks": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8787,
    "token": "replace-with-a-random-token",
    "workflows": "workflows",
    "executors": "workflow-executors.yaml"
  }
}
```

Use `0.0.0.0` or a specific LAN address only when another machine must reach Momoi. Protect the endpoint with the Bearer token and use a TLS reverse proxy across untrusted networks.

Restart Momoi after adding or changing a Workflow.

## Create a message workflow

Create `workflows/event-message.yaml`:

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

Call it with JSON matching the declared inputs:

```bash
curl -X POST http://127.0.0.1:8787/webhooks/event-message \
  -H "Authorization: Bearer $MOMOI_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"event_prompt":"The washing machine has finished. Remind the owner to collect the laundry."}'
```

Momoi receives her current conversation context, relevant memory, goals, reminders, mood, activity, and emotion catalog. The event is not treated as owner speech, but the resulting message still comes from the same Momoi.

A message step may use HTTP to fetch data required by the event prompt, then must finish by sending one or more messages. It cannot call arbitrary MCP or file tools.

## Add a fixed command step

Command steps are defined separately from Workflows. The model never writes or edits the command line. A Workflow can only pass declared, validated inputs into fixed argument positions.

The generic example checks an HTTP endpoint before sending a message.

`workflow-executors.yaml`:

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

`workflows/url-check-event.yaml`:

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

Call it:

```bash
curl -X POST http://127.0.0.1:8787/webhooks/url-check-event \
  -H "Authorization: Bearer $MOMOI_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"event_prompt":"The monitored service is healthy again. Notify the owner.","target_url":"https://status.example.com/health"}'
```

Steps run in order. If the command cannot start, exits nonzero, or times out, later steps do not run.

## Workflow reference

Each `*.yaml` file in the workflow directory defines one Workflow.

```yaml
version: 1
id: lowercase-workflow-id
description: Optional human-readable description
inputs: {}
steps: []
```

| Field | Required | Description |
| --- | --- | --- |
| `version` | Yes | Must be `1` |
| `id` | Yes | Route ID used in `/webhooks/{id}` |
| `description` | No | Human-readable purpose |
| `inputs` | No | Declared JSON request fields |
| `steps` | Yes | Non-empty ordered list of `message` or `exec` steps |

Workflow, step, executor, input, and parameter identifiers must begin with a lowercase letter and may then contain lowercase letters, digits, `_`, or `-`. The maximum length is 64 characters.

Unknown fields are rejected when Workflows load.

## Input schemas

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

| Field | Applies to | Description |
| --- | --- | --- |
| `type` | All inputs | `string`, `integer`, `number`, or `boolean` |
| `required` | All inputs | Defaults to `false` |
| `max_length` | String | Positive character limit |
| `pattern` | String | Full regular-expression match |
| `format: url` | String | Require an absolute URL with a hostname |
| `schemes` | URL string | Non-empty allowlist; defaults to `http` and `https` |

The request body may contain only declared inputs. Missing required inputs, unknown inputs, wrong JSON types, non-finite numbers, and invalid URLs return HTTP `400`.

An input referenced by an `exec` step must be present because the command argument cannot be omitted.

## Message steps

```yaml
- id: notify
  uses: message
  prompt: "Service ${inputs.service_name} changed state: ${inputs.state}"
```

`prompt` must be a non-empty string. `${inputs.<name>}` may appear anywhere in it, and every referenced name must be declared by the Workflow.

The prompt describes what happened and what Momoi should communicate. It should not contain credentials or pretend to be owner speech.

## Exec steps

```yaml
- id: run-action
  uses: exec
  executor: action-name
  args:
    target: "${inputs.target}"
```

| Field | Required | Description |
| --- | --- | --- |
| `id` | Yes | Unique step ID within the Workflow |
| `uses` | Yes | `exec` |
| `executor` | Yes | Name from `workflow-executors.yaml` |
| `args` | Yes | One entry for every declared executor parameter |

Each argument value must be exactly one `${inputs.<name>}` template. Partial interpolation such as `prefix-${inputs.target}` is rejected. Use a separate fixed `argv` token for prefixes and flags.

## Executor reference

```yaml
version: 1
executors:
  action-name:
    parameters: {}
    argv: [command, fixed-argument]
    env: {}
    timeout_seconds: 180
```

| Field | Required | Description |
| --- | --- | --- |
| `parameters` | No | Validated parameters accepted from Workflow steps |
| `argv` | Yes | Non-empty array passed directly to the process, without a shell |
| `env` | No | Additional process environment |
| `timeout_seconds` | No | Positive timeout; default `180` |

Executor parameters use the same schema as Workflow inputs.

`argv` and `env` values may be static strings or one complete template:

- `${args.<parameter>}` — a validated executor argument
- `${config.owner_qq}` — configured owner QQ
- `${config.napcat_url}` — configured NapCat WebSocket URL

Templates cannot be embedded inside a larger token. Momoi's existing process environment is inherited, so commands can read secrets from environment variables without placing them in Workflow input.

Do not define a generic shell executor or accept a command string from the request body. Define one narrow executor per allowed action and validate every dynamic argument.

## HTTP API

### Start a Workflow

```text
POST /webhooks/{workflow_id}
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <optional-key>
```

Successful requests return HTTP `202`:

```json
{
  "run_id": "7ed3e848d70e4f2f98ce0c8a3b2dc2fa",
  "workflow": "event-message",
  "state": "pending"
}
```

`Idempotency-Key` is optional. Reusing the same key for the same Workflow returns the existing run instead of creating a duplicate. Keys must be non-empty, contain no control characters, and be at most 200 characters.

The maximum request size is 64 KiB.

### Read Workflow state

```text
GET /webhook-runs/{run_id}
Authorization: Bearer <token>
```

The response includes the current Workflow and step states. Unknown Workflow IDs return `404`; invalid inputs return `400`; a missing or incorrect token returns `401`.

## Home Assistant example

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

Call `rest_command.momoi_event` with an `event_prompt` value from an automation. Use a network address reachable from Home Assistant, and keep the token in Home Assistant secrets for a real deployment.

## Execution and recovery

- Steps execute sequentially.
- A `message` step waits until its QQ messages are delivered before the next step starts.
- An `exec` step succeeds only with exit code `0`.
- A nonzero exit or start failure marks the step failed.
- A timeout has an ambiguous outcome because the process may have caused an external effect before it was stopped.
- Completed steps are not repeated after a normal restart.

Use narrow, idempotent commands where possible. Check Workflow state before manually retrying an event whose command outcome is uncertain.
